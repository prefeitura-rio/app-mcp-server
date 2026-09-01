"""Testes de `ToolCallTracingMiddleware` (src/observability/tracing.py).

Cobre a combinação de tracing e métricas: os dois são opt-in
independentemente um do outro, e nenhum atributo de métrica pode carregar o
`user_id`/texto de exceção que o span (canal separado, consumido só em
ferramenta de trace) tem permissão de carregar.
"""

import types

import pytest
from mcp.types import CallToolRequestParams
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)

from src.observability import metrics, tracing


def _context(tool_name: str, arguments: dict | None = None):
    message = CallToolRequestParams(name=tool_name, arguments=arguments)
    return types.SimpleNamespace(message=message)


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    metrics.reset_for_tests()
    monkeypatch.setattr(tracing, "_tracing_enabled", False)
    yield
    metrics.reset_for_tests()


@pytest.fixture
def span_exporter(monkeypatch):
    provider = TracerProvider()
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(tracing, "get_tracer", lambda: provider.get_tracer("test"))
    monkeypatch.setattr(tracing, "_tracing_enabled", True)
    return exporter


@pytest.fixture
def metrics_reader():
    reader = InMemoryMetricReader()
    metrics.configure_for_test(reader)
    return reader


def _points(reader: InMemoryMetricReader, metric_name: str) -> list:
    points = []
    for resource_metrics in reader.get_metrics_data().resource_metrics:
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                if metric.name == metric_name:
                    points.extend(metric.data.data_points)
    return points


@pytest.mark.asyncio
async def test_passthrough_quando_nada_esta_habilitado():
    middleware = tracing.ToolCallTracingMiddleware()
    chamado = []

    async def call_next(ctx):
        chamado.append(ctx)
        return "resultado"

    result = await middleware.on_call_tool(_context("calculator_add"), call_next)

    assert result == "resultado"
    assert len(chamado) == 1


@pytest.mark.asyncio
async def test_apenas_metricas_habilitadas_nao_cria_span(monkeypatch, metrics_reader):
    middleware = tracing.ToolCallTracingMiddleware()

    def _explode():
        raise AssertionError("não deveria criar tracer sem tracing habilitado")

    monkeypatch.setattr(tracing, "get_tracer", _explode)

    async def call_next(ctx):
        return {"ok": True}

    await middleware.on_call_tool(_context("calculator_add"), call_next)

    calls = _points(metrics_reader, "mcp.tool.calls")
    assert calls[0].attributes == {
        "mcp.tool.name": "calculator_add",
        "status": "success",
    }


@pytest.mark.asyncio
async def test_tracing_habilitado_cria_span_com_atributos_esperados(span_exporter):
    middleware = tracing.ToolCallTracingMiddleware()

    async def call_next(ctx):
        return {"ok": True}

    await middleware.on_call_tool(
        _context("get_user_memory", {"user_id": "5521999999999"}), call_next
    )

    (span,) = span_exporter.get_finished_spans()
    assert span.attributes["mcp.tool.name"] == "get_user_memory"
    assert span.attributes["mcp.tool.user_id"] == "5521999999999"
    assert span.attributes["mcp.tool.success"] is True


@pytest.mark.asyncio
async def test_falha_na_tool_marca_span_e_metrica_como_erro(
    span_exporter, metrics_reader
):
    middleware = tracing.ToolCallTracingMiddleware()

    async def call_next(ctx):
        raise ValueError("boom")

    with pytest.raises(ValueError):
        await middleware.on_call_tool(_context("multi_step_service"), call_next)

    (span,) = span_exporter.get_finished_spans()
    assert span.attributes["mcp.tool.success"] is False

    calls = _points(metrics_reader, "mcp.tool.calls")
    assert calls[0].attributes["status"] == "error"


@pytest.mark.asyncio
async def test_trabalho_ativo_e_contabilizado_durante_a_execucao(metrics_reader):
    middleware = tracing.ToolCallTracingMiddleware()
    observado_durante_execucao = {}

    async def call_next(ctx):
        active = _points(metrics_reader, "mcp.tool.calls.active")
        observado_durante_execucao["value"] = active[0].value
        return "ok"

    await middleware.on_call_tool(_context("google_search"), call_next)

    assert observado_durante_execucao["value"] == 1

    active_apos = _points(metrics_reader, "mcp.tool.calls.active")
    assert active_apos[0].value == 0


@pytest.mark.asyncio
async def test_metrica_de_tool_call_nunca_carrega_user_id(
    span_exporter, metrics_reader
):
    """O span pode ter `mcp.tool.user_id` (consumido em ferramenta de trace,
    fora do escopo desta task); a métrica agregada NUNCA pode."""
    middleware = tracing.ToolCallTracingMiddleware()

    async def call_next(ctx):
        return {"ok": True}

    await middleware.on_call_tool(
        _context("get_user_memory", {"user_id": "5521999999999"}), call_next
    )

    calls = _points(metrics_reader, "mcp.tool.calls")
    for point in calls:
        assert set(point.attributes.keys()) == {"mcp.tool.name", "status"}
        assert "5521999999999" not in str(point.attributes)
