"""Testes de `src/observability/metrics.py`.

Duas garantias centrais:

1. Sem configuração (`OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` ausente, ou antes
   de qualquer setup), gravar métricas é um no-op seguro — nunca levanta.
2. Todo ponto de métrica emitido carrega só atributos de um conjunto FECHADO
   e pequeno de valores (nome de tool/dependência conhecidos, status
   success/error) — nunca `user_id`, CPF, sessão, prompt, URL ou texto de
   exceção sem limite.
"""

import pytest
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from src.observability import metrics


@pytest.fixture(autouse=True)
def _reset_metrics():
    metrics.reset_for_tests()
    yield
    metrics.reset_for_tests()


def _data_points(reader: InMemoryMetricReader, metric_name: str) -> list:
    points = []
    data = reader.get_metrics_data()
    if data is None:
        return points
    for resource_metrics in data.resource_metrics:
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                if metric.name == metric_name:
                    points.extend(metric.data.data_points)
    return points


def test_gravar_sem_configuracao_nao_levanta():
    metrics.record_tool_call("calculator_add", success=True, duration_s=0.01)
    metrics.record_dependency_call("redis", success=False, duration_s=0.01)

    with metrics.track_active_tool_call("calculator_add"):
        pass

    assert metrics.is_metrics_enabled() is False


def test_setup_sem_endpoint_fica_desabilitado(monkeypatch):
    import types
    import sys

    env = types.SimpleNamespace(
        OTEL_EXPORTER_OTLP_TRACES_ENDPOINT="",
        OTEL_EXPORTER_OTLP_TRACES_HEADERS="",
        OTEL_SERVICE_NAME="app-mcp-server",
    )
    monkeypatch.setattr(sys.modules["src.config"], "env", env, raising=False)
    monkeypatch.setattr(metrics, "env", env)

    assert metrics.setup_metrics() is False
    assert metrics.is_metrics_enabled() is False


def test_chamada_de_tool_bem_sucedida_gera_contagem_e_duracao():
    reader = InMemoryMetricReader()
    metrics.configure_for_test(reader)

    metrics.record_tool_call("calculator_add", success=True, duration_s=0.25)

    calls = _data_points(reader, "mcp.tool.calls")
    durations = _data_points(reader, "mcp.tool.call.duration")

    assert len(calls) == 1
    assert calls[0].attributes == {
        "mcp.tool.name": "calculator_add",
        "status": "success",
    }
    assert calls[0].value == 1

    assert len(durations) == 1
    assert durations[0].attributes == {
        "mcp.tool.name": "calculator_add",
        "status": "success",
    }
    assert durations[0].sum == pytest.approx(0.25)


def test_chamada_de_tool_com_erro_usa_status_error():
    reader = InMemoryMetricReader()
    metrics.configure_for_test(reader)

    metrics.record_tool_call("multi_step_service", success=False, duration_s=0.5)

    calls = _data_points(reader, "mcp.tool.calls")
    assert calls[0].attributes["status"] == "error"


def test_trabalho_ativo_incrementa_e_decrementa():
    reader = InMemoryMetricReader()
    metrics.configure_for_test(reader)

    with metrics.track_active_tool_call("google_search"):
        pass

    active = _data_points(reader, "mcp.tool.calls.active")
    # UpDownCounter: soma líquida de +1/-1 é 0 depois do bloco encerrado.
    assert active[0].value == 0
    assert active[0].attributes == {"mcp.tool.name": "google_search"}


def test_dependencia_saudavel_nao_incrementa_erro():
    reader = InMemoryMetricReader()
    metrics.configure_for_test(reader)

    metrics.record_dependency_call("redis", success=True, duration_s=0.01)

    duration_points = _data_points(reader, "mcp.dependency.call.duration")
    error_points = _data_points(reader, "mcp.dependency.errors")

    assert duration_points[0].attributes == {
        "dependency.name": "redis",
        "status": "success",
    }
    assert error_points == []


def test_dependencia_com_falha_incrementa_contador_de_erro_sem_texto_de_excecao():
    reader = InMemoryMetricReader()
    metrics.configure_for_test(reader)

    metrics.record_dependency_call("redis", success=False, duration_s=2.0)

    error_points = _data_points(reader, "mcp.dependency.errors")
    assert len(error_points) == 1
    # Único atributo é o nome (fechado) da dependência — nada de mensagem de
    # exceção, host ou credencial.
    assert error_points[0].attributes == {"dependency.name": "redis"}
    assert error_points[0].value == 1


def test_nenhum_atributo_emitido_carrega_valor_sensivel_ou_de_alta_cardinalidade():
    """Simula um cenário realista (várias tools, uma com "erro" contendo o
    tipo de dado que NUNCA deve aparecer em atributo) e varre todos os
    pontos emitidos em busca de vazamento."""
    reader = InMemoryMetricReader()
    metrics.configure_for_test(reader)

    forbidden_substrings = [
        "5521999999999",  # telefone/user_id
        "111.111.111-11",  # CPF
        "session-abc123",  # id de sessão
        "ignore previous instructions",  # prompt bruto / injeção
        "http://",  # URL
        "https://",
        "senha-secreta",
    ]

    metrics.record_tool_call("get_user_memory", success=True, duration_s=0.1)
    metrics.record_tool_call("multi_step_service", success=False, duration_s=1.2)
    metrics.record_dependency_call("redis", success=False, duration_s=3.0)
    with metrics.track_active_tool_call("equipments_by_address"):
        pass

    for metric_name in (
        "mcp.tool.calls",
        "mcp.tool.call.duration",
        "mcp.tool.calls.active",
        "mcp.dependency.call.duration",
        "mcp.dependency.errors",
    ):
        for point in _data_points(reader, metric_name):
            for key, value in point.attributes.items():
                assert key in {"mcp.tool.name", "status", "dependency.name"}
                text = str(value)
                for forbidden in forbidden_substrings:
                    assert forbidden not in text
