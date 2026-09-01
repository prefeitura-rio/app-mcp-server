"""Métricas OpenTelemetry (OTel) de baixa cardinalidade para o servidor FastMCP.

Mesma postura defensiva de `src/observability/tracing.py`: 100% opt-in — se
`OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` não estiver configurado, ou qualquer
etapa da configuração falhar, métricas ficam desabilitadas e a aplicação
continua funcionando normalmente (nenhuma exceção propaga daqui).

Cardinalidade dos atributos, por instrumento — todos com um conjunto
FECHADO e pequeno de valores possíveis:

- `mcp.tool.name`: nome da tool chamada (~20 valores, os registrados em
  `src/app.py`; nunca o payload/argumentos da chamada).
- `status`: `"success"` ou `"error"` (2 valores).
- `dependency.name`: hoje só `"redis"`.

Nenhum atributo carrega `user_id`, CPF, ID de sessão, prompt do usuário, URL
ou texto de exceção — para isso já existe `/health/detail`
(`src/health/registry.py::sanitize_error`), que sanitiza antes de responder.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Iterator, Optional

from opentelemetry import metrics
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.metrics import Counter, Histogram, UpDownCounter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import MetricReader, PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource

import src.config.env as env
from src.utils.log import logger

_METER_NAME = "app-mcp-server"

_metrics_enabled = False
_setup_attempted = False

_tool_calls_total: Optional[Counter] = None
_tool_call_duration: Optional[Histogram] = None
_tool_calls_active: Optional[UpDownCounter] = None
_dependency_duration: Optional[Histogram] = None
_dependency_errors_total: Optional[Counter] = None


def is_metrics_enabled() -> bool:
    """Retorna True se as métricas OTel foram configuradas com sucesso."""
    return _metrics_enabled


def _activate(provider: MeterProvider) -> None:
    """(Re)cria os instrumentos a partir de `provider`. Ponto único usado
    tanto por `setup_metrics()` (produção, exportador OTLP/HTTP) quanto por
    `configure_for_test()` (leitor em memória) — os dois caminhos nunca
    divergem em nome/unidade/atributo de instrumento.

    Obtém o `Meter` diretamente de `provider.get_meter(...)`, e não via
    `opentelemetry.metrics.get_meter()` (API global): a API global só aceita
    UM `set_meter_provider()` por processo (chamadas seguintes viram no-op
    com warning), o que quebraria o isolamento entre testes — cada teste
    injeta seu próprio `MeterProvider`/`InMemoryMetricReader` e precisa que
    os instrumentos apontem exatamente para ele.
    """
    global _metrics_enabled
    global _tool_calls_total, _tool_call_duration, _tool_calls_active
    global _dependency_duration, _dependency_errors_total

    meter = provider.get_meter(_METER_NAME)

    _tool_calls_total = meter.create_counter(
        "mcp.tool.calls",
        unit="1",
        description="Total de chamadas de tool, por nome e status.",
    )
    _tool_call_duration = meter.create_histogram(
        "mcp.tool.call.duration",
        unit="s",
        description="Duração das chamadas de tool, por nome e status.",
    )
    _tool_calls_active = meter.create_up_down_counter(
        "mcp.tool.calls.active",
        unit="1",
        description="Chamadas de tool em andamento, por nome.",
    )
    _dependency_duration = meter.create_histogram(
        "mcp.dependency.call.duration",
        unit="s",
        description="Duração de chamadas a dependências internas (ex.: Redis).",
    )
    _dependency_errors_total = meter.create_counter(
        "mcp.dependency.errors",
        unit="1",
        description="Falhas de chamadas a dependências internas, por nome.",
    )
    _metrics_enabled = True


def setup_metrics() -> bool:
    """Configura o `MeterProvider` global com exportação OTLP/HTTP para o SigNoz.

    Idempotente e nunca levanta exceção, igual a `tracing.setup_tracing()`.
    Reaproveita o mesmo `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` de traces como
    base do endpoint de métricas (só o sufixo do path muda, de `/v1/traces`
    para `/v1/metrics`) — o coletor SigNoz aceita ambos os sinais na mesma
    porta 4318.

    Returns:
        bool: True se as métricas foram habilitadas com sucesso.
    """
    global _setup_attempted, _metrics_enabled

    if _setup_attempted:
        return _metrics_enabled
    _setup_attempted = True

    endpoint = env.OTEL_EXPORTER_OTLP_TRACES_ENDPOINT
    if not endpoint:
        logger.info(
            "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT não configurado. "
            "Métricas OpenTelemetry permanecerão DESABILITADAS."
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
        metrics_endpoint = endpoint.rstrip("/") + "/v1/metrics"
        otlp_exporter = OTLPMetricExporter(endpoint=metrics_endpoint, headers=headers)
        reader = PeriodicExportingMetricReader(
            otlp_exporter, export_interval_millis=15000
        )
        provider = MeterProvider(
            resource=Resource.create({"service.name": service_name}),
            metric_readers=[reader],
        )
        # Registro global best-effort: permite que outra instrumentação do
        # processo (ex.: futura auto-instrumentação ASGI) reaproveite este
        # provider via `opentelemetry.metrics.get_meter_provider()`. Não é
        # necessário para os instrumentos deste módulo — `_activate()` os
        # cria diretamente a partir de `provider`, então mesmo se este
        # registro global for no-op (outro provider já ativo no processo),
        # as métricas abaixo continuam funcionando.
        metrics.set_meter_provider(provider)
        _activate(provider)

        logger.info(
            f"Métricas OpenTelemetry habilitadas. service.name={service_name!r} "
            f"endpoint={metrics_endpoint!r}"
        )
        return True
    except Exception as e:
        logger.warning(
            f"Falha ao configurar métricas OpenTelemetry: {e}. Métricas "
            "permanecerão DESABILITADAS, mas a aplicação continuará "
            "funcionando normalmente."
        )
        _metrics_enabled = False
        return False


def configure_for_test(reader: MetricReader) -> MeterProvider:
    """Ativa métricas com um `MetricReader` informado (uso exclusivo de teste).

    Produção sempre passa por `setup_metrics()`; isto existe só para permitir
    inspecionar pontos de métrica (via `InMemoryMetricReader`) sem depender
    de rede nem esperar o intervalo do exportador periódico.
    """
    provider = MeterProvider(metric_readers=[reader])
    _activate(provider)
    return provider


def reset_for_tests() -> None:
    """Reseta o estado do módulo entre testes."""
    global _metrics_enabled, _setup_attempted
    global _tool_calls_total, _tool_call_duration, _tool_calls_active
    global _dependency_duration, _dependency_errors_total
    _metrics_enabled = False
    _setup_attempted = False
    _tool_calls_total = None
    _tool_call_duration = None
    _tool_calls_active = None
    _dependency_duration = None
    _dependency_errors_total = None


def record_tool_call(tool_name: str, *, success: bool, duration_s: float) -> None:
    """Registra uma chamada de tool concluída (contagem + duração).

    Lê os instrumentos para variáveis locais antes do `is None` check: os
    dois são `global`s `Optional[...]`, e só a checagem sobre a cópia local
    permite ao type checker estreitar (`narrow`) o tipo para não-opcional
    dentro da função — checar a global diretamente não tem essa garantia.
    """
    counter, histogram = _tool_calls_total, _tool_call_duration
    if counter is None or histogram is None:
        return
    attrs = {"mcp.tool.name": tool_name, "status": "success" if success else "error"}
    counter.add(1, attrs)
    histogram.record(duration_s, attrs)


@contextmanager
def track_active_tool_call(tool_name: str) -> Iterator[None]:
    """Contabiliza `tool_name` como em andamento durante o bloco `with`."""
    counter = _tool_calls_active
    if counter is None:
        yield
        return
    attrs = {"mcp.tool.name": tool_name}
    counter.add(1, attrs)
    try:
        yield
    finally:
        counter.add(-1, attrs)


def record_dependency_call(
    dependency: str, *, success: bool, duration_s: float
) -> None:
    """Registra a duração/falha de uma chamada a uma dependência interna."""
    duration_histogram, error_counter = _dependency_duration, _dependency_errors_total
    if duration_histogram is None or error_counter is None:
        return
    status = "success" if success else "error"
    duration_histogram.record(
        duration_s, {"dependency.name": dependency, "status": status}
    )
    if not success:
        error_counter.add(1, {"dependency.name": dependency})
