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
import types
from pathlib import Path

import httpx
import pytest

# Importado de verdade antes dos stubs de `src.config.env`/`src.utils.log` que a
# fixture instala (ver test_google_search_retry.py).
from src.observability import tracing  # noqa: F401


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
    env = types.SimpleNamespace(GEMINI_API_KEY="test-gemini-key", LINK_BLACKLIST=[])
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
