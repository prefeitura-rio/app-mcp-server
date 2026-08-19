"""Testes da política de retry/backoff da busca via Gemini (CHATR-122).

O erro 503/UNAVAILABLE do Gemini é transitório. Estes testes cobrem: que a janela de
retry é efetivamente exercida, que o esgotamento devolve mensagem tratada (nunca a
exceção crua, que antes chegava ao usuário final) e que uma falha do Typesense cai no
fallback do Google em vez de derrubar a tool.
"""

import asyncio
import importlib.util
import json
import sys
import types
from pathlib import Path

import httpx
import pytest
from google.genai import errors as genai_errors


PROJECT_ROOT = Path(__file__).resolve().parents[4]
GEMINI_MODULE_PATH = (
    PROJECT_ROOT / "src" / "tools" / "google_search" / "gemini_service.py"
)
SEARCH_MODULE_PATH = PROJECT_ROOT / "src" / "tools" / "search.py"


def _load_module(module_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _silent_logger():
    noop = lambda *_args, **_kwargs: None  # noqa: E731
    return types.SimpleNamespace(
        info=noop, warning=noop, error=noop, debug=noop, exception=noop
    )


def server_error(code: int = 503, status: str = "UNAVAILABLE", details=None):
    """Erro do Gemini como o SDK o constrói a partir do corpo da resposta."""
    body = {
        "error": {
            "code": code,
            "status": status,
            "message": "The model is overloaded. Please try again later.",
        }
    }
    if details is not None:
        body["error"]["details"] = details
    return genai_errors.ServerError(code, body)


def client_error(code: int, status: str = "INVALID_ARGUMENT"):
    return genai_errors.ClientError(
        code, {"error": {"code": code, "status": status, "message": "erro"}}
    )


def success_response():
    grounding = types.SimpleNamespace(
        grounding_chunks=[
            types.SimpleNamespace(
                web=types.SimpleNamespace(uri="https://carioca.rio/x", title="X")
            )
        ],
        grounding_supports=[],
        web_search_queries=["iptu rio"],
    )
    return types.SimpleNamespace(
        candidates=[types.SimpleNamespace(grounding_metadata=grounding)],
        text="Resposta com fonte oficial.",
        usage_metadata=None,
    )


class FakeModels:
    """Devolve um desfecho por chamada; repete o último quando a lista acaba."""

    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.calls = 0

    async def generate_content(self, **_kwargs):
        self.calls += 1
        outcome = self._outcomes[min(self.calls - 1, len(self._outcomes) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.fixture
def env_stub():
    return types.SimpleNamespace(
        GEMINI_API_KEY="test-gemini-key",
        GEMINI_MODEL="gemini-test",
        GEMINI_SEARCH_RETRY_ATTEMPTS=4,
        GEMINI_SEARCH_RETRY_BASE_SECONDS=2.0,
        GEMINI_SEARCH_RETRY_MAX_BACKOFF_SECONDS=16.0,
        GEMINI_SEARCH_RETRY_BUDGET_SECONDS=60.0,
        LINK_BLACKLIST=[],
        TYPESENSE_ACTIVE="false",
        TYPESENSE_HUB_SEARCH_URL="",
        TYPESENSE_PARAMETERS="none",
    )


@pytest.fixture
def gemini(monkeypatch, env_stub):
    """Carrega gemini_service.py isolado, com as dependências de I/O falsas."""
    reported_errors = []
    slept = []

    async def fake_send_api_error(**kwargs):
        reported_errors.append(kwargs)
        return True

    monkeypatch.setitem(sys.modules, "src.config", types.SimpleNamespace(env=env_stub))
    monkeypatch.setitem(sys.modules, "src.config.env", env_stub)
    monkeypatch.setitem(
        sys.modules, "src.utils.log", types.SimpleNamespace(logger=_silent_logger())
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

    module = _load_module("test_gemini_service_module", GEMINI_MODULE_PATH)

    # Etapas de pós-processamento que fazem rede ou dependem do formato real da
    # resposta — irrelevantes para a política de retry.
    async def fake_resolve_urls(**_kwargs):
        return {}

    monkeypatch.setattr(module, "resolve_urls", fake_resolve_urls)
    monkeypatch.setattr(module, "get_citations", lambda **_kwargs: [])
    monkeypatch.setattr(module, "format_text_with_citations", lambda text, _c: text)
    monkeypatch.setattr(
        module,
        "get_sources_list",
        lambda *_a: [{"url": "https://carioca.rio/x", "index": 1}],
    )

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(module.asyncio, "sleep", fake_sleep)

    def run(outcomes, **kwargs):
        models = FakeModels(outcomes)
        module.gemini_service.client = types.SimpleNamespace(
            aio=types.SimpleNamespace(models=models)
        )
        result = asyncio.run(
            module.gemini_service.google_search(query="iptu 2026", **kwargs)
        )
        return result, models

    return types.SimpleNamespace(
        module=module, run=run, slept=slept, reported_errors=reported_errors
    )


def test_recupera_apos_503_transitorio(gemini):
    """Dois 503 seguidos de sucesso: a busca se recupera dentro da janela de retry."""
    result, models = gemini.run([server_error(), server_error(), success_response()])

    assert models.calls == 3
    assert result["success"] is True
    assert result["text"] == "Resposta com fonte oficial."
    assert result["sources"]

    # Backoff exponencial com teto e equal jitter: base 2 -> [1,2]s, depois [2,4]s.
    assert len(gemini.slept) == 2
    assert 1.0 <= gemini.slept[0] <= 2.0
    assert 2.0 <= gemini.slept[1] <= 4.0


def test_esgotamento_devolve_mensagem_tratada(gemini):
    """Com todas as tentativas em 503, o usuário não pode ver a exceção crua."""
    result, models = gemini.run([server_error()])

    assert models.calls == 4
    assert result["success"] is False
    assert result["sources"] == []
    assert result["text"] == gemini.module.SEARCH_UNAVAILABLE_MESSAGE

    # O que vazava para o chat antes do CHATR-122.
    assert "503" not in result["text"]
    assert "UNAVAILABLE" not in result["text"]
    assert "Erro na pesquisa Google" not in result["text"]

    assert result["error"]["kind"] == "gemini_unavailable"
    assert result["error"]["code"] == 503
    assert result["error"]["status"] == "UNAVAILABLE"
    assert result["error"]["attempts"] == 4
    assert result["retry_attempts"] == 4


def test_esgotamento_e_reportado_ao_interceptor(gemini):
    """A falha é devolvida, não levantada: o report precisa ser explícito."""
    gemini.run([server_error()])

    assert len(gemini.reported_errors) == 1
    reported = gemini.reported_errors[0]
    assert reported["status_code"] == 503
    assert "gemini_unavailable" in reported["error_message"]
    assert reported["source"]["function"] == "google_search"


def test_erro_4xx_nao_retenta(gemini):
    """4xx não melhora com repetição: uma tentativa e para."""
    result, models = gemini.run([client_error(400)])

    assert models.calls == 1
    assert gemini.slept == []
    assert result["success"] is False
    assert result["error"]["kind"] == "gemini_client_error"
    assert result["error"]["attempts"] == 1
    assert result["text"] == gemini.module.SEARCH_FAILED_MESSAGE


def test_429_e_tratado_como_transitorio(gemini):
    """429 é saturação de cota: mesma família do 503, deve retentar."""
    result, models = gemini.run(
        [client_error(429, status="RESOURCE_EXHAUSTED"), success_response()]
    )

    assert models.calls == 2
    assert result["success"] is True


def test_retry_delay_do_servidor_tem_precedencia(gemini):
    """Quando a API informa quanto esperar, o valor dela vale mais que o backoff."""
    error = server_error(
        details=[
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "7s"}
        ]
    )
    _, models = gemini.run([error, success_response()])

    assert models.calls == 2
    assert gemini.slept == [7.0]


def test_retry_delay_respeita_o_teto(gemini, env_stub):
    """Um retryDelay absurdo não pode travar o chat: continua limitado pelo teto."""
    env_stub.GEMINI_SEARCH_RETRY_MAX_BACKOFF_SECONDS = 5.0
    error = server_error(
        details=[
            {"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "600s"}
        ]
    )
    gemini.run([error, success_response()])

    assert gemini.slept == [5.0]


def test_backoff_respeita_o_teto(gemini, env_stub):
    """O crescimento exponencial para no teto configurado."""
    env_stub.GEMINI_SEARCH_RETRY_BASE_SECONDS = 100.0
    env_stub.GEMINI_SEARCH_RETRY_MAX_BACKOFF_SECONDS = 6.0
    gemini.run([server_error()])

    assert gemini.slept
    assert all(3.0 <= wait <= 6.0 for wait in gemini.slept)


def test_orcamento_de_latencia_interrompe_os_retries(gemini, env_stub):
    """Não adianta retentar se a espera estoura o tempo que o usuário aguenta."""
    env_stub.GEMINI_SEARCH_RETRY_BUDGET_SECONDS = 0.5
    result, models = gemini.run([server_error()])

    assert models.calls == 1
    assert gemini.slept == []
    assert result["error"]["attempts"] == 1


def test_retry_attempts_explicito_tem_precedencia_sobre_o_env(gemini):
    result, models = gemini.run([server_error()], retry_attempts=2)

    assert models.calls == 2
    assert result["error"]["attempts"] == 2


def test_classificacao_de_erros(gemini):
    classify = gemini.module.classify_search_error

    assert classify(server_error())["kind"] == "gemini_unavailable"
    assert classify(server_error(code=500, status="INTERNAL"))["kind"] == (
        "gemini_server_error"
    )
    assert classify(client_error(429))["retryable"] is True
    assert classify(client_error(403))["retryable"] is False
    assert classify(asyncio.TimeoutError())["kind"] == "timeout"
    assert classify(ValueError("boom"))["kind"] == "unexpected_error"


# --------------------------------------------------------------------------------
# get_google_search: o Typesense não pode derrubar o fallback do Google
# --------------------------------------------------------------------------------


@pytest.fixture
def search(monkeypatch, env_stub):
    env_stub.TYPESENSE_ACTIVE = "true"
    env_stub.TYPESENSE_HUB_SEARCH_URL = "https://typesense.local/search"

    google_response = {
        "text": "resposta do google",
        "sources": [{"url": "https://carioca.rio/x"}],
        "web_search_queries": ["x"],
        "id": "abc",
        "success": True,
    }
    state = types.SimpleNamespace(google_response=google_response, bq_calls=[])

    async def fake_google_search(**_kwargs):
        return state.google_response

    async def fake_save_response_in_bq_background(**kwargs):
        state.bq_calls.append(kwargs)

    class FakeHubSearchRequest:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    async def fake_hub_search(request):  # sobrescrito em cada teste
        raise AssertionError("hub_search não configurado")

    monkeypatch.setitem(sys.modules, "src.config", types.SimpleNamespace(env=env_stub))
    monkeypatch.setitem(sys.modules, "src.config.env", env_stub)
    monkeypatch.setitem(
        sys.modules, "src.utils.log", types.SimpleNamespace(logger=_silent_logger())
    )
    monkeypatch.setitem(
        sys.modules,
        "src.utils.error_interceptor",
        types.SimpleNamespace(interceptor=lambda *_a, **_k: lambda func: func),
    )
    monkeypatch.setitem(
        sys.modules,
        "src.tools.google_search.gemini_service",
        types.SimpleNamespace(
            gemini_service=types.SimpleNamespace(google_search=fake_google_search)
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "src.utils.bigquery",
        types.SimpleNamespace(
            save_response_in_bq_background=fake_save_response_in_bq_background
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "src.utils.typesense_api",
        types.SimpleNamespace(
            HubSearchRequest=FakeHubSearchRequest, hub_search=fake_hub_search
        ),
    )

    module = _load_module("test_search_retry_module", SEARCH_MODULE_PATH)

    created_tasks = []
    monkeypatch.setattr(
        module.asyncio, "create_task", lambda coro: created_tasks.append(coro)
    )

    def run(query="iptu"):
        result = asyncio.run(module.get_google_search(query))
        for coro in created_tasks:
            coro.close()
        created_tasks.clear()
        return result

    state.module = module
    state.run = run
    return state


@pytest.mark.parametrize(
    "exc",
    [
        pytest.param(
            httpx.HTTPStatusError(
                "502 Bad Gateway",
                request=httpx.Request("GET", "http://typesense.local/search"),
                response=httpx.Response(502),
            ),
            id="http_status_error",
        ),
        pytest.param(httpx.ReadTimeout("read timeout"), id="timeout"),
        pytest.param(httpx.ConnectError("connection refused"), id="network_error"),
        pytest.param(httpx.RemoteProtocolError("resposta truncada"), id="protocol"),
        pytest.param(httpx.TooManyRedirects("loop de redirect"), id="redirects"),
        pytest.param(
            json.JSONDecodeError("Expecting value", "<html>502</html>", 0),
            id="corpo_nao_json",
        ),
        pytest.param(httpx.InvalidURL("url malformada"), id="url_malformada"),
        pytest.param(RuntimeError("caso escuso"), id="inesperado"),
    ],
)
def test_falha_do_typesense_cai_no_google(search, monkeypatch, exc):
    """Antes do CHATR-122, a exceção do Typesense matava a tool antes do fallback.

    Cada caso exercita um ramo distinto de `_try_hub_search`, na ordem da hierarquia
    do httpx. `InvalidURL` está fora da hierarquia de `HTTPError` e `JSONDecodeError`
    deriva de `ValueError` — ambos precisam de ramo próprio para não escapar. Todos
    devem degradar para o Google em vez de propagar.
    """

    async def exploding_hub_search(request):
        raise exc

    monkeypatch.setattr(search.module, "hub_search", exploding_hub_search)

    result = search.run()

    assert result["text"] == "resposta do google"
    assert result["id"] == "abc"


def test_hierarquia_de_excecoes_do_httpx_nao_regrediu():
    """A ordem dos ramos em `_try_hub_search` depende destas relações.

    Se uma versão futura do httpx reorganizar a hierarquia, um ramo mais genérico
    passaria na frente e engoliria o específico — sem quebrar nenhum outro teste.
    """
    assert issubclass(httpx.TimeoutException, httpx.TransportError)
    assert issubclass(httpx.TransportError, httpx.RequestError)
    assert issubclass(httpx.ConnectError, httpx.RequestError)
    assert issubclass(httpx.TooManyRedirects, httpx.RequestError)
    # Irmão de RequestError, não ancestral: precisa de ramo separado.
    assert not issubclass(httpx.HTTPStatusError, httpx.RequestError)
    # Fora da hierarquia de HTTPError: só o `except Exception` final o captura.
    assert not issubclass(httpx.InvalidURL, httpx.HTTPError)
    assert issubclass(json.JSONDecodeError, ValueError)


def test_falha_do_gemini_propaga_success_e_error(search, monkeypatch):
    """O agente precisa distinguir 'resultado de busca' de 'falha tratada'."""

    async def empty_hub_search(request):
        return {"results": [], "results_clean": []}

    monkeypatch.setattr(search.module, "hub_search", empty_hub_search)
    search.google_response = {
        "text": "mensagem tratada",
        "sources": [],
        "web_search_queries": [],
        "id": "err-1",
        "success": False,
        "error": {"kind": "gemini_unavailable", "code": 503, "attempts": 4},
    }

    result = search.run()

    assert result["success"] is False
    assert result["error"]["kind"] == "gemini_unavailable"


def test_resultado_do_typesense_nao_ganha_campo_de_erro(search, monkeypatch):
    async def hub_search_with_results(request):
        return {"results": [{"id": 1}], "results_clean": [{"title": "A"}]}

    monkeypatch.setattr(search.module, "hub_search", hub_search_with_results)

    result = search.run()

    assert result == {"response": [{"title": "A"}]}
