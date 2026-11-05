"""
Testes da DEV-279: o `InterceptedHTTPClient` não envia o texto livre do cidadão
no `request_body` reportado ao error interceptor externo.

Antes, o corpo da requisição ia ao interceptor só com `redact_body`, que tira
credenciais, CPF e telefone, mas não o contexto que o cidadão escreve (doença,
família, dívida). A DEV-278 corrigiu o `input_body` do `@interceptor`; o
`request_body` do client continuava levando a pergunta inteira em timeout, erro
de conexão e status interceptado.

Invariantes cobertos aqui:
- `free_text_fields` troca o campo por `resumir_texto_livre` (tamanho e tipos
  de dado pessoal) no report, em modo async e sync, em qualquer nível do corpo;
  a requisição de saída continua levando o texto (a API precisa dele).
- O resto do corpo segue com a redação de sempre (credencial redigida, campo
  comum com o valor).
- Sem `free_text_fields`, o report continua igual ao de antes.
- `dharma_search` e `surkai_search` reais, com a API fora, não mandam a
  pergunta em nenhum payload que sai do processo.
"""

import asyncio
import json

import httpx
import pytest

import src.utils.http_client as http_client_mod
from src.tools import dharma_search, web_search_surkai
from src.utils import error_interceptor
from src.utils.http_client import (
    DEFAULT_ERROR_STATUS_CODES,
    InterceptedHTTPClient,
    summarize_free_text,
)
from src.utils.pii import resumir_texto_livre

# Capturado no import, antes do fixture autouse `block_real_error_interceptor`
# (src/tests/unit/conftest.py) substituir o atributo por um mock: o teste das
# tools precisa do payload real que sairia do processo.
_real_send_error_to_interceptor = error_interceptor.send_error_to_interceptor

# Dados sintéticos. O CPF é válido no dígito verificador para a redação casar;
# o contexto de saúde é o que nenhuma redação por padrão apaga.
CPF_SINTETICO = "529.982.247-25"
PERGUNTA = (
    f"meu filho tem leucemia e preciso do remedio de alto custo, cpf {CPF_SINTETICO}"
)
TRECHOS_PROIBIDOS = ("leucemia", "remedio", "alto custo", CPF_SINTETICO)
SEGREDO = "CHAVE-SECRETA-DE-TESTE"
FONTE = {"source": "mcp", "tool": "teste"}
URL = "https://busca.example/v1/chats"


def _sem_texto_da_pergunta(obj) -> None:
    corpo = json.dumps(obj, ensure_ascii=False, default=str)
    for trecho in TRECHOS_PROIBIDOS:
        assert trecho not in corpo


@pytest.fixture
def interceptor_spy(monkeypatch):
    """Captura tudo que o client envia ao interceptor de erros."""
    chamadas = []

    async def fake_send_api_error(**kwargs):
        chamadas.append(kwargs)
        return True

    monkeypatch.setattr(http_client_mod, "send_api_error", fake_send_api_error)
    return chamadas


def _levanta(excecao):
    def handler(request: httpx.Request) -> httpx.Response:
        raise excecao("falha simulada", request=request)

    return handler


def _registra(status_code: int, requisicoes: list):
    def handler(request: httpx.Request) -> httpx.Response:
        requisicoes.append(request)
        return httpx.Response(status_code, text="erro da API")

    return handler


# ---------------------------------------------------------------------------
# summarize_free_text
# ---------------------------------------------------------------------------


def test_summarize_troca_so_os_campos_declarados_em_qualquer_nivel():
    corpo = {
        "Message": PERGUNTA,
        "k": 6,
        "lang": "pt-BR",
        "historico": [{"message": PERGUNTA, "papel": "cidadao"}],
    }

    resumido = summarize_free_text(corpo, frozenset({"message"}))

    assert resumido == {
        "Message": resumir_texto_livre(PERGUNTA),
        "k": 6,
        "lang": "pt-BR",
        "historico": [{"message": resumir_texto_livre(PERGUNTA), "papel": "cidadao"}],
    }
    _sem_texto_da_pergunta(resumido)
    # O corpo original não é alterado: é o mesmo objeto enviado à API.
    assert corpo["Message"] == PERGUNTA


def test_summarize_sem_campos_devolve_o_corpo_como_esta():
    corpo = {"message": PERGUNTA}
    assert summarize_free_text(corpo, frozenset()) is corpo
    assert summarize_free_text(None, frozenset({"message"})) is None


def test_summarize_corpo_cru_com_campos_declarados_vira_resumo():
    """Texto ou bytes crus não têm onde achar o campo: vai o resumo do todo."""
    assert summarize_free_text(PERGUNTA, frozenset({"message"})) == (
        resumir_texto_livre(PERGUNTA)
    )
    assert summarize_free_text(PERGUNTA.encode(), frozenset({"message"})) == {
        "tipo": "bytes"
    }


@pytest.mark.parametrize(
    "params",
    [
        [("q", PERGUNTA), ("limite", "5")],
        (("q", PERGUNTA), ("limite", "5")),
        httpx.QueryParams([("q", PERGUNTA), ("limite", "5")]),
    ],
)
def test_summarize_params_em_pares_ou_query_params(params):
    """`params` do httpx também aceita lista de pares e `QueryParams`."""
    resumido = summarize_free_text(params, frozenset({"q"}))

    assert resumido == [["q", resumir_texto_livre(PERGUNTA)], ["limite", "5"]]
    _sem_texto_da_pergunta(resumido)


def test_summarize_campo_que_nao_e_texto_vira_o_tipo():
    assert summarize_free_text(
        {"message": ["lista", "de", "texto"]}, frozenset({"message"})
    ) == {"message": {"tipo": "list"}}


def test_free_text_fields_como_string_e_um_nome():
    """Uma string sozinha é um nome, não o conjunto das letras dela."""
    client = InterceptedHTTPClient(
        user_id="u1", source=FONTE, free_text_fields="Message"
    )
    assert client.free_text_fields == frozenset({"message"})


# ---------------------------------------------------------------------------
# InterceptedHTTPClient real sobre MockTransport
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("excecao", "status_code"),
    [(httpx.ReadTimeout, 408), (httpx.ConnectError, 0), (httpx.ReadError, 0)],
)
async def test_async_falha_de_transporte_reporta_so_o_resumo(
    interceptor_spy, excecao, status_code
):
    client = InterceptedHTTPClient(
        user_id="u1",
        source=FONTE,
        free_text_fields=("message",),
        transport=httpx.MockTransport(_levanta(excecao)),
    )

    with pytest.raises(excecao):
        async with client:
            await client.post(
                URL, json={"message": PERGUNTA, "api_key": SEGREDO, "k": 6}
            )

    (reportado,) = interceptor_spy
    assert reportado["status_code"] == status_code
    assert reportado["request_body"] == {
        "message": resumir_texto_livre(PERGUNTA),
        "api_key": "<redacted>",
        "k": 6,
    }
    _sem_texto_da_pergunta(reportado)
    assert SEGREDO not in repr(reportado)


@pytest.mark.asyncio
async def test_async_status_interceptado_reporta_so_o_resumo_e_envia_o_texto(
    interceptor_spy,
):
    requisicoes = []
    client = InterceptedHTTPClient(
        user_id="u1",
        source=FONTE,
        free_text_fields=("query",),
        transport=httpx.MockTransport(_registra(500, requisicoes)),
    )

    async with client:
        response = await client.post(
            URL,
            json={"query": PERGUNTA, "k": 6, "lang": "pt-BR"},
            error_status_codes=DEFAULT_ERROR_STATUS_CODES,
        )

    assert response.status_code == 500
    # A API precisa da pergunta: a requisição de saída não muda.
    assert json.loads(requisicoes[0].content)["query"] == PERGUNTA

    (reportado,) = interceptor_spy
    assert reportado["request_body"] == {
        "query": resumir_texto_livre(PERGUNTA),
        "k": 6,
        "lang": "pt-BR",
    }
    _sem_texto_da_pergunta(reportado)


@pytest.mark.asyncio
async def test_async_params_de_get_tambem_sao_resumidos(interceptor_spy):
    client = InterceptedHTTPClient(
        user_id="u1",
        source=FONTE,
        free_text_fields=("q",),
        transport=httpx.MockTransport(_levanta(httpx.ConnectError)),
    )

    with pytest.raises(httpx.ConnectError):
        async with client:
            await client.get(URL, params={"q": PERGUNTA, "limite": "5"})

    (reportado,) = interceptor_spy
    assert reportado["request_body"] == {
        "q": resumir_texto_livre(PERGUNTA),
        "limite": "5",
    }
    _sem_texto_da_pergunta(reportado)


@pytest.mark.asyncio
async def test_async_params_em_pares_tambem_sao_resumidos(interceptor_spy):
    client = InterceptedHTTPClient(
        user_id="u1",
        source=FONTE,
        free_text_fields=("q",),
        transport=httpx.MockTransport(_levanta(httpx.ConnectError)),
    )

    with pytest.raises(httpx.ConnectError):
        async with client:
            await client.get(URL, params=[("q", PERGUNTA), ("limite", "5")])

    (reportado,) = interceptor_spy
    assert reportado["request_body"] == [
        ["q", resumir_texto_livre(PERGUNTA)],
        ["limite", "5"],
    ]
    _sem_texto_da_pergunta(reportado)


@pytest.mark.asyncio
async def test_sem_free_text_fields_o_report_continua_igual(interceptor_spy):
    """As demais chamadas não mudam: corpo com a redação de sempre."""
    client = InterceptedHTTPClient(
        user_id="u1",
        source=FONTE,
        transport=httpx.MockTransport(_levanta(httpx.ConnectError)),
    )

    with pytest.raises(httpx.ConnectError):
        async with client:
            await client.post(URL, json={"inscricao": "12345678", "token": SEGREDO})

    (reportado,) = interceptor_spy
    assert reportado["request_body"] == {
        "inscricao": "12345678",
        "token": "<redacted>",
    }


@pytest.mark.asyncio
async def test_sync_com_loop_rodando_reporta_so_o_resumo(interceptor_spy):
    client = InterceptedHTTPClient(
        user_id="u1",
        source=FONTE,
        sync=True,
        free_text_fields=("message",),
        transport=httpx.MockTransport(_levanta(httpx.ConnectError)),
    )

    with pytest.raises(httpx.ConnectError):
        with client:
            client.post_sync(URL, json={"message": PERGUNTA})

    # Com loop rodando, o report sai numa task: deixa ela executar.
    await asyncio.sleep(0)

    (reportado,) = interceptor_spy
    assert reportado["request_body"] == {"message": resumir_texto_livre(PERGUNTA)}
    _sem_texto_da_pergunta(reportado)


def test_sync_sem_loop_reporta_so_o_resumo(interceptor_spy):
    requisicoes = []
    client = InterceptedHTTPClient(
        user_id="u1",
        source=FONTE,
        sync=True,
        free_text_fields=("message",),
        transport=httpx.MockTransport(_registra(503, requisicoes)),
    )

    with client:
        response = client.post_sync(
            URL,
            json={"message": PERGUNTA},
            error_status_codes=DEFAULT_ERROR_STATUS_CODES,
        )

    assert response.status_code == 503
    assert json.loads(requisicoes[0].content)["message"] == PERGUNTA
    (reportado,) = interceptor_spy
    assert reportado["request_body"] == {"message": resumir_texto_livre(PERGUNTA)}
    _sem_texto_da_pergunta(reportado)


# ---------------------------------------------------------------------------
# Tools de busca reais, payload real que sai do processo
# ---------------------------------------------------------------------------


class _CapturingTransport(httpx.AsyncBaseTransport):
    """Faz o papel do interceptor externo: guarda o que chegaria nele."""

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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("module", "funcao", "campo"),
    [
        (dharma_search, "dharma_search", "message"),
        (web_search_surkai, "surkai_search", "query"),
    ],
)
@pytest.mark.parametrize("excecao", [httpx.ReadTimeout, httpx.ConnectError])
async def test_buscas_externas_fora_nao_enviam_a_pergunta(
    interceptor_externo, monkeypatch, module, funcao, campo, excecao
):
    requisicoes_da_tool = []

    def handler(request: httpx.Request) -> httpx.Response:
        requisicoes_da_tool.append(request)
        raise excecao("falha simulada", request=request)

    real_client = InterceptedHTTPClient

    def client_com_transport(**kwargs):
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(module, "InterceptedHTTPClient", client_com_transport)

    with pytest.raises(excecao):
        await getattr(module, funcao)(PERGUNTA)

    # A pergunta chegou à API de busca...
    assert json.loads(requisicoes_da_tool[0].content)[campo] == PERGUNTA

    # ...e não sai em nenhum payload ao interceptor.
    payloads = [json.loads(r.content) for r in interceptor_externo.requests]
    assert payloads, "a falha deveria ter sido reportada"
    for payload in payloads:
        _sem_texto_da_pergunta(payload)

    reports_do_client = [
        p for p in payloads if p.get("api_endpoint", "").startswith("http")
    ]
    assert len(reports_do_client) == 1
    request_body = json.loads(reports_do_client[0]["input_body"])
    assert request_body[campo] == resumir_texto_livre(PERGUNTA)
