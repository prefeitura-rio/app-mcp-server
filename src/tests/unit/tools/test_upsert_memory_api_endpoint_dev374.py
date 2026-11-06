"""
Testes da DEV-374: o `upsert_memory` não manda o `user_id` em claro ao error
interceptor quando a API de memória (RMI) falha.

Antes, a URL `/v1/memory/{user_id}` ia no `api_endpoint` do report do
`InterceptedHTTPClient` (só com a redação por padrão), e a mensagem do
`raise_for_status` do PUT/POST, com a mesma URL, ia como `error_message` no
report do `@interceptor`, sem redação. No POST que segue o 404 do PUT, o
traceback do erro do POST ainda levava a exceção do PUT como contexto.

Invariantes cobertos aqui:
- com a API fora (timeout ou erro de conexão), no PUT e no POST que segue um
  404, a requisição à API leva o `user_id` no caminho, mas nenhum payload ao
  interceptor o leva, e o `api_endpoint` do client é o rótulo
  `/v1/memory/{user_id}`;
- com a API respondendo erro HTTP (4xx/5xx, exceto o 404 do PUT e os 409), a
  exceção e o report do `@interceptor` levam o rótulo, nunca a URL real;
- 404 seguido de POST, os 409 e o sucesso continuam como antes, sem report.
"""

import json

import httpx
import pytest

from src.tools import memory
from src.utils import error_interceptor
from src.utils.http_client import InterceptedHTTPClient

# Capturado no import, antes do fixture autouse `block_real_error_interceptor`
# (src/tests/unit/conftest.py) trocar o atributo por um mock: os testes precisam
# do payload real que sairia do processo.
_real_send_error_to_interceptor = error_interceptor.send_error_to_interceptor

# Dados sintéticos. O `user_id` não tem formato de telefone, para que nenhuma
# redação por padrão o apague e o teste dependa só do rótulo.
USER_ID = "usuario-sintetico-374"
RMI_URL = "https://rmi.example"
ENDPOINT = f"{RMI_URL}/v1/memory/{{user_id}}"
URL_REAL = f"{RMI_URL}/v1/memory/{USER_ID}"
MEMORY_BANK = {
    "memory_name": "preferencia_sintetica",
    "description": "descricao sintetica",
    "relevance": "low",
    "memory_type": "appended",
    "value": "valor sintetico",
}


class _CapturingTransport(httpx.AsyncBaseTransport):
    def __init__(self):
        self.requests = []

    async def handle_async_request(self, request):
        self.requests.append(request)
        return httpx.Response(200, json={"status": "ok"})


@pytest.fixture
def interceptor_externo(monkeypatch):
    """Envio real ao interceptor, com o HTTP dele capturado em memória.

    Só o client sem `transport` (o do `send_error_to_interceptor`) cai no
    transporte de captura; o da tool usa o `MockTransport` que o teste passa.
    """
    capturing = _CapturingTransport()
    monkeypatch.setattr(
        error_interceptor.env,
        "ERROR_INTERCEPTOR_URL",
        "https://test.interceptor.local/api",
    )
    monkeypatch.setattr(error_interceptor.env, "ERROR_INTERCEPTOR_TOKEN", "test-token")
    monkeypatch.setattr(
        error_interceptor,
        "send_error_to_interceptor",
        _real_send_error_to_interceptor,
    )

    # Subclasse, não função: o `InterceptedHTTPClient` faz `isinstance` com
    # `httpx.AsyncClient`, que é o mesmo módulo trocado aqui.
    class _AsyncClientDeCaptura(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs.setdefault("transport", capturing)
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(error_interceptor.httpx, "AsyncClient", _AsyncClientDeCaptura)
    return capturing


@pytest.fixture
def memoria_com_api(monkeypatch):
    """`upsert_memory` com OAuth2 configurado e a API de memória simulada.

    Devolve a função que instala o handler da API e a lista de requisições
    que chegaram a ela.
    """
    requisicoes_da_api = []

    async def authorization_header():
        return "Bearer token-de-teste"

    monkeypatch.setattr(memory, "RMI_API_URL", RMI_URL)
    monkeypatch.setattr(memory, "is_oauth2_configured", lambda: True)
    monkeypatch.setattr(memory, "get_authorization_header", authorization_header)

    def instalar(handler):
        def handler_registrado(request: httpx.Request) -> httpx.Response:
            requisicoes_da_api.append(request)
            return handler(request)

        real_client = InterceptedHTTPClient

        def client_com_transport(**kwargs):
            return real_client(
                transport=httpx.MockTransport(handler_registrado), **kwargs
            )

        monkeypatch.setattr(memory, "InterceptedHTTPClient", client_com_transport)

    return instalar, requisicoes_da_api


def _payloads_sem_user_id(transport: _CapturingTransport) -> list:
    payloads = [json.loads(r.content) for r in transport.requests]
    assert payloads, "a falha deveria ter sido reportada"
    for payload in payloads:
        assert USER_ID not in json.dumps(payload, ensure_ascii=False)
    return payloads


def _report_do_client(payloads: list) -> dict:
    reports = [p for p in payloads if p["api_endpoint"].startswith("http")]
    assert len(reports) == 1
    return reports[0]


def _report_do_decorator(payloads: list) -> dict:
    reports = [p for p in payloads if p["api_endpoint"].startswith("internal://")]
    assert len(reports) == 1
    return reports[0]


def _falha(excecao):
    def handler(request: httpx.Request) -> httpx.Response:
        raise excecao("falha simulada", request=request)

    return handler


def _put_404_e_post(handler_do_post):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            return httpx.Response(404, json={"detail": "not found"})
        return handler_do_post(request)

    return handler


@pytest.mark.asyncio
@pytest.mark.parametrize("excecao", [httpx.ReadTimeout, httpx.ConnectError])
async def test_put_com_api_fora_nao_envia_user_id(
    interceptor_externo, memoria_com_api, excecao
):
    instalar, requisicoes_da_api = memoria_com_api
    instalar(_falha(excecao))

    with pytest.raises(excecao):
        await memory.upsert_memory(USER_ID, dict(MEMORY_BANK))

    # A requisição à API não muda: o user_id vai no caminho...
    assert [(r.method, str(r.url)) for r in requisicoes_da_api] == [("PUT", URL_REAL)]

    # ...e nenhum report ao interceptor o leva.
    payloads = _payloads_sem_user_id(interceptor_externo)
    assert _report_do_client(payloads)["api_endpoint"] == ENDPOINT
    _report_do_decorator(payloads)


@pytest.mark.asyncio
@pytest.mark.parametrize("excecao", [httpx.ReadTimeout, httpx.ConnectError])
async def test_post_apos_404_com_api_fora_nao_envia_user_id(
    interceptor_externo, memoria_com_api, excecao
):
    """O traceback do erro do POST não leva o erro do PUT (com a URL)."""
    instalar, requisicoes_da_api = memoria_com_api
    instalar(_put_404_e_post(_falha(excecao)))

    with pytest.raises(excecao) as erro:
        await memory.upsert_memory(user_id=USER_ID, memory_bank=dict(MEMORY_BANK))

    assert erro.value.__context__ is None
    assert [(r.method, str(r.url)) for r in requisicoes_da_api] == [
        ("PUT", URL_REAL),
        ("POST", URL_REAL),
    ]
    payloads = _payloads_sem_user_id(interceptor_externo)
    report = _report_do_client(payloads)
    assert report["api_endpoint"] == ENDPOINT
    assert "HTTPStatusError" not in report["error_response"]
    assert "HTTPStatusError" not in _report_do_decorator(payloads)["error_response"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [400, 401, 403, 500, 502])
async def test_put_com_erro_http_nao_envia_user_id(
    interceptor_externo, memoria_com_api, status_code
):
    """A mensagem do `raise_for_status` traz a URL; o decorator a reporta."""
    instalar, requisicoes_da_api = memoria_com_api
    instalar(lambda request: httpx.Response(status_code, text="erro da API"))

    with pytest.raises(httpx.HTTPStatusError) as erro:
        await memory.upsert_memory(USER_ID, dict(MEMORY_BANK))

    assert erro.value.response.status_code == status_code
    assert str(erro.value) == (
        f"HTTP {status_code} da API de memória em PUT {ENDPOINT}"
    )
    assert erro.value.__cause__ is None
    assert erro.value.__suppress_context__
    assert [r.method for r in requisicoes_da_api] == ["PUT"]
    report = _report_do_decorator(_payloads_sem_user_id(interceptor_externo))
    assert f"PUT {ENDPOINT}" in report["error_response"]


@pytest.mark.asyncio
@pytest.mark.parametrize("status_code", [400, 401, 403, 500, 502])
async def test_post_apos_404_com_erro_http_nao_envia_user_id(
    interceptor_externo, memoria_com_api, status_code
):
    instalar, requisicoes_da_api = memoria_com_api
    instalar(_put_404_e_post(lambda request: httpx.Response(status_code, text="erro")))

    with pytest.raises(httpx.HTTPStatusError) as erro:
        await memory.upsert_memory(USER_ID, dict(MEMORY_BANK))

    assert erro.value.response.status_code == status_code
    assert str(erro.value) == (
        f"HTTP {status_code} da API de memória em POST {ENDPOINT}"
    )
    assert [r.method for r in requisicoes_da_api] == ["PUT", "POST"]
    report = _report_do_decorator(_payloads_sem_user_id(interceptor_externo))
    assert f"POST {ENDPOINT}" in report["error_response"]
    assert "HTTP 404" not in report["error_response"]


@pytest.mark.asyncio
async def test_404_no_put_cria_com_post(interceptor_externo, memoria_com_api):
    instalar, requisicoes_da_api = memoria_com_api
    instalar(
        _put_404_e_post(lambda request: httpx.Response(201, json={"status": "ok"}))
    )

    assert await memory.upsert_memory(USER_ID, dict(MEMORY_BANK)) == {"status": "ok"}
    assert [r.method for r in requisicoes_da_api] == ["PUT", "POST"]
    assert json.loads(requisicoes_da_api[1].content) == MEMORY_BANK
    assert interceptor_externo.requests == []


@pytest.mark.asyncio
async def test_409_no_put_e_no_post_continuam_skipped(
    interceptor_externo, memoria_com_api
):
    instalar, requisicoes_da_api = memoria_com_api

    instalar(lambda request: httpx.Response(409, json={"detail": "conflict"}))
    assert await memory.upsert_memory(USER_ID, dict(MEMORY_BANK)) == {
        "status": "skipped",
        "reason": "conflict",
    }

    instalar(_put_404_e_post(lambda request: httpx.Response(409, json={})))
    assert await memory.upsert_memory(USER_ID, dict(MEMORY_BANK)) == {
        "status": "skipped",
        "reason": "conflict",
    }

    assert [r.method for r in requisicoes_da_api] == ["PUT", "PUT", "POST"]
    assert interceptor_externo.requests == []


@pytest.mark.asyncio
async def test_upsert_com_sucesso_nao_reporta(interceptor_externo, memoria_com_api):
    instalar, requisicoes_da_api = memoria_com_api
    instalar(lambda request: httpx.Response(200, json={"status": "ok"}))

    assert await memory.upsert_memory(USER_ID, dict(MEMORY_BANK)) == {"status": "ok"}
    assert [(r.method, str(r.url)) for r in requisicoes_da_api] == [("PUT", URL_REAL)]
    assert interceptor_externo.requests == []
