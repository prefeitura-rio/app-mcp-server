"""Instrumentação de OpenTelemetry (OTel) para o servidor FastMCP.

Configura um `TracerProvider` global exportando spans via OTLP/HTTP para o
coletor do SigNoz, e expõe um middleware do FastMCP (`ToolCallTracingMiddleware`)
que gera um span por chamada de tool com atributos de nome, usuário e
sucesso/falha.

Este módulo é 100% opt-in e defensivo: se `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`
não estiver configurado, ou se qualquer etapa da configuração falhar, o
tracing fica desabilitado e a aplicação continua funcionando normalmente
(nenhuma exceção deve propagar daqui para o restante da app).
"""

from __future__ import annotations

from typing import Any

from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from mcp.types import CallToolRequestParams
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Status, StatusCode

import src.config.env as env
from src.utils.log import logger

# Nome do tracer usado em toda a aplicação para spans de tool call.
_TRACER_NAME = "app-mcp-server"

# Flag de módulo para tornar `setup_tracing()` idempotente e indicar,
# para o resto da aplicação (ex.: `main.py`), se o tracing está ativo.
_tracing_enabled = False
_setup_attempted = False


def is_tracing_enabled() -> bool:
    """Retorna True se o tracing OTel foi configurado com sucesso."""
    return _tracing_enabled


def setup_tracing() -> bool:
    """Configura o `TracerProvider` global com exportação OTLP/HTTP para o SigNoz.

    Idempotente: chamadas subsequentes são no-op e retornam o resultado da
    primeira tentativa. Nunca levanta exceção — qualquer falha é logada e
    resulta em tracing desabilitado, preservando o funcionamento normal da
    aplicação (comportamento idêntico ao estado anterior sem tracing).

    Returns:
        bool: True se o tracing foi habilitado com sucesso, False caso
            contrário (endpoint não configurado ou falha na configuração).
    """
    global _tracing_enabled, _setup_attempted

    if _setup_attempted:
        return _tracing_enabled
    _setup_attempted = True

    endpoint = env.OTEL_EXPORTER_OTLP_TRACES_ENDPOINT
    if not endpoint:
        logger.info(
            "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT não configurado. "
            "Tracing OpenTelemetry permanecerá DESABILITADO."
        )
        return False

    try:
        headers_raw = env.OTEL_EXPORTER_OTLP_TRACES_HEADERS
        headers = (
            dict(
                header.split("=", 1)
                for header in headers_raw.split(",")
                if "=" in header
            )
            if headers_raw
            else None
        )

        service_name = env.OTEL_SERVICE_NAME or "app-mcp-server"
        provider = TracerProvider(
            resource=Resource.create({"service.name": service_name})
        )

        # O endpoint configurado é a URL base do coletor (ex.:
        # "http://signoz-otel-collector.signoz.svc.cluster.local:4318");
        # o path "/v1/traces" é adicionado explicitamente aqui em vez de
        # depender do comportamento de auto-sufixação do SDK.
        traces_endpoint = endpoint.rstrip("/") + "/v1/traces"
        otlp_exporter = OTLPSpanExporter(endpoint=traces_endpoint, headers=headers)

        batch_processor = BatchSpanProcessor(
            otlp_exporter,
            max_queue_size=8192,
            schedule_delay_millis=1000,
            export_timeout_millis=10000,
            max_export_batch_size=256,
        )
        provider.add_span_processor(batch_processor)
        trace.set_tracer_provider(provider)

        _tracing_enabled = True
        logger.info(
            f"Tracing OpenTelemetry habilitado. service.name={service_name!r} "
            f"endpoint={traces_endpoint!r}"
        )
        return True
    except Exception as e:
        logger.warning(
            f"Falha ao configurar tracing OpenTelemetry: {e}. "
            "Tracing permanecerá DESABILITADO, mas a aplicação continuará "
            "funcionando normalmente."
        )
        _tracing_enabled = False
        return False


def get_tracer() -> trace.Tracer:
    """Retorna o tracer nomeado usado para instrumentação manual."""
    return trace.get_tracer(_TRACER_NAME)


def _extract_user_id(arguments: dict[str, Any] | None) -> str:
    """Extrai `user_id` dos argumentos de uma chamada de tool, com fallback.

    Segue a mesma convenção usada em `src/utils/error_interceptor.py`
    (`extract_user_id` lambdas): tenta a chave `user_id` explicitamente e
    cai para `"unknown"` se ausente.
    """
    if not arguments:
        return "unknown"
    return str(arguments.get("user_id") or "unknown")


class ToolCallTracingMiddleware(Middleware):
    """Middleware do FastMCP que gera um span por chamada de tool.

    Atributos registrados no span:
        - `mcp.tool.name`: nome da tool chamada.
        - `mcp.tool.user_id`: melhor esforço de extração do usuário (ou
          "unknown" se não disponível).
        - `mcp.tool.success`: True/False conforme o resultado da chamada.

    Se o tracing não foi habilitado (`setup_tracing()` nunca retornou True),
    este middleware é um passthrough e não adiciona overhead relevante.
    """

    async def on_call_tool(
        self,
        context: MiddlewareContext[CallToolRequestParams],
        call_next: CallNext[CallToolRequestParams, Any],
    ) -> Any:
        if not _tracing_enabled:
            return await call_next(context)

        tool_name = context.message.name
        user_id = _extract_user_id(context.message.arguments)

        tracer = get_tracer()
        with tracer.start_as_current_span("mcp.tool_call") as span:
            span.set_attribute("mcp.tool.name", tool_name)
            span.set_attribute("mcp.tool.user_id", user_id)
            try:
                result = await call_next(context)
                span.set_attribute("mcp.tool.success", True)
                span.set_status(Status(StatusCode.OK))
                return result
            except Exception as e:
                span.set_attribute("mcp.tool.success", False)
                span.record_exception(e)
                span.set_status(Status(StatusCode.ERROR, str(e)))
                raise
