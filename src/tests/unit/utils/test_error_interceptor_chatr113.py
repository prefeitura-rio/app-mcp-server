"""
Testes para as melhorias do CHATR-113 em `error_interceptor`
(docs/decisions/CHATR-113-sentry-vs-error-interceptor.md):

1. Correlação de trace/span do OpenTelemetry no payload.
2. Redação básica de PII (customer_whatsapp_number e input_body).
3. Tracking das tasks fire-and-forget do decorator `interceptor()`.
"""

import asyncio
import json

import httpx
import pytest
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider

from src.utils import error_interceptor

# Capturado no import, antes do fixture autouse `block_real_error_interceptor`
# (src/tests/unit/conftest.py) substituir este atributo do módulo por um
# mock para os demais testes da suíte -- os testes de payload abaixo
# precisam da implementação real para inspecionar o que é montado/enviado.
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


# ---------------------------------------------------------------------------
# 1. Correlação de trace/span (CHATR-110/CHATR-113)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_send_error_to_interceptor_omits_trace_fields_without_active_span(
    monkeypatch,
):
    """Sem span ativo (tracing desabilitado/não configurado), trace_id e
    span_id não aparecem no payload, e nada levanta exceção."""
    transport = _CapturingTransport()
    _patch_interceptor_http(monkeypatch, transport)

    assert trace.get_current_span().get_span_context().is_valid is False

    result = await _real_send_error_to_interceptor(
        customer_whatsapp_number="5521999999999",
        flowname="test-flow",
        api_endpoint="https://api.example.com/test",
        input_body={"foo": "bar"},
        http_status_code=500,
        error_message="boom",
    )

    assert result is True
    sent_payload = json.loads(transport.requests[0].content.decode("utf-8"))
    assert "trace_id" not in sent_payload
    assert "span_id" not in sent_payload


@pytest.mark.asyncio
async def test_send_error_to_interceptor_includes_trace_fields_with_active_span(
    monkeypatch,
):
    """Com um span OTel ativo, o payload carrega trace_id/span_id (hex, 32
    e 16 caracteres) correspondentes ao span corrente, permitindo cruzar o
    erro reportado com o trace equivalente no SigNoz (CHATR-110).

    Usa um `TracerProvider` local (não registrado globalmente via
    `trace.set_tracer_provider`) para não interferir em outros testes.
    """
    transport = _CapturingTransport()
    _patch_interceptor_http(monkeypatch, transport)

    local_tracer = TracerProvider().get_tracer("test-chatr-113")

    with local_tracer.start_as_current_span("test-span") as span:
        span_context = span.get_span_context()
        expected_trace_id = trace.format_trace_id(span_context.trace_id)
        expected_span_id = trace.format_span_id(span_context.span_id)

        result = await _real_send_error_to_interceptor(
            customer_whatsapp_number="5521999999999",
            flowname="test-flow",
            api_endpoint="https://api.example.com/test",
            input_body={"foo": "bar"},
            http_status_code=500,
            error_message="boom",
        )

    assert result is True
    sent_payload = json.loads(transport.requests[0].content.decode("utf-8"))
    assert sent_payload["trace_id"] == expected_trace_id
    assert len(sent_payload["trace_id"]) == 32
    assert sent_payload["span_id"] == expected_span_id
    assert len(sent_payload["span_id"]) == 16

    # O TracerProvider global não deve ter sido alterado por este teste.
    assert trace.get_current_span().get_span_context().is_valid is False


def test_get_current_trace_context_never_raises_on_unexpected_error(monkeypatch):
    """Qualquer falha inesperada ao acessar o contexto de trace deve
    degradar graciosamente -- nunca propagar exceção para o chamador."""

    class _ExplodingSpan:
        def get_span_context(self):
            raise RuntimeError("boom")

    monkeypatch.setattr(
        error_interceptor.trace, "get_current_span", lambda: _ExplodingSpan()
    )

    assert error_interceptor._get_current_trace_context() == {}


# ---------------------------------------------------------------------------
# 2. Redação básica de PII (CHATR-113)
# ---------------------------------------------------------------------------


def test_mask_last_four_digits_keeps_only_last_four_visible():
    assert error_interceptor._mask_last_four_digits("5521999999999") == "*********9999"
    assert (
        error_interceptor._mask_last_four_digits("+5521999999999") == "+*********9999"
    )


def test_mask_last_four_digits_short_values_are_left_untouched():
    # Nada de útil a mascarar em valores muito curtos -- retorna como está
    # em vez de produzir algo enganoso (ex.: mascarar tudo).
    assert error_interceptor._mask_last_four_digits("1234") == "1234"
    assert error_interceptor._mask_last_four_digits("") == ""


def test_redact_pii_in_text_redacts_cpf_and_phone_but_keeps_the_rest():
    text = (
        '{"cliente": "Fulano de Tal", "cpf": "123.456.789-01", '
        '"telefone": "5521999999999", "observacao": "ligar as 10h"}'
    )

    redacted = error_interceptor._redact_pii_in_text(text)

    assert "123.456.789-01" not in redacted
    assert "5521999999999" not in redacted
    assert "[REDACTED-CPF]" in redacted
    assert "[REDACTED-PHONE]" in redacted
    # Não é uma limpeza completa do conteúdo -- o restante permanece visível
    # para preservar contexto útil de debug.
    assert "Fulano de Tal" in redacted
    assert "ligar as 10h" in redacted


@pytest.mark.asyncio
async def test_send_error_to_interceptor_masks_whatsapp_number_in_payload(
    monkeypatch,
):
    transport = _CapturingTransport()
    _patch_interceptor_http(monkeypatch, transport)

    result = await _real_send_error_to_interceptor(
        customer_whatsapp_number="5521999999999",
        flowname="test-flow",
        api_endpoint="https://api.example.com/test",
        input_body={"foo": "bar"},
        http_status_code=500,
        error_message="boom",
    )

    assert result is True
    sent_payload = json.loads(transport.requests[0].content.decode("utf-8"))
    assert sent_payload["customer_whatsapp_number"] == "*********9999"
    assert "5521999999999" not in transport.requests[0].content.decode("utf-8")


@pytest.mark.asyncio
async def test_send_error_to_interceptor_redacts_cpf_in_input_body(monkeypatch):
    transport = _CapturingTransport()
    _patch_interceptor_http(monkeypatch, transport)

    result = await _real_send_error_to_interceptor(
        customer_whatsapp_number="5521999999999",
        flowname="test-flow",
        api_endpoint="https://api.example.com/test",
        input_body={"cpf": "123.456.789-01", "nome": "Fulano"},
        http_status_code=500,
        error_message="boom",
    )

    assert result is True
    sent_payload = json.loads(transport.requests[0].content.decode("utf-8"))
    assert "123.456.789-01" not in sent_payload["input_body"]
    assert "[REDACTED-CPF]" in sent_payload["input_body"]
    # Contexto de debug (o restante do input_body) é preservado.
    assert "Fulano" in sent_payload["input_body"]


# ---------------------------------------------------------------------------
# 3. Tracking de tasks fire-and-forget do decorator `interceptor()`
#    (CHATR-113 -- error_interceptor.py:367-385 antes do fix)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sync_wrapper_tracks_fire_and_forget_task_until_completion(
    block_real_error_interceptor,
):
    """A task fire-and-forget criada por `sync_wrapper` deve ficar
    referenciada em `_pending_interceptor_tasks` enquanto pendente (evitando
    coleta pelo GC no meio da execução) e ser removida ao concluir -- sem
    que o chamador original seja bloqueado/aguardado."""

    @error_interceptor.interceptor(source={"source": "mcp", "tool": "test"})
    def flaky(x):
        raise ValueError(f"bad value: {x}")

    tasks_before = set(error_interceptor._pending_interceptor_tasks)

    with pytest.raises(ValueError, match="bad value: 42"):
        flaky(42)

    # `sync_wrapper` retorna/levanta sem aguardar -- a task só foi agendada.
    new_tasks = set(error_interceptor._pending_interceptor_tasks) - tasks_before
    assert len(new_tasks) == 1

    await asyncio.gather(*new_tasks)

    # O done-callback já limpou o set e o report foi de fato enviado (não
    # perdido, que era o bug original).
    assert set(error_interceptor._pending_interceptor_tasks) == tasks_before
    block_real_error_interceptor.assert_awaited_once()


@pytest.mark.asyncio
async def test_track_interceptor_task_logs_but_does_not_raise_on_task_exception():
    """Se a própria task de report falhar internamente, isso deve ser
    observável via log no done-callback, sem propagar para fora e sem
    deixar a task presa em `_pending_interceptor_tasks`."""

    async def _boom():
        raise RuntimeError("internal failure")

    task = asyncio.get_running_loop().create_task(_boom())
    error_interceptor._track_interceptor_task(task)

    assert task in error_interceptor._pending_interceptor_tasks

    # `asyncio.wait` observa a conclusão sem repropagar a exceção da task,
    # o que permite validar o done-callback isoladamente.
    await asyncio.wait([task])
    await asyncio.sleep(0)

    assert task.done()
    assert isinstance(task.exception(), RuntimeError)
    assert task not in error_interceptor._pending_interceptor_tasks
