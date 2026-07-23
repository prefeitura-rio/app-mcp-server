import datetime
import json

import httpx
import pytest

from src.utils import error_interceptor

# Captured at import time, before the autouse `block_real_error_interceptor`
# fixture (src/tests/unit/conftest.py) replaces this module attribute with a
# mock for every other test in the suite. This test specifically needs the
# real implementation to exercise its JSON serialization.
_real_send_error_to_interceptor = error_interceptor.send_error_to_interceptor


class _CapturingTransport(httpx.AsyncBaseTransport):
    def __init__(self):
        self.requests = []

    async def handle_async_request(self, request):
        self.requests.append(request)
        return httpx.Response(200, json={"status": "ok"})


@pytest.mark.asyncio
async def test_send_error_to_interceptor_serializes_datetime_time_input_body(
    monkeypatch,
):
    """Regression test for CHATR-100/CHATR-107.

    input_body may contain the original arguments of a failed call, which can
    include raw datetime.time/date/datetime values (e.g. captured from a
    BigQuery TIME column). Reporting the error itself must not fail with a
    second serialization TypeError.
    """
    monkeypatch.setattr(error_interceptor.env, "ERROR_INTERCEPTOR_URL", "https://test.interceptor.local/api")
    monkeypatch.setattr(error_interceptor.env, "ERROR_INTERCEPTOR_TOKEN", "test-token")

    transport = _CapturingTransport()
    real_async_client = httpx.AsyncClient

    def fake_async_client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(error_interceptor.httpx, "AsyncClient", fake_async_client)

    input_body = {
        "data": {
            "horario_funcionamento": datetime.time(8, 30, 0),
            "atualizado_em": datetime.datetime(2026, 4, 8, 9, 0, 0),
            "inaugurado_em": datetime.date(2010, 1, 1),
        }
    }

    result = await _real_send_error_to_interceptor(
        customer_whatsapp_number="5521999999999",
        flowname="bigquery",
        api_endpoint="https://api.example.com/bigquery",
        input_body=input_body,
        http_status_code=500,
        error_message="Object of type time is not JSON serializable",
    )

    assert result is True
    assert len(transport.requests) == 1
    sent_payload = json.loads(transport.requests[0].content.decode("utf-8"))
    sent_input_body = json.loads(sent_payload["input_body"])
    assert sent_input_body["data"]["horario_funcionamento"] == "08:30:00"
    assert sent_input_body["data"]["atualizado_em"] == "2026-04-08T09:00:00"
    assert sent_input_body["data"]["inaugurado_em"] == "2010-01-01"
