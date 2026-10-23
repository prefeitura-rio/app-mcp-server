"""Ruído de alertas do `resolve_urls` (CHATR-299).

A resolução dos links de redirect do grounding falha com frequência sem impacto
para o cidadão: a fonte sai com a URL de redirect, que funciona. Dois desses casos
chegavam ao interceptor como erro de produção: o timeout do httpx, cuja mensagem
é vazia ("Erro no HEAD: "), e o 429 do endpoint de redirect. Estes testes fixam
que os dois ficam só no log, que o 504 e o erro inesperado continuam reportados e
que nenhuma mensagem sai vazia. E que exceção no HEAD sempre segue para o GET, até
na última tentativa.
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

    def run(head, get):
        session = FakeSession(head, get)
        link = asyncio.run(module.process_link(session, {"uri": URI}))
        return link, session

    return types.SimpleNamespace(run=run, reported=reported, warnings=warnings)


class FakeSession:
    """Sessão HTTP falsa. Cada desfecho é uma exceção a levantar ou um status HTTP;
    o último se repete quando as chamadas passam do tamanho da lista."""

    def __init__(self, head, get):
        self.desfechos = {"HEAD": list(head), "GET": list(get)}
        self.chamadas = {"HEAD": 0, "GET": 0}

    async def _responder(self, metodo):
        fila = self.desfechos[metodo]
        desfecho = fila[min(self.chamadas[metodo], len(fila) - 1)]
        self.chamadas[metodo] += 1
        if isinstance(desfecho, Exception):
            raise desfecho
        request = httpx.Request(metodo, URL_FINAL if desfecho == 200 else URI)
        return httpx.Response(desfecho, request=request)

    async def head(self, _uri, **_kwargs):
        return await self._responder("HEAD")

    async def get(self, _uri, **_kwargs):
        return await self._responder("GET")


def test_timeout_nao_alerta_e_fica_no_log(resolve):
    link, _ = resolve.run(head=[httpx.ReadTimeout("")], get=[httpx.ConnectTimeout("")])

    assert resolve.reported == []
    assert link["url"] == URI
    # A falha final é a do GET, a última chamada feita.
    assert link["error"] == "Erro ao resolver URL: ConnectTimeout"
    assert any("ConnectTimeout" in w for w in resolve.warnings)


def test_timeout_no_get_tambem_nao_alerta(resolve):
    """403 no HEAD significa "HEAD não suportado" e manda para o GET; quem
    estoura em todas as tentativas é o GET."""
    link, session = resolve.run(head=[403], get=[httpx.ReadTimeout("")])

    assert resolve.reported == []
    assert session.chamadas["GET"] == 3
    assert link["url"] == URI
    assert link["error"] == "Erro ao resolver URL: ReadTimeout"


def test_429_nao_alerta_e_nao_retenta(resolve):
    link, session = resolve.run(head=[429], get=[200])

    assert resolve.reported == []
    assert session.chamadas == {"HEAD": 1, "GET": 0}
    assert link["error"] == "Erro ao resolver URL: HTTP 429: Too Many Requests"
    assert any("429" in w for w in resolve.warnings)


def test_504_continua_alertando(resolve):
    """O "Gateway Timeout" do 504 tem "timeout" no texto: a classificação por tipo
    é o que impede que ele seja silenciado junto com o timeout do httpx."""
    link, _ = resolve.run(head=[504], get=[504])

    assert len(resolve.reported) == 1
    assert resolve.reported[0]["status_code"] == 504
    assert resolve.reported[0]["error_message"] == "HTTP 504: Gateway Timeout"
    assert link["url"] == URI


def test_erro_inesperado_alerta_com_mensagem_nao_vazia(resolve):
    link, _ = resolve.run(head=[RuntimeError()], get=[RuntimeError()])

    assert len(resolve.reported) == 1
    assert resolve.reported[0]["status_code"] == 0
    assert resolve.reported[0]["error_message"] == "RuntimeError"
    assert link["error"] == "Erro ao resolver URL: RuntimeError"


def test_excecao_no_head_tenta_o_get_ate_na_ultima_tentativa(resolve):
    """Antes, a última tentativa desistia no HEAD sem tentar o GET."""
    erro = httpx.ConnectError("conexão recusada")
    link, session = resolve.run(head=[erro], get=[erro, erro, 200])

    assert session.chamadas == {"HEAD": 3, "GET": 3}
    assert resolve.reported == []
    assert link == {"uri": URI, "url": URL_FINAL, "error": None}


def test_falha_final_traz_o_erro_do_get_e_nao_do_head(resolve):
    """O HEAD lança exceção e o servidor responde 503 ao GET: o alerta é o 503, que
    é o erro real, e não "Erro no HEAD"."""
    link, session = resolve.run(
        head=[httpx.ConnectError("conexão recusada")], get=[503]
    )

    assert session.chamadas == {"HEAD": 3, "GET": 3}
    assert len(resolve.reported) == 1
    assert resolve.reported[0]["status_code"] == 503
    assert resolve.reported[0]["error_message"] == "HTTP 503: Service Unavailable"
    assert link["url"] == URI
    assert "Erro no HEAD" not in link["error"]


def test_redirect_resolvido_nao_alerta(resolve):
    link, _ = resolve.run(head=[200], get=[200])

    assert resolve.reported == []
    assert link == {"uri": URI, "url": URL_FINAL, "error": None}
