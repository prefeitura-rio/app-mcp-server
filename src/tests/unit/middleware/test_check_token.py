import importlib.util
import sys
import types
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException


PROJECT_ROOT = Path(__file__).resolve().parents[4]
MODULE_PATH = PROJECT_ROOT / "src" / "middleware" / "check_token.py"


def load_check_token_module(monkeypatch, valid_tokens):
    src_pkg = types.ModuleType("src")
    src_pkg.__path__ = [str(PROJECT_ROOT / "src")]
    config_pkg = types.ModuleType("src.config")
    config_pkg.__path__ = [str(PROJECT_ROOT / "src" / "config")]
    env_module = types.ModuleType("src.config.env")
    env_module.VALID_TOKENS = valid_tokens

    monkeypatch.setitem(sys.modules, "src", src_pkg)
    monkeypatch.setitem(sys.modules, "src.config", config_pkg)
    monkeypatch.setitem(sys.modules, "src.config.env", env_module)

    spec = importlib.util.spec_from_file_location(
        "test_check_token_module", MODULE_PATH
    )
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def make_context(headers):
    request = SimpleNamespace(headers=headers)
    fastmcp_context = SimpleNamespace(get_http_request=lambda: request)
    return SimpleNamespace(fastmcp_context=fastmcp_context)


@pytest.mark.asyncio
async def test_check_token_rejects_missing_authorization(monkeypatch):
    module = load_check_token_module(monkeypatch, "abc123")
    middleware = module.CheckTokenMiddleware()

    with pytest.raises(HTTPException, match="não fornecido") as exc_info:
        await middleware.on_request(make_context({}), AsyncMock())

    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_check_token_rejects_invalid_bearer_format(monkeypatch):
    module = load_check_token_module(monkeypatch, "abc123")
    middleware = module.CheckTokenMiddleware()

    with pytest.raises(HTTPException, match="Formato de token inválido") as exc_info:
        await middleware.on_request(
            make_context({"Authorization": "Token abc123"}),
            AsyncMock(),
        )

    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_check_token_rejects_unknown_token(monkeypatch):
    module = load_check_token_module(monkeypatch, "abc123, def456")
    middleware = module.CheckTokenMiddleware()

    with pytest.raises(HTTPException, match="Token inválido") as exc_info:
        await middleware.on_request(
            make_context({"Authorization": "Bearer nope"}),
            AsyncMock(),
        )

    assert exc_info.value.status_code == 401


@pytest.mark.asyncio
async def test_check_token_accepts_comma_separated_string_tokens(monkeypatch):
    module = load_check_token_module(monkeypatch, "abc123, def456")
    middleware = module.CheckTokenMiddleware()
    call_next = AsyncMock(return_value={"ok": True})

    result = await middleware.on_request(
        make_context({"Authorization": "Bearer def456"}),
        call_next,
    )

    assert result == {"ok": True}
    call_next.assert_awaited_once()


@pytest.mark.asyncio
async def test_check_token_accepts_list_tokens(monkeypatch):
    module = load_check_token_module(monkeypatch, ["abc123", "def456"])
    middleware = module.CheckTokenMiddleware()
    call_next = AsyncMock(return_value="passed")

    result = await middleware.on_request(
        make_context({"Authorization": "Bearer abc123"}),
        call_next,
    )

    assert result == "passed"
    call_next.assert_awaited_once()
