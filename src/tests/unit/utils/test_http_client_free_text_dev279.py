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
"""

import asyncio
import json

import httpx
import pytest

import src.utils.http_client as http_client_mod
from src.utils.http_client import (
    DEFAULT_ERROR_STATUS_CODES,
    InterceptedHTTPClient,
    summarize_free_text,
)
from src.utils.pii import resumir_texto_livre

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
