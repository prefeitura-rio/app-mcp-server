"""
Testes do CHATR-322: reportar ao interceptor nunca troca a exceção original.

Em prod, `get_bigquery_result` estourou o prazo com `query_parameters` contendo
`ScalarQueryParameter`. O `json.dumps` do `input_body` quebrou com `TypeError`,
o `async_wrapper` deixou esse `TypeError` escapar e o chamador recebeu o tipo
errado -- o `except BigQueryTimeoutError` de `pluscode_service` nunca casou e o
interceptor nunca recebeu o erro.

Invariantes cobertos aqui:
- `send_error_to_interceptor` é total: falha na montagem do payload vira
  `warning` com traceback e retorno `False`, também para quem a chama direto.
- Objeto desconhecido no `input_body` ou no `source` não derruba o report e
  não leva o valor cru para o interceptor (parâmetro do BigQuery vira
  nome + tipo; o resto vira o nome da classe).
- `sync_wrapper` e `async_wrapper` reportam e propagam a exceção original.
- O `CustomJSONEncoder` compartilhado (DLQ, BigQuery) continua falhando alto.
"""

import asyncio
import contextlib
import datetime
import json
from unittest.mock import AsyncMock

import httpx
import pytest
from google.cloud import bigquery

from src.utils import error_interceptor
from src.utils.bigquery import BigQueryTimeoutError
from src.utils.json_utils import CustomJSONEncoder

# Capturado no import, antes do fixture autouse `block_real_error_interceptor`
# (src/tests/unit/conftest.py) substituir este atributo do módulo por um
# mock -- os testes de payload precisam da serialização real.
_real_send_error_to_interceptor = error_interceptor.send_error_to_interceptor


class _CapturingTransport(httpx.AsyncBaseTransport):
    def __init__(self):
        self.requests = []

    async def handle_async_request(self, request):
        self.requests.append(request)
        return httpx.Response(200, json={"status": "ok"})


def _patch_interceptor_http(monkeypatch, transport: _CapturingTransport) -> None:
    monkeypatch.setattr(
        error_interceptor.env,
        "ERROR_INTERCEPTOR_URL",
        "https://test.interceptor.local/api",
    )
    monkeypatch.setattr(error_interceptor.env, "ERROR_INTERCEPTOR_TOKEN", "test-token")

    real_async_client = httpx.AsyncClient

    def fake_async_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(error_interceptor.httpx, "AsyncClient", fake_async_client)


def _usa_envio_real(monkeypatch) -> None:
    monkeypatch.setattr(
        error_interceptor,
        "send_error_to_interceptor",
        _real_send_error_to_interceptor,
    )


@contextlib.contextmanager
def _captura_warnings():
    """Captura registros `WARNING` do loguru, com a exceção anexada quando há.

    `logger.opt(exception=...)` devolve um logger novo, então trocar
    `logger.warning` por um mock não enxerga esses registros; um sink enxerga.
    """
    registros = []
    handler_id = error_interceptor.logger.add(
        lambda mensagem: registros.append(mensagem.record),
        level="WARNING",
        format="{message}",
    )
    try:
        yield registros
    finally:
        error_interceptor.logger.remove(handler_id)


def _registros_com(registros, trecho: str):
    return [r for r in registros if trecho in r["message"]]


@error_interceptor.interceptor(source={"source": "mcp", "tool": "bigquery"})
async def _consulta_que_estoura_o_prazo(query: str, query_parameters: list = None):
    raise BigQueryTimeoutError("BigQuery query execution timed out after 10.0s")


@error_interceptor.interceptor(source={"source": "mcp", "tool": "bigquery"})
def _consulta_sync_que_falha(query: str, query_parameters: list = None):
    raise ValueError("consulta sync falhou")


_QUERY_PARAMETERS = [
    bigquery.ScalarQueryParameter("plus8", "STRING", "589R4QQ6+"),
    bigquery.ScalarQueryParameter("cpf", "STRING", "12345678909"),
]

_PARAMETROS_ESPERADOS = [
    {"name": "plus8", "type": "STRING"},
    {"name": "cpf", "type": "STRING"},
]


def _payload_enviado(transport: _CapturingTransport) -> dict:
    assert len(transport.requests) == 1
    return json.loads(transport.requests[0].content.decode("utf-8"))


# ---------------------------------------------------------------------------
# Caso de prod e formato do input_body
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_timeout_com_query_parameters_chega_ao_chamador_e_ao_interceptor(
    monkeypatch,
):
    """Reproduz o caso de prod: o chamador recebe `BigQueryTimeoutError` e o
    interceptor recebe o `input_body` com os parâmetros descritos sem valor."""
    transport = _CapturingTransport()
    _patch_interceptor_http(monkeypatch, transport)
    _usa_envio_real(monkeypatch)
    spy = AsyncMock(wraps=error_interceptor.send_general_error)
    monkeypatch.setattr(error_interceptor, "send_general_error", spy)

    with pytest.raises(BigQueryTimeoutError, match="timed out after 10.0s"):
        await _consulta_que_estoura_o_prazo(
            "select 1", query_parameters=_QUERY_PARAMETERS
        )

    spy.assert_awaited_once()
    assert spy.await_args.kwargs["error_type"] == "BigQueryTimeoutError"
    assert spy.await_args.kwargs["input_body"]["query_parameters"] == (
        _QUERY_PARAMETERS
    )

    sent_payload = _payload_enviado(transport)
    sent_input_body = json.loads(sent_payload["input_body"])
    assert sent_input_body["query"] == "select 1"
    assert sent_input_body["query_parameters"] == _PARAMETROS_ESPERADOS
    assert "12345678909" not in sent_payload["input_body"]
    assert "589R4QQ6+" not in sent_payload["input_body"]


@pytest.mark.asyncio
async def test_valor_de_parametro_do_bigquery_nao_sai_do_processo(monkeypatch):
    """Nome, endereço e data de nascimento não casam com a redação por regex;
    o parâmetro vai como nome + tipo, sem valor, em todas as variantes."""
    transport = _CapturingTransport()
    _patch_interceptor_http(monkeypatch, transport)

    nome = bigquery.ScalarQueryParameter("nome", "STRING", "Maria da Silva")
    enderecos = bigquery.ArrayQueryParameter(
        "enderecos", "STRING", ["Rua das Laranjeiras, 123"]
    )
    pessoa = bigquery.StructQueryParameter(
        "pessoa",
        bigquery.ScalarQueryParameter("nascimento", "STRING", "1980-05-17"),
    )

    result = await _real_send_error_to_interceptor(
        customer_whatsapp_number="5521999999999",
        flowname="bigquery",
        api_endpoint="internal://BigQueryTimeoutError",
        input_body={"query_parameters": [nome, enderecos, pessoa]},
        http_status_code=0,
        error_message="timeout",
    )

    assert result is True
    sent_payload = _payload_enviado(transport)
    assert json.loads(sent_payload["input_body"])["query_parameters"] == [
        {"name": "nome", "type": "STRING"},
        {"name": "enderecos", "type": "ARRAY"},
        {"name": "pessoa", "type": "STRUCT"},
    ]
    corpo = json.dumps(sent_payload, ensure_ascii=False)
    assert "Maria da Silva" not in corpo
    assert "Laranjeiras" not in corpo
    assert "1980-05-17" not in corpo


@pytest.mark.asyncio
async def test_objeto_desconhecido_vira_nome_da_classe(monkeypatch):
    """Fora os parâmetros do BigQuery, objeto desconhecido vira só o nome da
    classe, como `_handle_error` já faz com os argumentos de topo."""
    transport = _CapturingTransport()
    _patch_interceptor_http(monkeypatch, transport)

    class Cliente:
        def __repr__(self):
            return "Cliente(nome='Maria da Silva')"

    result = await _real_send_error_to_interceptor(
        customer_whatsapp_number="5521999999999",
        flowname="bigquery",
        api_endpoint="internal://ValueError",
        input_body={"clientes": [Cliente()]},
        http_status_code=0,
        error_message="falhou",
    )

    assert result is True
    sent_payload = _payload_enviado(transport)
    assert json.loads(sent_payload["input_body"])["clientes"] == ["Cliente"]
    assert "Maria da Silva" not in sent_payload["input_body"]


# ---------------------------------------------------------------------------
# source e payload: a função de envio é total
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_source_com_valor_fora_do_json_nao_derruba_o_report(monkeypatch):
    """Um `extract_source` pode injetar datetime ou objeto no source; o report
    segue com o mesmo encoder do input_body."""
    transport = _CapturingTransport()
    _patch_interceptor_http(monkeypatch, transport)

    class Contexto:
        pass

    result = await _real_send_error_to_interceptor(
        customer_whatsapp_number="5521999999999",
        flowname="equipments",
        api_endpoint="internal://ValueError",
        input_body={"address": "Av. Vargas"},
        http_status_code=0,
        error_message="falhou",
        source={
            "source": "mcp",
            "tool": "equipments",
            "quando": datetime.datetime(2026, 10, 28, 10, 0, 0),
            "contexto": Contexto(),
        },
    )

    assert result is True
    sent_source = json.loads(_payload_enviado(transport)["source"])
    assert sent_source["quando"] == "2026-10-28T10:00:00"
    assert sent_source["contexto"] == "Contexto"


@pytest.mark.asyncio
async def test_falha_ao_montar_o_payload_nao_propaga_para_o_chamador_direto(
    monkeypatch,
):
    """`http_client`, `divida_ativa_v2`, `external_tables` e `rock_in_rio`
    chamam a função direto dentro de um `except`: ela nunca pode levantar."""
    transport = _CapturingTransport()
    _patch_interceptor_http(monkeypatch, transport)

    def redator_quebrado(_texto: str) -> str:
        raise RuntimeError("redator quebrou")

    monkeypatch.setattr(error_interceptor, "_redact_pii_in_text", redator_quebrado)

    with _captura_warnings() as registros:
        result = await _real_send_error_to_interceptor(
            customer_whatsapp_number="5521999999999",
            flowname="iptu",
            api_endpoint="https://api.example.com/iptu",
            input_body={"inscricao": "12345678"},
            http_status_code=500,
            error_message="Internal Server Error",
        )

    assert result is False
    assert transport.requests == []
    (registro,) = _registros_com(registros, "Falha ao montar o payload")
    assert registro["exception"] is not None
    assert registro["exception"].type is RuntimeError


@pytest.mark.asyncio
async def test_falha_real_no_report_nao_troca_a_excecao_original(monkeypatch):
    """Pelo decorator, com o envio real: a falha na serialização vira
    `warning` com traceback e o chamador recebe a exceção original."""
    transport = _CapturingTransport()
    _patch_interceptor_http(monkeypatch, transport)
    _usa_envio_real(monkeypatch)

    def redator_quebrado(_texto: str) -> str:
        raise RuntimeError("redator quebrou")

    monkeypatch.setattr(error_interceptor, "_redact_pii_in_text", redator_quebrado)

    with _captura_warnings() as registros:
        with pytest.raises(BigQueryTimeoutError, match="timed out after 10.0s"):
            await _consulta_que_estoura_o_prazo(
                "select 1", query_parameters=_QUERY_PARAMETERS
            )

    assert transport.requests == []
    (registro,) = _registros_com(registros, "Falha ao montar o payload")
    assert registro["exception"].type is RuntimeError
    # A função de envio é total, então o guarda do wrapper não precisou agir.
    assert _registros_com(registros, "Falha ao reportar ao interceptor") == []


@pytest.mark.asyncio
async def test_guarda_do_async_wrapper_loga_com_traceback(monkeypatch):
    """Se algo antes do envio em `_handle_error` levantar, o wrapper segura,
    loga com traceback e propaga a exceção original."""
    monkeypatch.setattr(
        error_interceptor,
        "send_general_error",
        AsyncMock(side_effect=TypeError("Object of type X is not JSON serializable")),
    )

    with _captura_warnings() as registros:
        with pytest.raises(BigQueryTimeoutError):
            await _consulta_que_estoura_o_prazo(
                "select 1", query_parameters=_QUERY_PARAMETERS
            )

    (registro,) = _registros_com(registros, "Falha ao reportar ao interceptor")
    assert registro["exception"] is not None
    assert registro["exception"].type is TypeError


# ---------------------------------------------------------------------------
# sync_wrapper
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_wrapper_reporta_parametros_e_propaga_a_excecao_original(
    monkeypatch,
):
    """Função sync decorada, dentro de um event loop: o report vai numa task
    fire-and-forget e a exceção original chega ao chamador."""
    transport = _CapturingTransport()
    _patch_interceptor_http(monkeypatch, transport)
    _usa_envio_real(monkeypatch)

    antes = set(error_interceptor._pending_interceptor_tasks)

    with pytest.raises(ValueError, match="consulta sync falhou"):
        _consulta_sync_que_falha("select 1", query_parameters=_QUERY_PARAMETERS)

    novas = error_interceptor._pending_interceptor_tasks - antes
    assert len(novas) == 1
    await asyncio.gather(*novas)

    sent_payload = _payload_enviado(transport)
    sent_input_body = json.loads(sent_payload["input_body"])
    assert sent_input_body["query_parameters"] == _PARAMETROS_ESPERADOS
    assert "12345678909" not in sent_payload["input_body"]
    assert novas.isdisjoint(error_interceptor._pending_interceptor_tasks)


# ---------------------------------------------------------------------------
# O encoder compartilhado não ganhou o fallback
# ---------------------------------------------------------------------------


def test_custom_json_encoder_compartilhado_continua_falhando_alto():
    """DLQ e gravação no BigQuery usam `CustomJSONEncoder` e devem continuar
    falhando com `TypeError` em vez de gravar nome + tipo de um parâmetro."""
    with pytest.raises(TypeError, match="not JSON serializable"):
        json.dumps({"query_parameters": _QUERY_PARAMETERS}, cls=CustomJSONEncoder)
