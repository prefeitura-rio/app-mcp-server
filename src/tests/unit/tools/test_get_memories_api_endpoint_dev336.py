"""
Testes da DEV-336: o `get_memories` não manda o nome da memória nem o `user_id`
em claro ao error interceptor quando a API de memória (RMI) está fora.

O nome da memória é escolhido pelo agente a partir da conversa (um rótulo de
saúde ou de família, por exemplo) e é tratado como dado sensível. Antes, em
timeout ou erro de conexão, a URL `/v1/memory/{user_id}/{memory_name}` ia no
`api_endpoint` do report do `InterceptedHTTPClient`, e o `@interceptor` do
`get_memories` copiava os dois argumentos para o `input_body`, só com a redação
por padrão.

Invariantes cobertos aqui:
- com a API fora (timeout ou erro de conexão), a requisição à API leva o nome e
  o `user_id` no caminho, mas nenhum payload ao interceptor leva um nem outro;
- o `api_endpoint` do client é o rótulo `/v1/memory/{user_id}/{memory_name}`
  (ou `/v1/memory/{user_id}` sem nome), nunca a URL real;
- o `input_body` do `@interceptor` leva só o tamanho e os tipos de dado pessoal
  do nome (`resumir_texto_livre`), e não leva o `user_id`;
- `report_endpoint` do `InterceptedHTTPClient` troca só o que vai ao
  interceptor: sem ele, o `api_endpoint` segue sendo a URL redigida.
"""

import json

import httpx
import pytest

from src.tools import memory
from src.utils import error_interceptor
from src.utils.http_client import InterceptedHTTPClient
from src.utils.pii import resumir_texto_livre

# Capturado no import, antes do fixture autouse `block_real_error_interceptor`
# (src/tests/unit/conftest.py) trocar o atributo por um mock: os testes precisam
# do payload real que sairia do processo.
_real_send_error_to_interceptor = error_interceptor.send_error_to_interceptor

# Dados sintéticos. O nome da memória é o rótulo de saúde que nenhuma redação
# por padrão apaga.
USER_ID = "usuario-sintetico-336"
MEMORIA_NOME = "saude_gestante_diabetes"
RMI_URL = "https://rmi.example"
ENDPOINT_COM_NOME = f"{RMI_URL}/v1/memory/{{user_id}}/{{memory_name}}"
ENDPOINT_SEM_NOME = f"{RMI_URL}/v1/memory/{{user_id}}"
TRECHOS_PROIBIDOS = (USER_ID, MEMORIA_NOME, "gestante", "diabetes")


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
    """`get_memories` com OAuth2 configurado e a API de memória simulada.

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


def _payloads_sem_dado_do_cidadao(transport: _CapturingTransport) -> list:
    payloads = [json.loads(r.content) for r in transport.requests]
    assert payloads, "a falha deveria ter sido reportada"
    for payload in payloads:
        corpo = json.dumps(payload, ensure_ascii=False)
        for trecho in TRECHOS_PROIBIDOS:
            assert trecho not in corpo
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


@pytest.mark.asyncio
@pytest.mark.parametrize("excecao", [httpx.ReadTimeout, httpx.ConnectError])
async def test_api_fora_com_nome_nao_envia_nome_nem_user_id(
    interceptor_externo, memoria_com_api, excecao
):
    instalar, requisicoes_da_api = memoria_com_api
    instalar(_falha(excecao))

    with pytest.raises(excecao):
        await memory.get_memories(USER_ID, MEMORIA_NOME)

    # A requisição à API não muda: o nome e o user_id vão no caminho...
    assert [str(r.url) for r in requisicoes_da_api] == [
        f"{RMI_URL}/v1/memory/{USER_ID}/{MEMORIA_NOME}"
    ]

    # ...e nenhum report ao interceptor leva um nem outro.
    payloads = _payloads_sem_dado_do_cidadao(interceptor_externo)
    assert _report_do_client(payloads)["api_endpoint"] == ENDPOINT_COM_NOME
    assert json.loads(_report_do_decorator(payloads)["input_body"]) == {
        "memory_name": resumir_texto_livre(MEMORIA_NOME)
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("excecao", [httpx.ReadTimeout, httpx.ConnectError])
async def test_api_fora_com_nome_por_keyword(
    interceptor_externo, memoria_com_api, excecao
):
    instalar, _ = memoria_com_api
    instalar(_falha(excecao))

    with pytest.raises(excecao):
        await memory.get_memories(user_id=USER_ID, memory_name=MEMORIA_NOME)

    payloads = _payloads_sem_dado_do_cidadao(interceptor_externo)
    assert _report_do_client(payloads)["api_endpoint"] == ENDPOINT_COM_NOME
    assert json.loads(_report_do_decorator(payloads)["input_body"]) == {
        "memory_name": resumir_texto_livre(MEMORIA_NOME)
    }


@pytest.mark.asyncio
async def test_api_fora_sem_nome_nao_envia_user_id(
    interceptor_externo, memoria_com_api
):
    instalar, requisicoes_da_api = memoria_com_api
    instalar(_falha(httpx.ConnectError))

    with pytest.raises(httpx.ConnectError):
        await memory.get_memories(USER_ID)

    assert [str(r.url) for r in requisicoes_da_api] == [
        f"{RMI_URL}/v1/memory/{USER_ID}"
    ]
    payloads = _payloads_sem_dado_do_cidadao(interceptor_externo)
    assert _report_do_client(payloads)["api_endpoint"] == ENDPOINT_SEM_NOME
    assert json.loads(_report_do_decorator(payloads)["input_body"]) == {
        "memory_name": resumir_texto_livre(None)
    }


@pytest.mark.asyncio
async def test_get_com_sucesso_nao_reporta(interceptor_externo, memoria_com_api):
    instalar, requisicoes_da_api = memoria_com_api
    instalar(lambda request: httpx.Response(200, json={"memory_name": "x"}))

    resultado = await memory.get_memories(USER_ID, MEMORIA_NOME)

    assert resultado == {"memory_name": "x"}
    assert len(requisicoes_da_api) == 1
    assert interceptor_externo.requests == []


@pytest.mark.asyncio
async def test_client_sem_report_endpoint_reporta_a_url_redigida(
    interceptor_externo,
):
    url = "https://api.example/recurso?api_key=segredo-sintetico"

    async with InterceptedHTTPClient(
        user_id=USER_ID,
        source={"source": "mcp", "tool": "teste"},
        transport=httpx.MockTransport(_falha(httpx.ConnectError)),
    ) as client:
        with pytest.raises(httpx.ConnectError):
            await client.get(url)

    (payload,) = [json.loads(r.content) for r in interceptor_externo.requests]
    assert payload["api_endpoint"].startswith("https://api.example/recurso?api_key=")
    assert "segredo-sintetico" not in payload["api_endpoint"]


@pytest.mark.parametrize("sync", [False, True])
def test_report_endpoint_substitui_a_url_no_report(sync):
    client = InterceptedHTTPClient(
        user_id=USER_ID,
        source={"source": "mcp", "tool": "teste"},
        sync=sync,
        report_endpoint="/v1/recurso/{id}",
    )

    assert client._report_endpoint(f"{RMI_URL}/v1/recurso/{USER_ID}") == (
        "/v1/recurso/{id}"
    )


def test_sem_report_endpoint_reporta_a_url():
    client = InterceptedHTTPClient(user_id=USER_ID, source={"source": "mcp"})

    assert client._report_endpoint(f"{RMI_URL}/v1/recurso") == f"{RMI_URL}/v1/recurso"


@pytest.mark.parametrize(
    "args, kwargs, esperado",
    [
        ((USER_ID, MEMORIA_NOME), {}, resumir_texto_livre(MEMORIA_NOME)),
        ((USER_ID,), {"memory_name": MEMORIA_NOME}, resumir_texto_livre(MEMORIA_NOME)),
        ((), {"user_id": USER_ID}, resumir_texto_livre(None)),
        ((USER_ID, 42), {}, {"tipo": "int"}),
    ],
)
def test_input_body_do_get_so_leva_o_resumo_do_nome(args, kwargs, esperado):
    assert memory._input_body_do_get(args, kwargs) == {"memory_name": esperado}
