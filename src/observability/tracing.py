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

import asyncio
import contextvars
import time
from concurrent.futures import Executor, Future
from contextlib import contextmanager, nullcontext
from typing import Any, Callable, Generator, TypeVar

from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext
from mcp.types import CallToolRequestParams
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace import Status, StatusCode

import src.config.env as env
from src.observability import metrics
from src.utils.log import logger

# Nome do tracer usado em toda a aplicação para spans de tool call.
_TRACER_NAME = "app-mcp-server"

_T = TypeVar("_T")

# Flag de módulo para tornar `setup_tracing()` idempotente e indicar,
# para o resto da aplicação (ex.: `main.py`), se o tracing está ativo.
_tracing_enabled = False
_setup_attempted = False


def is_tracing_enabled() -> bool:
    """Retorna True se o tracing OTel foi configurado com sucesso."""
    return _tracing_enabled


def _build_resource_attributes(service_name: str) -> dict[str, str]:
    """Monta os atributos de Resource comuns a todos os spans do processo.

    `service.name` sozinho não distingue staging de prod: os dois publicam com
    o mesmo valor e um alerta de taxa de erro acabaria avaliando os dois
    ambientes num stream só. `deployment.environment` é o que separa os dois;
    `k8s.pod.name` é o que permite atribuir um pico a uma réplica específica.

    Atributo vazio não é registrado: um `deployment.environment=""` no SigNoz
    é pior que a ausência, porque parece um valor legítimo na hora de filtrar.
    """
    attributes = {"service.name": service_name}

    environment = (getattr(env, "ENVIRONMENT", None) or "").strip()
    if environment:
        attributes["deployment.environment"] = environment

    # Vem da downward API (`fieldRef: metadata.name`) nos manifests; fora do
    # cluster simplesmente não existe e o atributo é omitido.
    pod_name = (getattr(env, "K8S_POD_NAME", None) or "").strip()
    if pod_name:
        attributes["k8s.pod.name"] = pod_name

    return attributes


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
        resource_attributes = _build_resource_attributes(service_name)
        provider = TracerProvider(resource=Resource.create(resource_attributes))

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
        # `deployment.environment` entra no log de propósito: é ele que separa
        # staging de prod nos alertas, e um valor errado (o default de
        # `env.ENVIRONMENT`, por exemplo) só é detectável olhando o pod.
        logger.info(
            f"Tracing OpenTelemetry habilitado. service.name={service_name!r} "
            f"deployment.environment="
            f"{resource_attributes.get('deployment.environment')!r} "
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


def mark_span_error(span: trace.Span, erro: BaseException) -> None:
    """Marca um span de instrumentação manual como erro sem texto de exceção.

    Só o nome da classe vai para o span, no atributo semântico padrão do OTel
    (`error.type`) — nunca `record_exception`/`str(erro)`, que embutiriam a
    mensagem da exceção (que pode carregar corpo de resposta de um terceiro,
    por exemplo) no backend de trace. Por isso os spans que chamam esta
    função devem ser abertos com `record_exception=False,
    set_status_on_exception=False`: sem isso, o `__exit__` do próprio span
    registraria a mensagem por conta própria ao ver a exceção propagar.
    """
    span.set_attribute("error.type", type(erro).__name__)
    span.set_status(Status(StatusCode.ERROR))


@contextmanager
def traced_stage(name: str) -> Generator[trace.Span, None, None]:
    """Span de instrumentação manual para um estágio interno "tudo ou nada".

    Só serve estágios em que exceção propagada == falha e retorno normal ==
    sucesso — nunca um estágio que capture a própria exceção e ainda assim
    retorne (aí a marcação de erro precisa ser manual; ver `mark_span_error`),
    porque `Status(OK)`, ao final deste gerenciador, sobrescreveria um
    `Status(ERROR)` já setado por quem chamou.
    """
    with get_tracer().start_as_current_span(
        name, record_exception=False, set_status_on_exception=False
    ) as span:
        try:
            yield span
        except Exception as erro:
            mark_span_error(span, erro)
            raise
        else:
            span.set_status(Status(StatusCode.OK))


def run_in_executor_with_context(
    loop: asyncio.AbstractEventLoop,
    executor: Executor | None,
    func: Callable[..., _T],
    *args: Any,
) -> Future[_T]:
    """`loop.run_in_executor` que leva o contexto OTel junto para a thread.

    O span corrente do OpenTelemetry vive num `contextvar`, e
    `run_in_executor` executa a função numa thread do pool sem copiar o
    contexto do chamador. O efeito é que todo span aberto dentro do executor
    (`bigquery.query`, `bigquery.save_response`, …) nascia como raiz de um
    trace próprio em vez de filho do `mcp.tool_call` que o originou: os spans
    existiam, com duração e status, mas não havia como ir da tool lenta até a
    query que a segurou.

    `contextvars.copy_context()` tira um retrato do contexto aqui, do lado do
    event loop, e `ctx.run(...)` o restaura dentro da thread — que é
    exatamente o que `asyncio.to_thread` faz. Não usamos `to_thread` direto
    porque ele não aceita escolher o executor, e as leituras de BigQuery
    rodam num pool dedicado (ver `_get_read_executor`).
    """
    ctx = contextvars.copy_context()
    return loop.run_in_executor(executor, lambda: ctx.run(func, *args))


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
    """Middleware do FastMCP que instrumenta cada chamada de tool.

    Dois sinais independentes, cada um opt-in por si só (um pode estar
    habilitado sem o outro, dependendo do que `setup_tracing()`/
    `setup_metrics()` conseguiram configurar):

    - Trace: um span `mcp.tool_call` com atributos de nome, usuário
      (melhor esforço; ver `_extract_user_id`) e sucesso/falha.
    - Métricas de baixa cardinalidade (`src.observability.metrics`):
      contagem, duração e trabalho-em-andamento por `mcp.tool.name` +
      `status`, e latência/falha de dependência quando aplicável — nunca o
      `user_id` ou texto de exceção, que ficam só no span (consumido em
      ambiente de trace, não em séries temporais/dashboards agregados).

    Se nem tracing nem métricas estiverem habilitados, este middleware é um
    passthrough sem overhead relevante.
    """

    async def on_call_tool(
        self,
        context: MiddlewareContext[CallToolRequestParams],
        call_next: CallNext[CallToolRequestParams, Any],
    ) -> Any:
        if not _tracing_enabled and not metrics.is_metrics_enabled():
            return await call_next(context)

        tool_name = context.message.name
        started = time.monotonic()

        span_context = (
            get_tracer().start_as_current_span("mcp.tool_call")
            if _tracing_enabled
            else nullcontext()
        )

        with metrics.track_active_tool_call(tool_name), span_context as span:
            if span is not None:
                user_id = _extract_user_id(context.message.arguments)
                span.set_attribute("mcp.tool.name", tool_name)
                span.set_attribute("mcp.tool.user_id", user_id)

            try:
                result = await call_next(context)
                if span is not None:
                    span.set_attribute("mcp.tool.success", True)
                    span.set_status(Status(StatusCode.OK))
                metrics.record_tool_call(
                    tool_name, success=True, duration_s=time.monotonic() - started
                )
                return result
            except Exception as e:
                if span is not None:
                    span.set_attribute("mcp.tool.success", False)
                    span.record_exception(e)
                    span.set_status(Status(StatusCode.ERROR, str(e)))
                metrics.record_tool_call(
                    tool_name, success=False, duration_s=time.monotonic() - started
                )
                raise
