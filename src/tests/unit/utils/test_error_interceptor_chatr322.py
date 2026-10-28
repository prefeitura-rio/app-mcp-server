"""
Testes do CHATR-322: o decorator `interceptor()` nunca troca a exceção original.

Em prod, `get_bigquery_result` estourou o prazo com `query_parameters` contendo
`ScalarQueryParameter`. O `json.dumps` do `input_body` quebrou com `TypeError`,
o `async_wrapper` deixou esse `TypeError` escapar e o chamador recebeu o tipo
errado -- o `except BigQueryTimeoutError` de `pluscode_service` nunca casou e o
interceptor nunca recebeu o erro.
"""

import json
from unittest.mock import AsyncMock

import httpx
import pytest
from google.cloud import bigquery

from src.utils import error_interceptor
from src.utils.bigquery import BigQueryTimeoutError

# Capturado no import, antes do fixture autouse `block_real_error_interceptor`
# (src/tests/unit/conftest.py) substituir este atributo do módulo por um
# mock -- o teste de payload precisa da serialização real.
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


@error_interceptor.interceptor(source={"source": "mcp", "tool": "bigquery"})
async def _consulta_que_estoura_o_prazo(query: str, query_parameters: list = None):
    raise BigQueryTimeoutError("BigQuery query execution timed out after 10.0s")


_QUERY_PARAMETERS = [
    bigquery.ScalarQueryParameter("plus8", "STRING", "589R4QQ6+"),
    bigquery.ScalarQueryParameter("cpf", "STRING", "12345678909"),
]


@pytest.mark.asyncio
async def test_timeout_com_query_parameters_chega_ao_chamador_e_ao_interceptor(
    monkeypatch,
):
    """Reproduz o caso de prod: o chamador recebe `BigQueryTimeoutError` e o
    interceptor recebe o `input_body` serializado, com PII redigida."""
    transport = _CapturingTransport()
    _patch_interceptor_http(monkeypatch, transport)
    monkeypatch.setattr(
        error_interceptor,
        "send_error_to_interceptor",
        _real_send_error_to_interceptor,
    )
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

    assert len(transport.requests) == 1
    sent_payload = json.loads(transport.requests[0].content.decode("utf-8"))
    sent_input_body = json.loads(sent_payload["input_body"])
    assert sent_input_body["query"] == "select 1"
    assert sent_input_body["query_parameters"] == [
        "ScalarQueryParameter('plus8', 'STRING', '589R4QQ6+')",
        "ScalarQueryParameter('cpf', 'STRING', '[REDACTED-CPF]')",
    ]
    assert "12345678909" not in sent_payload["input_body"]


@pytest.mark.asyncio
async def test_falha_no_report_nao_troca_a_excecao_original(monkeypatch):
    """Qualquer falha ao reportar vira `warning`; o chamador continua
    recebendo a exceção original."""
    monkeypatch.setattr(
        error_interceptor,
        "send_general_error",
        AsyncMock(side_effect=TypeError("Object of type X is not JSON serializable")),
    )
    warnings = []
    monkeypatch.setattr(error_interceptor.logger, "warning", warnings.append)

    with pytest.raises(BigQueryTimeoutError):
        await _consulta_que_estoura_o_prazo(
            "select 1", query_parameters=_QUERY_PARAMETERS
        )

    assert len(warnings) == 1
    assert "Falha ao reportar ao interceptor" in warnings[0]
    assert "TypeError" in warnings[0]
