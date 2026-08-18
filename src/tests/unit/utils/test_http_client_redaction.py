"""
Testes de redação de credenciais antes do envio ao interceptor de erros.

Várias APIs integradas autenticam por query string (IPTU) ou por campo de body
(Dívida Ativa). Exceções do httpx embutem a URL completa na mensagem e no traceback
(`HTTPStatusError` é a mais comum), então a credencial só não chega ao monitoramento
porque é redigida aqui.

A primeira metade do arquivo cobre as funções de redação isoladamente; a segunda
exercita o `InterceptedHTTPClient` real sobre `httpx.MockTransport`.
"""

import httpx
import pytest

import src.utils.http_client as http_client_mod
from src.utils.http_client import (
    DEFAULT_ERROR_STATUS_CODES,
    InterceptedHTTPClient,
    redact_body,
    redact_text,
)


def test_redact_text_remove_token_de_query_string():
    sujo = (
        "Exceeded maximum allowed redirects for url "
        "'https://iptu.example/ConsultarGuias?token=SEGREDO&inscricao=123'"
    )
    limpo = redact_text(sujo)

    assert "SEGREDO" not in limpo
    assert "token=<redacted>" in limpo
    # O que é útil para diagnóstico continua legível
    assert "inscricao=123" in limpo
    assert "Exceeded maximum allowed redirects" in limpo


@pytest.mark.parametrize(
    "chave",
    ["token", "TOKEN", "api_key", "apikey", "password", "secret", "ChaveAcesso"],
)
def test_redact_text_cobre_variacoes_de_chave(chave):
    assert "SEGREDO" not in redact_text(f"https://ex.com/a?{chave}=SEGREDO&x=1")


def test_redact_text_preserva_texto_sem_credencial():
    assert redact_text("falha de conexão") == "falha de conexão"
    assert redact_text("") == ""
    assert redact_text(None) is None


def test_redact_body_remove_campos_sensiveis():
    body = {
        "grant_type": "password",
        "Consumidor": "consultar-dividas-contribuinte",
        "ChaveAcesso": "CHAVE-SECRETA",
        "inscricaoImobiliaria": "12345678",
    }
    limpo = redact_body(body)

    assert limpo["ChaveAcesso"] == "<redacted>"
    assert limpo["inscricaoImobiliaria"] == "12345678"
    assert limpo["Consumidor"] == "consultar-dividas-contribuinte"


def test_redact_body_e_recursivo():
    limpo = redact_body({"dados": [{"token": "SEGREDO", "id": "1"}]})
    assert limpo["dados"][0]["token"] == "<redacted>"
    assert limpo["dados"][0]["id"] == "1"


def test_redact_body_preserva_tipos_nao_estruturados():
    assert redact_body(None) is None
    assert redact_body(42) == 42
    assert (
        redact_body("https://ex.com/a?token=X") == "https://ex.com/a?token=<redacted>"
    )


@pytest.mark.asyncio
async def test_interceptor_recebe_payload_redigido(monkeypatch):
    """Ponto de estrangulamento: nada sai daqui com credencial em claro."""
    capturado = {}

    async def fake_send_api_error(**kwargs):
        capturado.update(kwargs)
        return True

    # setattr no objeto do módulo: outros testes da suíte substituem "src" em
    # sys.modules por um stub, o que quebra o monkeypatch por caminho em string
    monkeypatch.setattr(http_client_mod, "send_api_error", fake_send_api_error)

    client = InterceptedHTTPClient(user_id="u1", source={"source": "test"})
    await client._intercept_error_async(
        url="https://iptu.example/ConsultarGuias?token=SEGREDO",
        request_body={"inscricao": "123", "token": "SEGREDO"},
        status_code=500,
        error_message="boom at https://iptu.example/x?token=SEGREDO",
        traceback_str="File x.py, line 1: https://iptu.example/x?token=SEGREDO",
    )

    assert "SEGREDO" not in repr(capturado)
    assert capturado["request_body"]["inscricao"] == "123"


# --- InterceptedHTTPClient real sobre MockTransport ---------------------------
#
# Os testes acima verificam as funções de redação isoladamente. Estes exercitam o
# client de verdade, para travar as duas metades do contrato de uma só vez: a
# credencial PRECISA chegar na requisição de saída (a API exige) e NUNCA pode chegar
# ao interceptor.

SEGREDO = "TOKEN-SUPER-SECRETO"

FONTE_IPTU = {
    "source": "mcp",
    "tool": "multi_step_service",
    "workflow": "iptu_pagamento",
}

# Corpo devolvido pela fachada do IPTU quando a conexão dela com o backend legado cai
CORPO_RECONEXAO = "6000 - Connect required before calling other methods."


@pytest.fixture
def interceptor_spy(monkeypatch):
    """Captura tudo que o client envia ao interceptor de erros."""
    chamadas = []

    async def fake_send_api_error(**kwargs):
        chamadas.append(kwargs)
        return True

    monkeypatch.setattr(http_client_mod, "send_api_error", fake_send_api_error)
    return chamadas


def responder(status_code: int, texto: str = "", requisicoes: list | None = None):
    """Handler de MockTransport que registra a requisição e devolve uma resposta fixa."""

    def handler(request: httpx.Request) -> httpx.Response:
        if requisicoes is not None:
            requisicoes.append(request)
        return httpx.Response(status_code, text=texto)

    return handler


def cliente_iptu(handler) -> InterceptedHTTPClient:
    """Client real com o token no próprio client, como faz o serviço de IPTU."""
    return InterceptedHTTPClient(
        user_id="5521999999999",
        source=FONTE_IPTU,
        transport=httpx.MockTransport(handler),
        params={"token": SEGREDO},
    )


@pytest.mark.asyncio
async def test_mock_200_leva_o_token_e_nao_alerta(interceptor_spy):
    requisicoes = []

    async with cliente_iptu(responder(200, '{"ok": true}', requisicoes)) as client:
        response = await client.get(
            "https://iptu.example/ConsultarGuias",
            params={"inscricao": "12345678"},
            error_status_codes=DEFAULT_ERROR_STATUS_CODES,
        )

    assert response.status_code == 200
    # A API autentica por query string: o token tem mesmo que chegar lá
    assert requisicoes[0].url.params["token"] == SEGREDO
    assert requisicoes[0].url.params["inscricao"] == "12345678"
    assert interceptor_spy == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status_code, corpo",
    [
        (500, CORPO_RECONEXAO),  # CHATR-121: tentativas esgotadas
        (500, "Erro interno"),  # 500 genérico
        (401, "Unauthorized"),
    ],
)
async def test_mock_erro_de_status_reporta_sem_credencial(
    status_code, corpo, interceptor_spy
):
    requisicoes = []

    async with cliente_iptu(responder(status_code, corpo, requisicoes)) as client:
        response = await client.get(
            "https://iptu.example/ConsultarGuias",
            params={"inscricao": "12345678"},
            error_status_codes=DEFAULT_ERROR_STATUS_CODES,
        )

    assert response.status_code == status_code
    assert requisicoes[0].url.params["token"] == SEGREDO

    (reportado,) = interceptor_spy
    assert SEGREDO not in repr(reportado)
    assert reportado["status_code"] == status_code
    # O que serve para diagnóstico continua chegando ao monitoramento
    assert corpo in reportado["error_message"]
    assert reportado["request_body"]["inscricao"] == "12345678"


@pytest.mark.asyncio
async def test_mock_connect_error_nao_expoe_token_da_url(interceptor_spy):
    """Quando o caller monta a URL com o token, é `api_endpoint` que precisa ser redigido."""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("Connection refused", request=request)

    with pytest.raises(httpx.ConnectError):
        async with cliente_iptu(handler) as client:
            await client.get(
                f"https://iptu.example/ConsultarGuias?token={SEGREDO}",
                params={"inscricao": "12345678"},
            )

    (reportado,) = interceptor_spy
    assert SEGREDO not in repr(reportado)
    assert (
        reportado["api_endpoint"]
        == "https://iptu.example/ConsultarGuias?token=<redacted>"
    )
    assert reportado["status_code"] == 0
    assert "Connection error" in reportado["error_message"]


@pytest.mark.asyncio
async def test_mock_excecao_do_httpx_com_url_na_mensagem(interceptor_spy):
    """
    `HTTPStatusError` é montada pelo próprio httpx com a URL completa — e a URL
    completa carrega o token, porque a API de IPTU autentica por query string.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        # raise_for_status() monta a mensagem a partir de str(request.url)
        httpx.Response(500, request=request).raise_for_status()
        raise AssertionError("raise_for_status deveria ter levantado")

    with pytest.raises(httpx.HTTPStatusError) as exc_info:
        async with cliente_iptu(handler) as client:
            await client.get(
                "https://iptu.example/ConsultarGuias",
                params={"inscricao": "12345678"},
            )

    # O vazamento é real, não hipotético: a mensagem crua do httpx traz o token
    assert SEGREDO in str(exc_info.value)

    # ...e não passa daqui, nem pela mensagem nem pelo traceback
    (reportado,) = interceptor_spy
    assert SEGREDO not in repr(reportado)
    assert "token=<redacted>" in reportado["error_message"]
    assert SEGREDO not in reportado["traceback"]


@pytest.mark.asyncio
async def test_mock_chave_acesso_da_divida_ativa_nao_vai_no_body(interceptor_spy):
    """A Dívida Ativa autentica por campo de body, não por query string."""
    chave = "CHAVE-DE-ACESSO-SECRETA"
    requisicoes = []

    client = InterceptedHTTPClient(
        user_id="5521999999999",
        source=FONTE_IPTU,
        transport=httpx.MockTransport(responder(500, "Erro interno", requisicoes)),
    )
    async with client:
        await client.post(
            "https://divida.example/security/token",
            data={
                "grant_type": "password",
                "Consumidor": "consultar-dividas-contribuinte",
                "ChaveAcesso": chave,
            },
            error_status_codes=DEFAULT_ERROR_STATUS_CODES,
        )

    # A credencial tem que chegar na API
    assert chave in requisicoes[0].content.decode()

    (reportado,) = interceptor_spy
    assert chave not in repr(reportado)
    assert reportado["request_body"]["ChaveAcesso"] == "<redacted>"
    assert reportado["request_body"]["Consumidor"] == "consultar-dividas-contribuinte"
