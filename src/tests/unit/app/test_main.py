import runpy
import sys
import types
from pathlib import Path
from unittest.mock import Mock

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
    InMemorySpanExporter,
)
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient


PROJECT_ROOT = Path(__file__).resolve().parents[4]
MAIN_PATH = PROJECT_ROOT / "src" / "main.py"


def run_main_with_env(monkeypatch, is_local: bool, tracing: bool = False):
    app_module = types.ModuleType("src.app")
    app_module.mcp = Mock()

    env_module = types.ModuleType("src.config.env")
    env_module.IS_LOCAL = is_local
    env_module.MCP_STATELESS_HTTP = not is_local

    src_pkg = types.ModuleType("src")
    src_pkg.__path__ = [str(PROJECT_ROOT / "src")]
    config_pkg = types.ModuleType("src.config")
    config_pkg.__path__ = [str(PROJECT_ROOT / "src" / "config")]
    observability_pkg = types.ModuleType("src.observability")
    observability_pkg.__path__ = [str(PROJECT_ROOT / "src" / "observability")]
    tracing_module = types.ModuleType("src.observability.tracing")
    tracing_module.is_tracing_enabled = Mock(return_value=tracing)
    health_pkg = types.ModuleType("src.health")
    health_pkg.__path__ = [str(PROJECT_ROOT / "src" / "health")]
    preflight_module = types.ModuleType("src.health.preflight")
    preflight_module.run_startup_preflight = Mock()

    monkeypatch.setitem(sys.modules, "src", src_pkg)
    monkeypatch.setitem(sys.modules, "src.app", app_module)
    monkeypatch.setitem(sys.modules, "src.config", config_pkg)
    monkeypatch.setitem(sys.modules, "src.config.env", env_module)
    monkeypatch.setitem(sys.modules, "src.observability", observability_pkg)
    monkeypatch.setitem(sys.modules, "src.observability.tracing", tracing_module)
    monkeypatch.setitem(sys.modules, "src.health", health_pkg)
    monkeypatch.setitem(sys.modules, "src.health.preflight", preflight_module)

    runpy.run_path(str(MAIN_PATH), run_name="__main__")

    return app_module.mcp, preflight_module.run_startup_preflight


def test_main_runs_default_transport_locally(monkeypatch):
    mcp, _ = run_main_with_env(monkeypatch, is_local=True)

    mcp.run.assert_called_once_with()


def test_main_runs_streamable_http_when_not_local(monkeypatch):
    mcp, _ = run_main_with_env(monkeypatch, is_local=False)

    mcp.run.assert_called_once()
    kwargs = mcp.run.call_args.kwargs
    middleware = kwargs.pop("middleware")
    assert kwargs == dict(
        transport="streamable-http",
        host="0.0.0.0",
        port=80,
        path="/mcp",
        stateless_http=True,
    )
    # Fora do modo local, as rotas HTTP próprias ganham o guard que nega por
    # padrão (sem tracing, é o único middleware).
    assert [m.cls.__name__ for m in middleware] == ["RequireAuthenticatedByDefault"]


def test_com_tracing_o_401_do_guard_tambem_gera_span(monkeypatch):
    """A pilha que o `main.py` monta com o tracing ligado: o middleware do OTel
    fica por fora do guard, então a requisição recusada também vira span."""
    mcp, _ = run_main_with_env(monkeypatch, is_local=False, tracing=True)
    middleware = mcp.run.call_args.kwargs["middleware"]
    assert [m.cls.__name__ for m in middleware] == [
        "OpenTelemetryMiddleware",
        "RequireAuthenticatedByDefault",
    ]

    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    # Mesma pilha, só com o provider de teste no lugar do global.
    otel, guard = middleware
    pilha = [
        Middleware(otel.cls, *otel.args, **otel.kwargs, tracer_provider=provider),
        guard,
    ]
    chamadas = []

    async def rota(request):
        chamadas.append(request.url.path)
        return JSONResponse({"ok": True})

    app = Starlette(routes=[Route("/rota_nova", rota)], middleware=pilha)

    response = TestClient(app).get("/rota_nova")

    assert response.status_code == 401
    assert chamadas == []
    spans = exporter.get_finished_spans()
    assert spans, "o 401 do guard não gerou span"
    status = [
        span.attributes.get("http.status_code")
        or span.attributes.get("http.response.status_code")
        for span in spans
    ]
    assert 401 in status


def test_main_runs_config_preflight(monkeypatch):
    _, preflight = run_main_with_env(monkeypatch, is_local=False)

    preflight.assert_called_once_with()


def test_preflight_precede_o_import_da_aplicacao():
    """O preflight precisa rodar antes de `src.app` ser importado.

    `src.app` importa `src.config.env`, que aborta na primeira variável
    faltante — o que anularia o propósito de reportar todas de uma vez. A
    ordem é frágil justamente porque parece um import fora de lugar: este
    teste impede que alguém "conserte" o E402 movendo os imports para o topo.
    """
    source = MAIN_PATH.read_text(encoding="utf-8")

    chamada_preflight = source.index("run_startup_preflight()")
    import_da_app = source.index("from src.app import")

    assert chamada_preflight < import_da_app
