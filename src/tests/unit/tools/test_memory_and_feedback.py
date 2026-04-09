import asyncio
import importlib.util
import sys
import types
from pathlib import Path

import httpx
import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[4]


def load_module(module_name: str, relative_path: str):
    spec = importlib.util.spec_from_file_location(
        module_name, PROJECT_ROOT / relative_path
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def ensure_package(name: str, path: Path):
    pkg = types.ModuleType(name)
    pkg.__path__ = [str(path)]
    sys.modules[name] = pkg
    return pkg


def passthrough_interceptor(*_args, **_kwargs):
    def decorator(func):
        return func

    return decorator


class FakeResponse:
    def __init__(self, payload=None, status_code=200):
        self._payload = payload or {}
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "error", request=types.SimpleNamespace(), response=self
            )


@pytest.mark.asyncio
async def test_memory_get_and_upsert(monkeypatch):
    ensure_package("src", PROJECT_ROOT / "src")
    ensure_package("src.tools", PROJECT_ROOT / "src" / "tools")
    ensure_package("src.utils", PROJECT_ROOT / "src" / "utils")
    ensure_package("src.config", PROJECT_ROOT / "src" / "config")

    env_module = types.SimpleNamespace(RMI_API_URL="https://rmi.example")
    monkeypatch.setitem(sys.modules, "src.config.env", env_module)
    monkeypatch.setitem(
        sys.modules, "src.config", types.SimpleNamespace(env=env_module)
    )
    monkeypatch.setitem(
        sys.modules,
        "src.utils.error_interceptor",
        types.SimpleNamespace(interceptor=passthrough_interceptor),
    )

    class FakeClient:
        def __init__(self):
            self.put_calls = []
            self.post_calls = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def get(self, url, headers=None):
            return FakeResponse([{"memory_name": "prefs"}])

        async def put(self, url, headers=None, json=None):
            self.put_calls.append((url, headers, json))
            return FakeResponse({"status": "updated"})

        async def post(self, url, headers=None, json=None):
            self.post_calls.append((url, headers, json))
            return FakeResponse({"status": "created"})

    fake_client = FakeClient()
    monkeypatch.setitem(
        sys.modules,
        "src.utils.http_client",
        types.SimpleNamespace(InterceptedHTTPClient=lambda **kwargs: fake_client),
    )
    monkeypatch.setitem(
        sys.modules,
        "src.utils.rmi_oauth2",
        types.SimpleNamespace(
            get_authorization_header=lambda: asyncio.sleep(0, result="Bearer abc"),
            is_oauth2_configured=lambda: True,
        ),
    )

    module = load_module("test_memory_module", "src/tools/memory.py")

    result = await module.get_memories("u1")
    assert result == [{"memory_name": "prefs"}]

    result = await module.get_memories("u1", "")
    assert result == [{"memory_name": "prefs"}]

    result = await module.upsert_memory(
        "u1",
        {
            "memory_name": "prefs",
            "description": "descr",
            "relevance": "high",
            "memory_type": "base",
            "value": "123",
        },
    )
    assert result == {"status": "updated"}
    assert fake_client.put_calls[0][2]["memory_name"] == "prefs"

    result = await module.upsert_memory("u1", {"invalid": True})
    assert result["status"] == "Error"


@pytest.mark.asyncio
async def test_memory_unauthorized_and_create_on_404(monkeypatch):
    ensure_package("src", PROJECT_ROOT / "src")
    ensure_package("src.tools", PROJECT_ROOT / "src" / "tools")
    ensure_package("src.utils", PROJECT_ROOT / "src" / "utils")
    ensure_package("src.config", PROJECT_ROOT / "src" / "config")

    env_module = types.SimpleNamespace(RMI_API_URL="https://rmi.example")
    monkeypatch.setitem(sys.modules, "src.config.env", env_module)
    monkeypatch.setitem(
        sys.modules, "src.config", types.SimpleNamespace(env=env_module)
    )
    monkeypatch.setitem(
        sys.modules,
        "src.utils.error_interceptor",
        types.SimpleNamespace(interceptor=passthrough_interceptor),
    )
    monkeypatch.setitem(
        sys.modules,
        "src.utils.rmi_oauth2",
        types.SimpleNamespace(
            get_authorization_header=lambda: asyncio.sleep(0, result="Bearer abc"),
            is_oauth2_configured=lambda: False,
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "src.utils.http_client",
        types.SimpleNamespace(InterceptedHTTPClient=lambda **kwargs: None),
    )

    module = load_module("test_memory_module_unauth", "src/tools/memory.py")
    result = await module.get_memories("u1")
    assert result["status"] == "Error"

    class CreateClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return None

        async def put(self, url, headers=None, json=None):
            raise httpx.HTTPStatusError(
                "404",
                request=types.SimpleNamespace(),
                response=types.SimpleNamespace(status_code=404),
            )

        async def post(self, url, headers=None, json=None):
            return FakeResponse({"status": "created"})

    monkeypatch.setitem(
        sys.modules,
        "src.utils.rmi_oauth2",
        types.SimpleNamespace(
            get_authorization_header=lambda: asyncio.sleep(0, result="Bearer abc"),
            is_oauth2_configured=lambda: True,
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "src.utils.http_client",
        types.SimpleNamespace(InterceptedHTTPClient=lambda **kwargs: CreateClient()),
    )
    module = load_module("test_memory_module_create", "src/tools/memory.py")
    result = await module.upsert_memory(
        "u1",
        {
            "memory_name": "prefs",
            "description": "descr",
            "relevance": "low",
            "memory_type": "appended",
            "value": "123",
        },
    )
    assert result == {"status": "created"}


@pytest.mark.asyncio
async def test_feedback_tool_paths(monkeypatch):
    ensure_package("src", PROJECT_ROOT / "src")
    ensure_package("src.tools", PROJECT_ROOT / "src" / "tools")
    ensure_package("src.utils", PROJECT_ROOT / "src" / "utils")
    ensure_package("src.config", PROJECT_ROOT / "src" / "config")

    created = []

    def fake_create_task(coro):
        created.append(coro)
        coro.close()
        return None

    monkeypatch.setattr(asyncio, "create_task", fake_create_task)

    monkeypatch.setitem(
        sys.modules,
        "src.utils.bigquery",
        types.SimpleNamespace(
            save_feedback_in_bq_background=lambda **kwargs: asyncio.sleep(0),
            get_datetime=lambda: "2026-04-08T10:00:00.000000",
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "src.utils.log",
        types.SimpleNamespace(
            logger=types.SimpleNamespace(
                info=lambda *_a, **_k: None, error=lambda *_a, **_k: None
            )
        ),
    )
    env_module = types.SimpleNamespace(ENVIRONMENT="test")
    monkeypatch.setitem(sys.modules, "src.config.env", env_module)
    monkeypatch.setitem(
        sys.modules, "src.config", types.SimpleNamespace(env=env_module)
    )
    monkeypatch.setitem(
        sys.modules,
        "src.utils.error_interceptor",
        types.SimpleNamespace(interceptor=passthrough_interceptor),
    )

    module = load_module("test_feedback_module", "src/tools/feedback_tools.py")

    result = await module.store_user_feedback("", "ok")
    assert result["success"] is False

    result = await module.store_user_feedback("u1", "")
    assert result["success"] is False

    result = await module.store_user_feedback("u1", "closed_beta_feedback")
    assert result["success"] is True
    assert "cumprimente o usuário" in result["message"].lower()

    result = await module.store_user_feedback(" u1 ", " gostei ")
    assert result["success"] is True
    assert result["timestamp"] == "2026-04-08T10:00:00.000000"
    assert created

    monkeypatch.setitem(
        sys.modules,
        "src.utils.bigquery",
        types.SimpleNamespace(
            save_feedback_in_bq_background=lambda **kwargs: asyncio.sleep(0),
            get_datetime=lambda: (_ for _ in ()).throw(RuntimeError("boom")),
        ),
    )
    module = load_module("test_feedback_module_error", "src/tools/feedback_tools.py")
    result = await module.store_user_feedback("u1", "teste")
    assert result["success"] is False
