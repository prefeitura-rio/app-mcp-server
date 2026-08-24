"""Testes de `src/observability/tracing.py`.

O módulo era coberto apenas de forma indireta por `test_main.py`, que mocka
`src.observability.tracing` inteiro — nem `setup_tracing()` nem o middleware
tinham cobertura real. Como o alerta de taxa de erro depende dos atributos de
Resource montados aqui, e a correlação entre tool call e query depende do
`run_in_executor_with_context`, os dois precisam de teste próprio.
"""

import asyncio
import contextvars

import pytest

import src.observability.tracing as tracing


@pytest.fixture(autouse=True)
def _reset_estado_do_modulo(monkeypatch):
    """`setup_tracing()` é idempotente por flags de módulo — zera entre testes."""
    monkeypatch.setattr(tracing, "_tracing_enabled", False)
    monkeypatch.setattr(tracing, "_setup_attempted", False)


class _SpanFalso:
    def __init__(self):
        self.attrs = {}
        self.status = None
        self.exceptions = []

    def set_attribute(self, key, value):
        self.attrs[key] = value

    def set_status(self, status):
        self.status = status

    def record_exception(self, exc):
        self.exceptions.append(exc)

    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


class _TracerFalso:
    def __init__(self):
        self.spans = {}

    def start_as_current_span(self, name):
        span = _SpanFalso()
        self.spans.setdefault(name, []).append(span)
        return span


class _ResourceFalso:
    """Captura os atributos entregues a `Resource.create`."""

    ultima_chamada = None

    @staticmethod
    def create(attributes):
        _ResourceFalso.ultima_chamada = dict(attributes)
        return object()


def _neutralizar_sdk(monkeypatch):
    """Troca o SDK real por dublês: o teste não deve abrir socket nem exportar."""
    _ResourceFalso.ultima_chamada = None
    monkeypatch.setattr(tracing, "Resource", _ResourceFalso)
    monkeypatch.setattr(
        tracing, "OTLPSpanExporter", lambda **kwargs: ("exporter", kwargs)
    )
    monkeypatch.setattr(tracing, "BatchSpanProcessor", lambda *a, **k: ("processor",))

    class _ProviderFalso:
        def __init__(self, resource=None):
            self.resource = resource
            self.processors = []

        def add_span_processor(self, processor):
            self.processors.append(processor)

    monkeypatch.setattr(tracing, "TracerProvider", _ProviderFalso)
    monkeypatch.setattr(tracing.trace, "set_tracer_provider", lambda _p: None)


def _configurar_env(monkeypatch, **valores):
    padrao = {
        "OTEL_EXPORTER_OTLP_TRACES_ENDPOINT": "http://collector:4318",
        "OTEL_EXPORTER_OTLP_TRACES_HEADERS": "",
        "OTEL_SERVICE_NAME": "app-mcp-server",
        "ENVIRONMENT": "staging",
        "K8S_POD_NAME": "mcp-abc123",
    }
    padrao.update(valores)
    for nome, valor in padrao.items():
        monkeypatch.setattr(tracing.env, nome, valor, raising=False)


# ---------------------------------------------------------------------------
# Atributos de Resource
# ---------------------------------------------------------------------------


def test_resource_separa_ambiente_e_pod():
    """Sem `deployment.environment`, staging e prod caem no mesmo stream.

    É a diferença entre um alerta de taxa de erro que aponta o ambiente certo
    e um que mistura os dois — e nasce disparando por causa de staging.
    """
    atributos = tracing._build_resource_attributes("app-mcp-server")

    assert atributos["service.name"] == "app-mcp-server"
    assert atributos["deployment.environment"]
    assert "k8s.pod.name" not in atributos or atributos["k8s.pod.name"]


def test_resource_usa_environment_e_pod_do_env(monkeypatch):
    monkeypatch.setattr(tracing.env, "ENVIRONMENT", "prod", raising=False)
    monkeypatch.setattr(tracing.env, "K8S_POD_NAME", "mcp-7d9f-x2", raising=False)

    atributos = tracing._build_resource_attributes("app-mcp-server")

    assert atributos["deployment.environment"] == "prod"
    assert atributos["k8s.pod.name"] == "mcp-7d9f-x2"


def test_resource_omite_atributo_vazio(monkeypatch):
    """Fora do cluster não há downward API — melhor ausente que string vazia.

    `k8s.pod.name=""` no SigNoz parece um valor legítimo na hora de filtrar.
    """
    monkeypatch.setattr(tracing.env, "ENVIRONMENT", "   ", raising=False)
    monkeypatch.setattr(tracing.env, "K8S_POD_NAME", "", raising=False)

    atributos = tracing._build_resource_attributes("app-mcp-server")

    assert atributos == {"service.name": "app-mcp-server"}


# ---------------------------------------------------------------------------
# setup_tracing
# ---------------------------------------------------------------------------


def test_setup_sem_endpoint_desabilita_sem_erro(monkeypatch):
    _configurar_env(monkeypatch, OTEL_EXPORTER_OTLP_TRACES_ENDPOINT="")

    assert tracing.setup_tracing() is False
    assert tracing.is_tracing_enabled() is False


def test_setup_publica_resource_completo(monkeypatch):
    _neutralizar_sdk(monkeypatch)
    _configurar_env(monkeypatch, ENVIRONMENT="prod", K8S_POD_NAME="mcp-1")

    assert tracing.setup_tracing() is True
    assert tracing.is_tracing_enabled() is True
    assert _ResourceFalso.ultima_chamada == {
        "service.name": "app-mcp-server",
        "deployment.environment": "prod",
        "k8s.pod.name": "mcp-1",
    }


def test_setup_acrescenta_o_path_de_traces_ao_endpoint(monkeypatch):
    capturado = {}

    _neutralizar_sdk(monkeypatch)
    monkeypatch.setattr(
        tracing, "OTLPSpanExporter", lambda **kwargs: capturado.update(kwargs)
    )
    _configurar_env(
        monkeypatch,
        OTEL_EXPORTER_OTLP_TRACES_ENDPOINT="http://collector:4318/",
        OTEL_EXPORTER_OTLP_TRACES_HEADERS="signoz-access-token=abc,ignorado",
    )

    assert tracing.setup_tracing() is True
    assert capturado["endpoint"] == "http://collector:4318/v1/traces"
    assert capturado["headers"] == {"signoz-access-token": "abc"}


def test_setup_e_idempotente(monkeypatch):
    chamadas = []

    _neutralizar_sdk(monkeypatch)
    monkeypatch.setattr(
        tracing.trace, "set_tracer_provider", lambda _p: chamadas.append(_p)
    )
    _configurar_env(monkeypatch)

    assert tracing.setup_tracing() is True
    assert tracing.setup_tracing() is True
    assert len(chamadas) == 1


def test_setup_engole_falha_e_mantem_a_app_de_pe(monkeypatch):
    """Tracing é opt-in: uma falha aqui não pode derrubar o servidor."""

    def _explode(**_kwargs):
        raise RuntimeError("coletor inalcançável")

    _neutralizar_sdk(monkeypatch)
    monkeypatch.setattr(tracing, "OTLPSpanExporter", _explode)
    _configurar_env(monkeypatch)

    assert tracing.setup_tracing() is False
    assert tracing.is_tracing_enabled() is False


# ---------------------------------------------------------------------------
# Propagação de contexto para o executor
# ---------------------------------------------------------------------------


_marcador = contextvars.ContextVar("marcador", default="ausente")


@pytest.mark.asyncio
async def test_run_in_executor_leva_o_contexto_para_a_thread():
    """É o que impede o span de BigQuery de virar raiz de trace própria.

    O contexto do OTel vive num `contextvar`; `run_in_executor` puro não o
    copia, e o span aberto na thread perdia o pai — a tool lenta deixava de
    apontar para a query que a segurou.
    """
    loop = asyncio.get_running_loop()
    _marcador.set("mcp.tool_call")

    visto = await tracing.run_in_executor_with_context(
        loop, None, lambda: _marcador.get()
    )

    assert visto == "mcp.tool_call"


@pytest.mark.asyncio
async def test_run_in_executor_repassa_argumentos_posicionais():
    loop = asyncio.get_running_loop()

    resultado = await tracing.run_in_executor_with_context(
        loop, None, lambda a, b: (a, b), "query", 42
    )

    assert resultado == ("query", 42)


@pytest.mark.asyncio
async def test_run_in_executor_propaga_excecao_da_thread():
    loop = asyncio.get_running_loop()

    def _explode():
        raise ValueError("falha na thread")

    with pytest.raises(ValueError, match="falha na thread"):
        await tracing.run_in_executor_with_context(loop, None, _explode)


# ---------------------------------------------------------------------------
# _extract_user_id
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argumentos,esperado",
    [
        (None, "unknown"),
        ({}, "unknown"),
        ({"user_id": ""}, "unknown"),
        ({"user_id": None}, "unknown"),
        ({"outro": "x"}, "unknown"),
        ({"user_id": "5521999999999"}, "5521999999999"),
        ({"user_id": 123}, "123"),
    ],
)
def test_extract_user_id(argumentos, esperado):
    assert tracing._extract_user_id(argumentos) == esperado


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


class _ContextoFalso:
    def __init__(self, name="equipments_instructions", arguments=None):
        self.message = type(
            "Mensagem", (), {"name": name, "arguments": arguments or {}}
        )()


@pytest.mark.asyncio
async def test_middleware_e_passthrough_com_tracing_desligado(monkeypatch):
    """Sem endpoint configurado o middleware não pode custar nada nem abrir span."""
    tracer = _TracerFalso()
    monkeypatch.setattr(tracing, "get_tracer", lambda: tracer)

    async def _call_next(_ctx):
        return "ok"

    resultado = await tracing.ToolCallTracingMiddleware().on_call_tool(
        _ContextoFalso(), _call_next
    )

    assert resultado == "ok"
    assert tracer.spans == {}


@pytest.mark.asyncio
async def test_middleware_registra_sucesso(monkeypatch):
    monkeypatch.setattr(tracing, "_tracing_enabled", True)
    tracer = _TracerFalso()
    monkeypatch.setattr(tracing, "get_tracer", lambda: tracer)

    async def _call_next(_ctx):
        return "ok"

    contexto = _ContextoFalso(
        name="equipments_instructions", arguments={"user_id": "5521999999999"}
    )
    resultado = await tracing.ToolCallTracingMiddleware().on_call_tool(
        contexto, _call_next
    )

    span = tracer.spans["mcp.tool_call"][0]
    assert resultado == "ok"
    assert span.attrs["mcp.tool.name"] == "equipments_instructions"
    assert span.attrs["mcp.tool.user_id"] == "5521999999999"
    assert span.attrs["mcp.tool.success"] is True
    assert span.status.status_code is tracing.StatusCode.OK


@pytest.mark.asyncio
async def test_middleware_marca_erro_e_reergue(monkeypatch):
    """A tool tem de continuar falhando como antes — o span é observação, não controle."""
    monkeypatch.setattr(tracing, "_tracing_enabled", True)
    tracer = _TracerFalso()
    monkeypatch.setattr(tracing, "get_tracer", lambda: tracer)

    erro = RuntimeError("tool quebrou")

    async def _call_next(_ctx):
        raise erro

    with pytest.raises(RuntimeError, match="tool quebrou"):
        await tracing.ToolCallTracingMiddleware().on_call_tool(
            _ContextoFalso(arguments=None), _call_next
        )

    span = tracer.spans["mcp.tool_call"][0]
    assert span.attrs["mcp.tool.success"] is False
    assert span.attrs["mcp.tool.user_id"] == "unknown"
    assert span.exceptions == [erro]
    assert span.status.status_code is tracing.StatusCode.ERROR


def test_get_tracer_usa_o_nome_do_servico():
    assert tracing.get_tracer() is not None
