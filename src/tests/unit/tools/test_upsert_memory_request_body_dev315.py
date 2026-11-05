"""
Testes da DEV-315: o `upsert_memory` não manda o banco de memórias do cidadão
no `request_body` que o `InterceptedHTTPClient` reporta ao error interceptor.

A DEV-280 tirou o texto do `input_body` do `@interceptor`, mas o client da
chamada à API de memória (RMI) reportava o próprio corpo da requisição em
timeout e erro de conexão: o `memory_bank` validado ia inteiro (descrição e
valor em texto livre), só com a redação por padrão.

Invariantes cobertos aqui:
- com a API de memória fora (timeout ou erro de conexão), no PUT e no POST que
  segue um 404, o texto chega à API, mas nenhum payload ao interceptor o leva;
- o `request_body` reportado pelo client leva só os nomes das chaves e, de cada
  valor, o tamanho e os tipos de dado pessoal (`resumir_texto_livre`);
- todos os campos do `MemoryBank` estão declarados como texto livre no client.
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

# Dados sintéticos. O CPF é válido no dígito verificador para a redação casar;
# o contexto de saúde é o que nenhuma redação por padrão apaga.
CPF_SINTETICO = "529.982.247-25"
USER_ID = "usuario-sintetico-315"
MEMORIA_NOME = "saude_familia_gestante"
MEMORIA_VALOR = (
    f"cidada gestante com diabetes, mora com a avo acamada, cpf {CPF_SINTETICO}"
)
MEMORIA_DESCRICAO = "condicao de saude informada na conversa"
MEMORY_BANK = {
    "memory_name": MEMORIA_NOME,
    "description": MEMORIA_DESCRICAO,
    "relevance": "high",
    "memory_type": "base",
    "value": MEMORIA_VALOR,
}
TRECHOS_PROIBIDOS = (
    "gestante",
    "diabetes",
    "acamada",
    "condicao de saude",
    "conversa",
    CPF_SINTETICO,
)
RESUMO_DO_CORPO = {
    "memory_name": resumir_texto_livre(MEMORIA_NOME),
    "description": resumir_texto_livre(MEMORIA_DESCRICAO),
    "relevance": resumir_texto_livre("high"),
    "memory_type": resumir_texto_livre("base"),
    "value": resumir_texto_livre(MEMORIA_VALOR),
}
RMI_URL = "https://rmi.example"


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


def _sem_texto_do_cidadao(payload: dict) -> None:
    corpo = json.dumps(payload, ensure_ascii=False)
    for trecho in TRECHOS_PROIBIDOS:
        assert trecho not in corpo


def _reports_do_client(transport: _CapturingTransport) -> list:
    payloads = [json.loads(r.content) for r in transport.requests]
    assert payloads, "a falha deveria ter sido reportada"
    for payload in payloads:
        _sem_texto_do_cidadao(payload)
    return [p for p in payloads if p.get("api_endpoint", "").startswith("http")]


@pytest.mark.asyncio
@pytest.mark.parametrize("excecao", [httpx.ReadTimeout, httpx.ConnectError])
async def test_put_com_api_fora_nao_envia_o_banco_de_memorias(
    interceptor_externo, memoria_com_api, excecao
):
    instalar, requisicoes_da_api = memoria_com_api

    def handler(request: httpx.Request) -> httpx.Response:
        raise excecao("falha simulada", request=request)

    instalar(handler)

    with pytest.raises(excecao):
        await memory.upsert_memory(USER_ID, dict(MEMORY_BANK))

    # O banco de memórias chegou à API...
    assert [r.method for r in requisicoes_da_api] == ["PUT"]
    assert json.loads(requisicoes_da_api[0].content) == MEMORY_BANK

    # ...e o client reportou só nomes e tamanhos.
    reports = _reports_do_client(interceptor_externo)
    assert len(reports) == 1
    request_body = json.loads(reports[0]["input_body"])
    assert request_body == RESUMO_DO_CORPO
    assert "cpf" in request_body["value"]["pii_redigida"]


@pytest.mark.asyncio
@pytest.mark.parametrize("excecao", [httpx.ReadTimeout, httpx.ConnectError])
async def test_post_apos_404_com_api_fora_nao_envia_o_banco_de_memorias(
    interceptor_externo, memoria_com_api, excecao
):
    """PUT responde 404 (memória ainda não existe) e o POST que cria falha."""
    instalar, requisicoes_da_api = memoria_com_api

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PUT":
            return httpx.Response(404, json={"detail": "not found"})
        raise excecao("falha simulada", request=request)

    instalar(handler)

    with pytest.raises(excecao):
        await memory.upsert_memory(user_id=USER_ID, memory_bank=dict(MEMORY_BANK))

    assert [r.method for r in requisicoes_da_api] == ["PUT", "POST"]
    assert json.loads(requisicoes_da_api[1].content) == MEMORY_BANK

    reports = _reports_do_client(interceptor_externo)
    assert len(reports) == 1
    assert json.loads(reports[0]["input_body"]) == RESUMO_DO_CORPO


@pytest.mark.asyncio
async def test_upsert_com_sucesso_continua_enviando_o_corpo_inteiro(
    interceptor_externo, memoria_com_api
):
    instalar, requisicoes_da_api = memoria_com_api
    instalar(lambda request: httpx.Response(200, json={"status": "ok"}))

    resultado = await memory.upsert_memory(USER_ID, dict(MEMORY_BANK))

    assert resultado == {"status": "ok"}
    assert json.loads(requisicoes_da_api[0].content) == MEMORY_BANK
    assert interceptor_externo.requests == []


def test_todos_os_campos_do_memory_bank_sao_texto_livre_no_client():
    assert set(memory._CAMPOS_DO_MEMORY_BANK) == set(memory.MemoryBank.model_fields)
