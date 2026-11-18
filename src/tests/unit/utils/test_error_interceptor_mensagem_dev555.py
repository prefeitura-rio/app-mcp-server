"""
Testes da DEV-555: `error_message` e `traceback` do report ao error interceptor
passam pela redação de PII antes de sair do processo.

Antes, só o `input_body` era redigido em `_build_interceptor_payload`; a
mensagem e o traceback iam crus em `error_response`. Qualquer exceção cujo
`str(e)` ecoasse dado do cidadão (resposta de API de terceiro, erro de
validação) levava esse dado ao interceptor externo, por qualquer um dos pontos
decorados com `@interceptor` ou que chamam `send_general_error`/`send_api_error`.

Invariantes cobertos aqui:
- `_build_interceptor_payload` redige credencial, dado de formato reconhecível
  (CPF, e-mail, telefone) e valor de chave sensível rotulada (`'nome': ...`) na
  mensagem e no traceback, e mantém o resto legível para diagnóstico;
- pelo caminho real do decorator `@interceptor`, uma exceção com CPF e nome no
  `str(e)` não leva nenhum dos dois ao payload enviado;
- `get_user_info` (poda de árvore) relança só o tipo da falha da API `/person`,
  sem a exceção original no traceback: nome sem formato nem rótulo nenhuma
  redação alcança, então ele não pode estar na mensagem.

Dados sintéticos.
"""

import json

import httpx
import pytest

from src.tools.multi_step_service.workflows.poda_de_arvore.api import api_service
from src.utils import error_interceptor

# Capturado no import, antes do fixture autouse `block_real_error_interceptor`
# (src/tests/unit/conftest.py) trocar o atributo por um mock: os testes precisam
# do payload real que sairia do processo.
_real_send_error_to_interceptor = error_interceptor.send_error_to_interceptor

CPF_PONTUADO = "123.456.789-09"
CPF_DIGITOS = "12345678909"
NOME = "Quimera Zirconio Inventado"
EMAIL = "quimera.zirconio@exemplo.com"
TRECHOS_PROIBIDOS = (
    CPF_PONTUADO,
    CPF_DIGITOS,
    "456.789",
    "Quimera",
    "Zirconio",
    "Inventado",
    EMAIL,
)


class _CapturingTransport(httpx.AsyncBaseTransport):
    def __init__(self):
        self.requests = []

    async def handle_async_request(self, request):
        self.requests.append(request)
        return httpx.Response(200, json={"status": "ok"})


@pytest.fixture
def transport(monkeypatch):
    """Envio real ao interceptor, com o HTTP capturado em memória."""
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

    class _AsyncClientCapturado(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = capturing
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _AsyncClientCapturado)
    return capturing


def _payload_enviado(transport: _CapturingTransport) -> dict:
    assert len(transport.requests) == 1
    return json.loads(transport.requests[0].content.decode("utf-8"))


def _assert_sem_pii(texto: str) -> None:
    for trecho in TRECHOS_PROIBIDOS:
        assert trecho not in texto, trecho


def _mensagem_com_pii() -> str:
    """O formato de uma resposta de API ecoada em `str(e)`."""
    return (
        "API /person respondeu 422: "
        f"{{'cpf': '{CPF_PONTUADO}', 'nome': '{NOME}', 'email': '{EMAIL}'}}"
    )


# ---------------------------------------------------------------------------
# _build_interceptor_payload
# ---------------------------------------------------------------------------


def _build(error_message: str, traceback=None) -> dict:
    return error_interceptor._build_interceptor_payload(
        customer_whatsapp_number="5521999999999",
        flowname="test-flow",
        api_endpoint="internal://Exception",
        input_body={"cpf": CPF_PONTUADO},
        http_status_code=0,
        error_message=error_message,
        traceback=traceback,
        source=None,
    )


def test_payload_redige_cpf_nome_e_email_da_mensagem_e_do_traceback():
    mensagem = _mensagem_com_pii()
    traceback = (
        "Traceback (most recent call last):\n"
        '  File "/app/src/x.py", line 10, in get_user_info\n'
        "    raise Exception(mensagem)\n"
        f"Exception: {mensagem}\n"
    )

    payload = _build(mensagem, traceback)

    _assert_sem_pii(json.dumps(payload, ensure_ascii=False))
    error_response = json.loads(payload["error_response"])
    assert "[REDACTED-CPF]" in error_response["error_message"]
    assert "[REDACTED-CPF]" in error_response["traceback"]


def test_payload_redige_cpf_sem_pontuacao_e_credencial_na_mensagem():
    mensagem = (
        f"Falha ao consultar cpf {CPF_DIGITOS} em "
        "https://api.exemplo.local/person?api_key=chave-sintetica-123"
    )

    payload = _build(mensagem)

    error_message = json.loads(payload["error_response"])["error_message"]
    assert CPF_DIGITOS not in error_message
    assert "chave-sintetica-123" not in error_message
    assert "api_key=<redacted>" in error_message


def test_payload_mantem_mensagem_sem_pii_legivel():
    mensagem = "Service Unavailable (503) em https://api.exemplo.local/iptu"
    traceback = 'Traceback (most recent call last):\n  File "/app/src/x.py", line 7\n'

    payload = _build(mensagem, traceback)

    error_response = json.loads(payload["error_response"])
    assert error_response == {"error_message": mensagem, "traceback": traceback}


def test_payload_sem_traceback_continua_sem_a_chave():
    payload = _build("boom")

    assert json.loads(payload["error_response"]) == {"error_message": "boom"}


@pytest.mark.asyncio
async def test_send_api_error_redige_a_mensagem(transport):
    resultado = await error_interceptor.send_api_error(
        user_id="5521999999999",
        source={"source": "mcp", "tool": "teste"},
        api_endpoint="https://api.exemplo.local/person",
        request_body={"q": "teste"},
        status_code=422,
        error_message=_mensagem_com_pii(),
    )

    assert resultado is True
    _assert_sem_pii(json.dumps(_payload_enviado(transport), ensure_ascii=False))


# ---------------------------------------------------------------------------
# Decorator @interceptor, pelo caminho real
# ---------------------------------------------------------------------------


@error_interceptor.interceptor(source={"source": "mcp", "tool": "teste_dev555"})
async def _funcao_que_falha_com_pii(protocolo: str) -> None:
    raise ValueError(_mensagem_com_pii())


@error_interceptor.interceptor(source={"source": "mcp", "tool": "teste_dev555"})
def _funcao_sync_que_falha_com_pii(protocolo: str) -> None:
    raise ValueError(_mensagem_com_pii())


@pytest.mark.asyncio
async def test_interceptor_async_nao_envia_cpf_nem_nome_do_str_da_excecao(
    transport,
):
    with pytest.raises(ValueError) as excinfo:
        await _funcao_que_falha_com_pii("PROTOCOLO-1")

    # A exceção que sobe para o chamador não muda.
    assert CPF_PONTUADO in str(excinfo.value)

    payload = _payload_enviado(transport)
    _assert_sem_pii(json.dumps(payload, ensure_ascii=False))
    error_response = json.loads(payload["error_response"])
    assert "API /person respondeu 422" in error_response["error_message"]
    assert "ValueError" in error_response["traceback"]
    assert "_funcao_que_falha_com_pii" in error_response["traceback"]


def test_interceptor_sync_nao_envia_cpf_nem_nome_do_str_da_excecao(transport):
    with pytest.raises(ValueError):
        _funcao_sync_que_falha_com_pii("PROTOCOLO-1")

    payload = _payload_enviado(transport)
    _assert_sem_pii(json.dumps(payload, ensure_ascii=False))


# ---------------------------------------------------------------------------
# get_user_info (poda de árvore)
# ---------------------------------------------------------------------------


class _ClienteQueFalha:
    """A API `/person` falha com uma mensagem que ecoa o CPF e o nome, sem
    rótulo: o caso que a redação por formato e por chave não alcança."""

    def __init__(self, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def post(self, *args, **kwargs):
        raise RuntimeError(f"cadastro de {NOME} ({CPF_DIGITOS}) inconsistente")


@pytest.mark.asyncio
async def test_get_user_info_relanca_so_o_tipo_da_falha(transport, monkeypatch):
    monkeypatch.setattr(api_service, "InterceptedHTTPClient", _ClienteQueFalha)

    with pytest.raises(Exception) as excinfo:
        await api_service.SGRCAPIService().get_user_info(CPF_DIGITOS)

    assert str(excinfo.value) == "Failed to get user info: RuntimeError"
    assert excinfo.value.__cause__ is None
    assert excinfo.value.__suppress_context__ is True

    payload = _payload_enviado(transport)
    _assert_sem_pii(json.dumps(payload, ensure_ascii=False))
    error_response = json.loads(payload["error_response"])
    assert error_response["error_message"] == "Failed to get user info: RuntimeError"
    assert "inconsistente" not in error_response["traceback"]
