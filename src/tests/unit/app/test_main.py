import runpy
import sys
import types
from pathlib import Path
from unittest.mock import Mock


PROJECT_ROOT = Path(__file__).resolve().parents[4]
MAIN_PATH = PROJECT_ROOT / "src" / "main.py"


def run_main_with_env(monkeypatch, is_local: bool):
    app_module = types.ModuleType("src.app")
    app_module.mcp = Mock()

    env_module = types.ModuleType("src.config.env")
    env_module.IS_LOCAL = is_local

    src_pkg = types.ModuleType("src")
    src_pkg.__path__ = [str(PROJECT_ROOT / "src")]
    config_pkg = types.ModuleType("src.config")
    config_pkg.__path__ = [str(PROJECT_ROOT / "src" / "config")]

    monkeypatch.setitem(sys.modules, "src", src_pkg)
    monkeypatch.setitem(sys.modules, "src.app", app_module)
    monkeypatch.setitem(sys.modules, "src.config", config_pkg)
    monkeypatch.setitem(sys.modules, "src.config.env", env_module)

    runpy.run_path(str(MAIN_PATH), run_name="__main__")

    return app_module.mcp


def test_main_runs_default_transport_locally(monkeypatch):
    mcp = run_main_with_env(monkeypatch, is_local=True)

    mcp.run.assert_called_once_with()


def test_main_runs_streamable_http_when_not_local(monkeypatch):
    mcp = run_main_with_env(monkeypatch, is_local=False)

    mcp.run.assert_called_once_with(
        transport="streamable-http",
        host="0.0.0.0",
        port=80,
        path="/mcp",
    )
