import runpy
import sys
import types
from pathlib import Path
from unittest.mock import Mock


PROJECT_ROOT = Path(__file__).resolve().parents[4]
MAIN_PATH = PROJECT_ROOT / "src" / "main.py"

# Sentinela: o conteúdo não importa, só que seja exatamente este objeto que
# chega a `mcp.run(middleware=...)`.
MIDDLEWARE_MONTADO = ["middleware-montado"]

PORTA_DO_STUB = 18080


def run_main_with_env(monkeypatch, is_local: bool):
    app_module = types.ModuleType("src.app")
    app_module.mcp = Mock()
    # `build_http_middleware()` monta a exigência de autenticação e o teto de
    # corpo. Aqui ele é um Mock com valor sentinela só para verificar que
    # `main.py` de fato repassa o que ele devolve — o comportamento do
    # middleware em si é testado em `test_http_auth_coverage.py`.
    app_module.build_http_middleware = Mock(return_value=MIDDLEWARE_MONTADO)

    env_module = types.ModuleType("src.config.env")
    env_module.IS_LOCAL = is_local
    env_module.MCP_STATELESS_HTTP = not is_local
    # Valor arbitrário de propósito: o que se testa aqui é que `main.py`
    # repassa `env.SERVER_PORT`, não qual porta ele escolhe. A porta efetiva
    # bater com a do manifesto é assunto de `test_porta_privilegiada.py`.
    env_module.SERVER_PORT = PORTA_DO_STUB

    src_pkg = types.ModuleType("src")
    src_pkg.__path__ = [str(PROJECT_ROOT / "src")]
    config_pkg = types.ModuleType("src.config")
    config_pkg.__path__ = [str(PROJECT_ROOT / "src" / "config")]
    observability_pkg = types.ModuleType("src.observability")
    observability_pkg.__path__ = [str(PROJECT_ROOT / "src" / "observability")]
    tracing_module = types.ModuleType("src.observability.tracing")
    tracing_module.is_tracing_enabled = Mock(return_value=False)
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

    return app_module, preflight_module.run_startup_preflight


def test_main_runs_default_transport_locally(monkeypatch):
    app_module, _ = run_main_with_env(monkeypatch, is_local=True)

    app_module.mcp.run.assert_called_once_with()


def test_main_runs_streamable_http_when_not_local(monkeypatch):
    app_module, _ = run_main_with_env(monkeypatch, is_local=False)

    app_module.mcp.run.assert_called_once_with(
        transport="streamable-http",
        host="0.0.0.0",
        port=PORTA_DO_STUB,
        path="/mcp",
        middleware=MIDDLEWARE_MONTADO,
        stateless_http=True,
    )


def test_main_serve_com_o_middleware_de_autenticacao(monkeypatch):
    """`main.py` precisa servir o que `build_http_middleware()` devolve.

    Passar `middleware=None` aqui (como era antes) devolve as rotas de
    `custom_route` ao estado em que atendiam sem token nenhum. É uma linha só
    de diferença, e nada mais no CI perceberia.
    """
    app_module, _ = run_main_with_env(monkeypatch, is_local=False)

    app_module.build_http_middleware.assert_called_once_with(None)
    _, kwargs = app_module.mcp.run.call_args
    assert kwargs["middleware"] is MIDDLEWARE_MONTADO


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
