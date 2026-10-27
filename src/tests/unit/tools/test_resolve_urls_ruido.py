"""Resolução dos links do grounding pelo `Location` (CHATR-314) e ruído de
alertas do `resolve_urls` (CHATR-299).

Cada fonte do Gemini é um link de redirect do Google. A resolução faz uma única
requisição a ele, sem seguir o redirecionamento, e usa o `Location`: o site de
destino nunca é contatado. Estes testes fixam isso, a nova tentativa curta só
para 5xx e erro de transporte, e a classificação de alerta do CHATR-299, agora
só para o salto no Google: 429 e timeout ficam no log, o 504 e o erro inesperado
continuam reportados, e nenhuma mensagem sai vazia.
"""

import asyncio
import importlib.util
import sys
import time
import types
from pathlib import Path

import httpx
import pytest

# Importado de verdade antes dos stubs de `src.config.env`/`src.utils.log` que a
# fixture instala (ver test_google_search_retry.py).
from src.observability import tracing  # noqa: F401

# Idem: o `GeminiService` monta no boot o prompt de cada modelo da configuração.
from src.config import gemini_search


PROJECT_ROOT = Path(__file__).resolve().parents[4]
GEMINI_MODULE_PATH = (
    PROJECT_ROOT / "src" / "tools" / "google_search" / "gemini_service.py"
)
URI = "https://vertexaisearch.cloud.google.com/grounding-api-redirect/abc"
URL_FINAL = "https://prefeitura.rio/servico"
REDIRECT = (302, URL_FINAL)


@pytest.fixture
def resolve(monkeypatch):
    """Carrega gemini_service.py isolado e devolve um executor de `process_link`."""
    reported, warnings = [], []

    async def fake_send_api_error(**kwargs):
        reported.append(kwargs)
        return True

    def registra_warning(msg, *_a, **_k):
        warnings.append(msg)

    noop = lambda *_a, **_k: None  # noqa: E731
    env = types.SimpleNamespace(
        GEMINI_API_KEY="test-gemini-key",
        GEMINI_SEARCH_CONFIG=gemini_search.DEFAULT_CONFIG,
    )
    monkeypatch.setitem(sys.modules, "src.config", types.SimpleNamespace(env=env))
    monkeypatch.setitem(sys.modules, "src.config.env", env)
    monkeypatch.setitem(
        sys.modules,
        "src.utils.log",
        types.SimpleNamespace(
            logger=types.SimpleNamespace(
                info=noop, warning=registra_warning, error=noop, debug=noop
            )
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "src.utils.error_interceptor",
        types.SimpleNamespace(
            interceptor=lambda *_a, **_k: lambda func: func,
            send_api_error=fake_send_api_error,
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "src.utils.http_client",
        types.SimpleNamespace(InterceptedHTTPClient=object),
    )

    spec = importlib.util.spec_from_file_location(
        "test_resolve_urls_module", GEMINI_MODULE_PATH
    )
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "test_resolve_urls_module", module)
    spec.loader.exec_module(module)

    async def sem_espera(_seconds):
        return None

    monkeypatch.setattr(module.asyncio, "sleep", sem_espera)

    def run(*desfechos, uri=URI):
        session = FakeSession(desfechos)
        link = asyncio.run(module.process_link(session, {"uri": uri}))
        return link, session

    return types.SimpleNamespace(
        module=module, run=run, reported=reported, warnings=warnings
    )


class FakeSession:
    """Sessão HTTP falsa que só aceita GET. Cada desfecho é uma exceção a levantar,
    um status HTTP ou um par (status, Location); o último se repete quando as
    chamadas passam do tamanho da lista."""

    def __init__(self, desfechos):
        self.desfechos = list(desfechos)
        self.chamadas = []

    async def get(self, url, **kwargs):
        self.chamadas.append((url, kwargs))
        desfecho = self.desfechos[min(len(self.chamadas), len(self.desfechos)) - 1]
        if isinstance(desfecho, Exception):
            raise desfecho
        status, location = desfecho if isinstance(desfecho, tuple) else (desfecho, None)
        headers = {"location": location} if location is not None else {}
        return httpx.Response(
            status, headers=headers, request=httpx.Request("GET", url)
        )


# --------------------------------------------------------------------------------
# Resolução pelo `Location`
# --------------------------------------------------------------------------------


def test_location_absoluto_vira_a_url_da_fonte(resolve):
    link, session = resolve.run(REDIRECT)

    assert link == {"uri": URI, "url": URL_FINAL, "error": None}
    assert resolve.reported == []


def test_uma_requisicao_ao_google_sem_seguir_o_redirect(resolve):
    """Nenhuma conexão com o site de destino: só o salto no Google."""
    _, session = resolve.run(REDIRECT)

    assert len(session.chamadas) == 1
    url, kwargs = session.chamadas[0]
    assert url == URI
    assert kwargs["follow_redirects"] is False
    assert kwargs["timeout"] == resolve.module.GROUNDING_REDIRECT_TIMEOUT_SECONDS


def test_location_relativo_e_resolvido_contra_o_link_do_google(resolve):
    link, _ = resolve.run((302, "/outra/rota?x=1"))

    assert link["url"] == "https://vertexaisearch.cloud.google.com/outra/rota?x=1"
    assert link["error"] is None


@pytest.mark.parametrize("status", [301, 303, 307, 308])
def test_qualquer_redirect_com_location_resolve(resolve, status):
    link, _ = resolve.run((status, URL_FINAL))

    assert link["url"] == URL_FINAL


def test_location_sem_http_mantem_o_link_do_google(resolve):
    link, _ = resolve.run((302, "javascript:alert(1)"))

    assert link["url"] == URI
    assert link["error"] == "Erro ao resolver URL: HTTP 302: Location inválido"


def test_link_que_nao_e_do_google_fica_como_esta_sem_requisicao(resolve):
    link, session = resolve.run(REDIRECT, uri=URL_FINAL)

    assert link == {"uri": URL_FINAL, "url": URL_FINAL, "error": None}
    assert session.chamadas == []


# --------------------------------------------------------------------------------
# Falha no salto do Google: mantém o link do Google
# --------------------------------------------------------------------------------


def test_redirect_sem_location_mantem_o_link_do_google_e_alerta(resolve):
    link, session = resolve.run(302)

    assert len(session.chamadas) == 1
    assert link["url"] == URI
    assert link["error"] == "Erro ao resolver URL: HTTP 302: sem Location"
    assert len(resolve.reported) == 1
    assert resolve.reported[0]["status_code"] == 302


def test_resposta_sem_redirect_mantem_o_link_do_google(resolve):
    link, session = resolve.run(200)

    assert len(session.chamadas) == 1
    assert link["url"] == URI
    assert link["error"] == "Erro ao resolver URL: HTTP 200: sem redirecionamento"


def test_404_do_google_alerta_sem_nova_tentativa(resolve):
    link, session = resolve.run(404, REDIRECT)

    assert len(session.chamadas) == 1
    assert link["url"] == URI
    assert len(resolve.reported) == 1
    assert resolve.reported[0]["status_code"] == 404
    assert resolve.reported[0]["error_message"] == "HTTP 404: Not Found"
    assert resolve.reported[0]["api_endpoint"] == URI


def test_429_nao_alerta_e_nao_retenta(resolve):
    link, session = resolve.run(429, REDIRECT)

    assert resolve.reported == []
    assert len(session.chamadas) == 1
    assert link["url"] == URI
    assert link["error"] == "Erro ao resolver URL: HTTP 429: Too Many Requests"
    assert any("429" in w for w in resolve.warnings)


def test_5xx_ganha_uma_nova_tentativa(resolve):
    link, session = resolve.run(503, REDIRECT)

    assert len(session.chamadas) == 2
    assert link == {"uri": URI, "url": URL_FINAL, "error": None}
    assert resolve.reported == []


def test_504_persistente_continua_alertando(resolve):
    """O "Gateway Timeout" do 504 tem "timeout" no texto: a classificação por tipo
    é o que impede que ele seja silenciado junto com o timeout do httpx."""
    link, session = resolve.run(504)

    assert len(session.chamadas) == resolve.module.GROUNDING_REDIRECT_ATTEMPTS
    assert len(resolve.reported) == 1
    assert resolve.reported[0]["status_code"] == 504
    assert resolve.reported[0]["error_message"] == "HTTP 504: Gateway Timeout"
    assert link["url"] == URI


def test_timeout_retenta_nao_alerta_e_fica_no_log(resolve):
    link, session = resolve.run(httpx.ReadTimeout(""), httpx.ConnectTimeout(""))

    assert len(session.chamadas) == 2
    assert resolve.reported == []
    assert link["url"] == URI
    # A falha final é a da última tentativa.
    assert link["error"] == "Erro ao resolver URL: ConnectTimeout"
    assert any("ConnectTimeout" in w for w in resolve.warnings)


def test_connect_error_ganha_uma_nova_tentativa(resolve):
    link, session = resolve.run(httpx.ConnectError("conexão recusada"), REDIRECT)

    assert len(session.chamadas) == 2
    assert link == {"uri": URI, "url": URL_FINAL, "error": None}
    assert resolve.reported == []


def test_connect_error_persistente_alerta(resolve):
    link, session = resolve.run(httpx.ConnectError("conexão recusada"))

    assert len(session.chamadas) == resolve.module.GROUNDING_REDIRECT_ATTEMPTS
    assert len(resolve.reported) == 1
    assert resolve.reported[0]["status_code"] == 0
    assert resolve.reported[0]["error_message"] == "ConnectError: conexão recusada"
    assert link["url"] == URI


def test_erro_inesperado_alerta_com_mensagem_nao_vazia_sem_retentar(resolve):
    link, session = resolve.run(RuntimeError(), REDIRECT)

    assert len(session.chamadas) == 1
    assert len(resolve.reported) == 1
    assert resolve.reported[0]["status_code"] == 0
    assert resolve.reported[0]["error_message"] == "RuntimeError"
    assert link["error"] == "Erro ao resolver URL: RuntimeError"


def test_pior_caso_cabe_na_reserva_da_resolucao(resolve):
    """Duas tentativas estourando o timeout, mais a espera, dentro dos 10 s."""
    m = resolve.module
    pior_caso = (
        m.GROUNDING_REDIRECT_ATTEMPTS * m.GROUNDING_REDIRECT_TIMEOUT_SECONDS
        + (m.GROUNDING_REDIRECT_ATTEMPTS - 1) * m.GROUNDING_REDIRECT_BACKOFF_SECONDS
    )
    assert pior_caso < m.URL_RESOLUTION_RESERVE_SECONDS


# --------------------------------------------------------------------------------
# Blacklist de domínios nas citações e na lista de fontes (CHATR-314)
# --------------------------------------------------------------------------------

TEXTO = "Primeiro fato. Segundo fato."


def _uri(n: int) -> str:
    return f"https://vertexaisearch.cloud.google.com/grounding-api-redirect/{n}"


def _resposta(*fontes):
    """Resposta do Gemini com uma fonte por frase de `TEXTO`. Cada fonte é um par
    (título, domínio), como o grounding entrega em `chunk.web`."""
    chunks, supports = [], []
    fim = 0
    for i, (titulo, dominio) in enumerate(fontes):
        chunks.append(
            types.SimpleNamespace(
                web=types.SimpleNamespace(uri=_uri(i), title=titulo, domain=dominio)
            )
        )
        fim = TEXTO.index(".", fim) + 1
        supports.append(
            types.SimpleNamespace(
                segment=types.SimpleNamespace(start_index=0, end_index=fim),
                grounding_chunk_indices=[i],
            )
        )
    metadata = types.SimpleNamespace(
        grounding_chunks=chunks, grounding_supports=supports
    )
    return types.SimpleNamespace(
        candidates=[types.SimpleNamespace(grounding_metadata=metadata)], text=TEXTO
    )


def _montar_resposta(module, response, resolved_map):
    """O pós-processamento do `google_search`: citações, texto e fontes."""
    citations = module.get_citations(response=response, resolved_urls_map=resolved_map)
    texto = module.format_text_with_citations(response.text, citations)
    return texto, module.get_sources_list(citations, texto)


def test_citacoes_e_fontes_comparam_por_dominio_e_nao_por_trecho(resolve):
    """`x.com` bloqueia o `mobile.x.com`, mas não o `netflix.com`, que a
    `LINK_BLACKLIST` barrava por conter o trecho."""
    response = _resposta(("x.com", None), ("netflix.com", None))
    resolved = {
        _uri(0): {"url": "https://mobile.x.com/post/1", "error": None},
        _uri(1): {"url": "https://www.netflix.com/br/", "error": None},
    }

    texto, fontes = _montar_resposta(resolve.module, response, resolved)

    assert [f["url"] for f in fontes] == ["https://www.netflix.com/br/"]
    assert "x.com/post" not in texto
    assert "https://www.netflix.com/br/" in texto


class FakeClient:
    """Substitui o `InterceptedHTTPClient` do `resolve_urls`: cada link do Google
    redireciona para o destino dado em `destinos`."""

    def __init__(self, destinos):
        self.destinos = destinos
        self.pedidos = []

    def __call__(self, **_kwargs):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc):
        return False

    async def get(self, url, **_kwargs):
        self.pedidos.append(url)
        return httpx.Response(
            302,
            headers={"location": self.destinos[url]},
            request=httpx.Request("GET", url),
        )


def _resolver(module, monkeypatch, response, destinos):
    """Roda a resolução de verdade (`_resolver_urls_no_prazo` → `resolve_urls` →
    `process_link`) sobre as fontes da resposta, com o Google falso."""
    client = FakeClient(destinos)
    monkeypatch.setattr(module, "InterceptedHTTPClient", client)
    chunks = response.candidates[0].grounding_metadata.grounding_chunks
    resolved = asyncio.run(
        module._resolver_urls_no_prazo(chunks, deadline_at=time.monotonic() + 10)
    )
    return resolved, client


def test_fonte_bloqueada_pelo_dominio_do_grounding_nem_e_resolvida(
    resolve, monkeypatch
):
    response = _resposta(("Perfil", "m.facebook.com"), ("IPTU", "prefeitura.rio"))
    destinos = {
        _uri(0): "https://m.facebook.com/perfil",
        _uri(1): "https://prefeitura.rio/iptu",
    }

    resolved, client = _resolver(resolve.module, monkeypatch, response, destinos)

    assert client.pedidos == [_uri(1)]
    assert resolved == {_uri(1): {"url": "https://prefeitura.rio/iptu", "error": None}}


def test_sem_dominio_vale_o_titulo_com_forma_de_dominio(resolve, monkeypatch):
    """O Gemini API devolve `domain=None` e o domínio do site no `title`."""
    response = _resposta(("facebook.com", None), ("Prefeitura do Rio", None))
    destinos = {
        _uri(0): "https://m.facebook.com/perfil",
        _uri(1): "https://prefeitura.rio/iptu",
    }

    _, client = _resolver(resolve.module, monkeypatch, response, destinos)

    assert client.pedidos == [_uri(1)]


def test_dominio_bloqueado_vindo_pelo_location_some_da_resposta(resolve, monkeypatch):
    """O domínio do grounding diverge do destino: quem bloqueia é o `Location`. A
    fonte some das citações e da lista, e o link do Google também não aparece."""
    response = _resposta(("Notícia", "noticia.com.br"), ("IPTU", "prefeitura.rio"))
    destinos = {
        _uri(0): "https://www.facebook.com/photo/?fbid=1",
        _uri(1): "https://prefeitura.rio/iptu",
    }

    resolved, client = _resolver(resolve.module, monkeypatch, response, destinos)
    texto, fontes = _montar_resposta(resolve.module, response, resolved)

    assert sorted(client.pedidos) == [_uri(0), _uri(1)]
    assert _uri(0) not in resolved
    assert [f["url"] for f in fontes] == ["https://prefeitura.rio/iptu"]
    assert "facebook" not in texto
    assert "vertexaisearch" not in texto


def test_prazo_esgotado_nao_devolve_a_fonte_bloqueada(resolve, monkeypatch):
    """No estouro do prazo as fontes saem com o link do Google; a bloqueada não."""
    monkeypatch.setattr(resolve.module, "URL_RESOLUTION_RESERVE_SECONDS", 0.05)

    async def resolve_urls_travado(**_kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr(resolve.module, "resolve_urls", resolve_urls_travado)
    response = _resposta(("Perfil", "facebook.com"), ("IPTU", "prefeitura.rio"))
    chunks = response.candidates[0].grounding_metadata.grounding_chunks

    resolved = asyncio.run(
        resolve.module._resolver_urls_no_prazo(chunks, time.monotonic() + 10)
    )

    assert resolved == {_uri(1): {"url": _uri(1), "error": "prazo esgotado"}}


def test_pagina_de_busca_do_google_vinda_pelo_location_some(resolve, monkeypatch):
    response = _resposta(("google.com", None), ("IPTU", "prefeitura.rio"))
    destinos = {
        _uri(0): "https://www.google.com/search?q=iptu+rio",
        _uri(1): "https://prefeitura.rio/iptu",
    }

    resolved, _ = _resolver(resolve.module, monkeypatch, response, destinos)
    texto, fontes = _montar_resposta(resolve.module, response, resolved)

    assert [f["url"] for f in fontes] == ["https://prefeitura.rio/iptu"]
    assert "google.com/search" not in texto
    assert "vertexaisearch" not in texto


def test_pagina_de_busca_do_google_some_das_citacoes(resolve):
    """Também quando já chega resolvida ao pós-processamento."""
    response = _resposta(("google.com", None), ("IPTU", "prefeitura.rio"))
    resolved = {
        _uri(0): {"url": "https://google.com/search?q=x", "error": None},
        _uri(1): {"url": "https://prefeitura.rio/iptu", "error": None},
    }

    texto, fontes = _montar_resposta(resolve.module, response, resolved)

    assert [f["url"] for f in fontes] == ["https://prefeitura.rio/iptu"]
    assert "google.com/search" not in texto
