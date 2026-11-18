"""Spans de erro não carregam o texto da exceção (DEV-640).

A `ValidationError` que o FastMCP levanta para argumento inválido de tool tem
como mensagem o `str()` do erro do pydantic, com `input_value=` e o dict de
argumentos inteiro. Tanto o span `mcp.tool_call` do nosso middleware quanto o
`tools/call <tool>` do próprio FastMCP (`fastmcp.server.telemetry.server_span`)
gravavam essa mensagem no evento de exceção e na descrição do status, que vão
ao SigNoz. Estes testes provam que, depois da redação, só o tipo do erro sai.

Todos os valores são sintéticos.
"""

import pytest
from fastmcp import Client, FastMCP
from fastmcp.exceptions import ToolError
from opentelemetry.sdk.trace import Event, ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExportResult
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from opentelemetry.trace import Status, StatusCode

from src.observability import tracing

# Valor sintético com cara de inscrição imobiliária: não pode aparecer em
# nenhum span exportado.
_VALOR_SINTETICO = "SINTETICO-0000000-0"


def _texto_exportado(spans) -> str:
    """Tudo o que um span leva ao backend que pode conter texto livre."""
    partes = []
    for span in spans:
        partes.append(span.name)
        partes.append(repr(dict(span.attributes or {})))
        partes.append(repr(span.status.description))
        for evento in span.events:
            partes.append(evento.name)
            partes.append(repr(dict(evento.attributes or {})))
    return "\n".join(partes)


@pytest.fixture
def exportado(monkeypatch):
    """Provider real com o redator na frente de um exportador em memória.

    O tracer do FastMCP e o do middleware passam a usar este provider; o
    provider global do processo não é tocado.
    """
    import fastmcp.server.telemetry as fastmcp_telemetry

    memoria = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(
        SimpleSpanProcessor(tracing.ExceptionTextRedactingSpanExporter(memoria))
    )
    monkeypatch.setattr(
        fastmcp_telemetry, "get_tracer", lambda: provider.get_tracer("fastmcp")
    )
    monkeypatch.setattr(tracing, "get_tracer", lambda: provider.get_tracer("test"))
    monkeypatch.setattr(tracing, "_tracing_enabled", True)
    return memoria


def _servidor() -> FastMCP:
    mcp = FastMCP("teste-dev-640")
    mcp.add_middleware(tracing.ToolCallTracingMiddleware())

    @mcp.tool
    def consultar(payload_json: str) -> str:
        return "ok"

    return mcp


@pytest.mark.asyncio
async def test_argumento_invalido_nao_vaza_valor_em_nenhum_span(exportado):
    async with Client(_servidor()) as client:
        with pytest.raises(ToolError):
            await client.call_tool("consultar", {"payload": _VALOR_SINTETICO})

    spans = exportado.get_finished_spans()
    por_nome = {span.name: span for span in spans}
    assert "mcp.tool_call" in por_nome
    assert "tools/call consultar" in por_nome

    texto = _texto_exportado(spans)
    assert _VALOR_SINTETICO not in texto
    assert "input_value" not in texto

    middleware = por_nome["mcp.tool_call"]
    assert middleware.status.status_code is StatusCode.ERROR
    assert middleware.status.description is None
    assert middleware.attributes["error.type"] == "ValidationError"
    assert middleware.attributes["mcp.tool.success"] is False
    assert [e for e in middleware.events if e.name == "exception"] == []

    biblioteca = por_nome["tools/call consultar"]
    assert biblioteca.status.status_code is StatusCode.ERROR
    assert biblioteca.status.description is None
    assert biblioteca.attributes["error.type"] == "ValidationError"
    for evento in biblioteca.events:
        if evento.name == "exception":
            assert set(evento.attributes) <= {"exception.type", "exception.escaped"}


@pytest.mark.asyncio
async def test_chamada_valida_continua_com_status_ok(exportado):
    async with Client(_servidor()) as client:
        resultado = await client.call_tool("consultar", {"payload_json": "{}"})

    assert resultado.data == "ok"
    por_nome = {span.name: span for span in exportado.get_finished_spans()}
    assert por_nome["mcp.tool_call"].status.status_code is StatusCode.OK
    assert por_nome["mcp.tool_call"].attributes["mcp.tool.success"] is True


# ---------------------------------------------------------------------------
# Redator do exportador, isolado
# ---------------------------------------------------------------------------


def _span(events=(), status=None, attributes=None) -> ReadableSpan:
    return ReadableSpan(
        name="tools/call consultar",
        attributes=attributes or {"gen_ai.tool.name": "consultar"},
        events=events,
        status=status or Status(StatusCode.UNSET),
        start_time=1,
        end_time=2,
    )


class _Capturador:
    def __init__(self):
        self.spans = []
        self.desligado = False
        self.flush = None

    def export(self, spans):
        self.spans.extend(spans)
        return SpanExportResult.SUCCESS

    def shutdown(self):
        self.desligado = True

    def force_flush(self, timeout_millis=30000):
        self.flush = timeout_millis
        return True


def test_redator_deixa_so_o_tipo_no_evento_de_excecao():
    capturador = _Capturador()
    redator = tracing.ExceptionTextRedactingSpanExporter(capturador)
    evento = Event(
        "exception",
        {
            "exception.type": "ValidationError",
            "exception.message": f"input_value={{'payload': '{_VALOR_SINTETICO}'}}",
            "exception.stacktrace": f"Traceback ... {_VALOR_SINTETICO}",
            "exception.escaped": "False",
        },
        timestamp=5,
    )
    span = _span(
        events=[evento],
        status=Status(StatusCode.ERROR, f"input_value={_VALOR_SINTETICO}"),
    )

    assert redator.export([span]) is SpanExportResult.SUCCESS

    (redigido,) = capturador.spans
    assert _VALOR_SINTETICO not in _texto_exportado([redigido])
    (evento_redigido,) = redigido.events
    assert evento_redigido.name == "exception"
    assert evento_redigido.timestamp == 5
    assert dict(evento_redigido.attributes) == {
        "exception.type": "ValidationError",
        "exception.escaped": "False",
    }
    assert redigido.status.status_code is StatusCode.ERROR
    assert redigido.status.description is None
    # O resto do span é preservado.
    assert redigido.name == span.name
    assert dict(redigido.attributes) == {"gen_ai.tool.name": "consultar"}
    assert (redigido.start_time, redigido.end_time) == (1, 2)


def test_redator_preserva_eventos_que_nao_sao_de_excecao():
    capturador = _Capturador()
    redator = tracing.ExceptionTextRedactingSpanExporter(capturador)
    evento = Event("cache.hit", {"cache.key_prefix": "tool"}, timestamp=3)
    span = _span(events=[evento], status=Status(StatusCode.ERROR, "falhou"))

    redator.export([span])

    (redigido,) = capturador.spans
    assert [(e.name, dict(e.attributes)) for e in redigido.events] == [
        ("cache.hit", {"cache.key_prefix": "tool"})
    ]
    assert redigido.status.description is None


def test_redator_nao_reconstroi_span_sem_texto_de_erro():
    capturador = _Capturador()
    redator = tracing.ExceptionTextRedactingSpanExporter(capturador)
    span = _span(status=Status(StatusCode.OK))

    redator.export([span])

    assert capturador.spans == [span]
    assert capturador.spans[0] is span


def test_redator_repassa_shutdown_e_flush():
    capturador = _Capturador()
    redator = tracing.ExceptionTextRedactingSpanExporter(capturador)

    assert redator.force_flush(1234) is True
    redator.shutdown()

    assert capturador.flush == 1234
    assert capturador.desligado is True


def test_setup_embrulha_o_exportador_otlp_no_redator(monkeypatch):
    capturado = {}

    class _ProviderFalso:
        def __init__(self, resource=None):
            pass

        def add_span_processor(self, processor):
            capturado["processor"] = processor

    def _batch(exportador, **_kwargs):
        capturado["exportador"] = exportador
        return "processor"

    monkeypatch.setattr(tracing, "_tracing_enabled", False)
    monkeypatch.setattr(tracing, "_setup_attempted", False)
    monkeypatch.setattr(tracing, "TracerProvider", _ProviderFalso)
    monkeypatch.setattr(tracing, "BatchSpanProcessor", _batch)
    monkeypatch.setattr(tracing, "OTLPSpanExporter", lambda **_k: "otlp")
    monkeypatch.setattr(tracing.trace, "set_tracer_provider", lambda _p: None)
    monkeypatch.setattr(
        tracing.env, "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT", "http://collector:4318"
    )
    monkeypatch.setattr(tracing.env, "OTEL_EXPORTER_OTLP_TRACES_HEADERS", "")

    assert tracing.setup_tracing() is True
    exportador = capturado["exportador"]
    assert isinstance(exportador, tracing.ExceptionTextRedactingSpanExporter)
    assert exportador._delegate == "otlp"
