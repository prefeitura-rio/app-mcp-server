import asyncio
import importlib.util
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace

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
    def __init__(self, payload):
        self._payload = payload
        self.text = json.dumps(payload)

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


class FakeAsyncClientContext:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return None

    async def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.response


@pytest.fixture
def infisical_module(monkeypatch):
    module = load_module("test_infisical_module", "src/utils/infisical.py")
    monkeypatch.setattr(module, "_env_cache", {})
    return module


def test_load_dotenv_returns_empty_when_missing(
    tmp_path, monkeypatch, infisical_module
):
    monkeypatch.chdir(tmp_path)

    assert infisical_module._load_dotenv() == {}


def test_load_dotenv_parses_values_and_uses_cache(
    tmp_path, monkeypatch, infisical_module
):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(
        "\n".join(
            [
                "# comment",
                "FOO=bar",
                'BAR="baz qux"',
                "SPACED = ' value '",
            ]
        ),
        encoding="utf-8",
    )

    parsed = infisical_module._load_dotenv()
    second = infisical_module._load_dotenv()

    assert parsed == {"FOO": "bar", "BAR": "baz qux", "SPACED": " value "}
    assert second is parsed


def test_getenv_or_action_respects_env_precedence(monkeypatch, infisical_module):
    monkeypatch.setattr(
        infisical_module, "_load_dotenv", lambda: {"TOKEN": "from-file"}
    )
    monkeypatch.setattr(
        infisical_module, "getenv", lambda *_args, **_kwargs: "from-env"
    )

    assert infisical_module.getenv_or_action("TOKEN", action="ignore") == "from-env"


def test_getenv_or_action_supports_warn_ignore_and_raise(monkeypatch, infisical_module):
    monkeypatch.setattr(infisical_module, "getenv", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(infisical_module, "_load_dotenv", lambda: {})
    warnings = []
    monkeypatch.setattr(infisical_module.logger, "warning", warnings.append)

    assert infisical_module.getenv_or_action("MISSING", action="warn") is None
    assert warnings
    assert (
        infisical_module.getenv_or_action("MISSING", action="ignore", default="x")
        == "x"
    )

    with pytest.raises(EnvironmentError, match="MISSING"):
        infisical_module.getenv_or_action("MISSING", action="raise")


def test_getenv_or_action_rejects_invalid_action(infisical_module):
    with pytest.raises(ValueError, match="action must be one of"):
        infisical_module.getenv_or_action("ANY", action="oops")


def test_getenv_list_or_action_and_mask_string(monkeypatch, infisical_module):
    monkeypatch.setattr(
        infisical_module,
        "getenv_or_action",
        lambda env_name, action="raise", default=None: {
            "CSV": "a,b,c",
            "LIST": ["x", "y"],
            "BAD": 123,
            "EMPTY": None,
        }[env_name],
    )

    assert infisical_module.getenv_list_or_action("CSV") == ["a", "b", "c"]
    assert infisical_module.getenv_list_or_action("LIST") == ["x", "y"]
    assert infisical_module.getenv_list_or_action("EMPTY") == []
    with pytest.raises(TypeError, match="string or a list"):
        infisical_module.getenv_list_or_action("BAD")

    assert infisical_module.mask_string("abcdefgh") == "a****h"
    assert infisical_module.mask_string("abcdefghi", mask="#") == "a#####i"


def build_wrapper_module(
    monkeypatch, module_name: str, relative_path: str, env_values: dict
):
    ensure_package("src", PROJECT_ROOT / "src")
    ensure_package("src.tools", PROJECT_ROOT / "src" / "tools")
    ensure_package("src.utils", PROJECT_ROOT / "src" / "utils")
    ensure_package("src.config", PROJECT_ROOT / "src" / "config")

    env_module = types.ModuleType("src.config.env")
    for key, value in env_values.items():
        setattr(env_module, key, value)
    monkeypatch.setitem(sys.modules, "src.config.env", env_module)

    env_pkg = types.ModuleType("src.config")
    env_pkg.env = env_module
    monkeypatch.setitem(sys.modules, "src.config", env_pkg)

    error_interceptor_module = types.ModuleType("src.utils.error_interceptor")
    error_interceptor_module.interceptor = passthrough_interceptor
    monkeypatch.setitem(
        sys.modules, "src.utils.error_interceptor", error_interceptor_module
    )

    return load_module(module_name, relative_path)


@pytest.mark.asyncio
async def test_internal_request_handles_json_empty_and_timeout(monkeypatch):
    module = build_wrapper_module(
        monkeypatch,
        "test_tools_utils_module",
        "src/tools/utils.py",
        {
            "CHATBOT_INTEGRATIONS_URL": "https://integrations.local/",
            "CHATBOT_INTEGRATIONS_KEY": "secret-key",
        },
    )

    logger_messages = {"info": [], "warning": [], "error": []}
    monkeypatch.setattr(module.logger, "info", logger_messages["info"].append)
    monkeypatch.setattr(module.logger, "warning", logger_messages["warning"].append)
    monkeypatch.setattr(module.logger, "error", logger_messages["error"].append)

    assert (
        module.get_integrations_url("/request") == "https://integrations.local/request"
    )

    json_client = FakeAsyncClientContext(FakeResponse({"ok": True}))
    monkeypatch.setattr(module, "InterceptedHTTPClient", lambda **kwargs: json_client)
    response = await module.internal_request(
        "https://api.local/resource", method="POST", request_kwargs={"a": 1}
    )
    assert response == {"ok": True}
    url, kwargs = json_client.calls[0]
    assert url == "https://integrations.local/request"
    assert kwargs["headers"]["Authorization"] == "Bearer secret-key"

    empty_client = FakeAsyncClientContext(SimpleNamespace(text=""))
    monkeypatch.setattr(module, "InterceptedHTTPClient", lambda **kwargs: empty_client)
    assert await module.internal_request("https://api.local/empty") is None

    timeout_client = FakeAsyncClientContext(
        SimpleNamespace(text="504 Gateway Time-out")
    )
    monkeypatch.setattr(
        module, "InterceptedHTTPClient", lambda **kwargs: timeout_client
    )
    with pytest.raises(TimeoutError, match="Gateway timeout"):
        await module.internal_request("https://api.local/timeout")


@pytest.mark.asyncio
async def test_dharma_search_builds_expected_request(monkeypatch):
    module = build_wrapper_module(
        monkeypatch,
        "test_dharma_search_module",
        "src/tools/dharma_search.py",
        {"DHARMA_API_KEY": "dharma-token"},
    )

    client = FakeAsyncClientContext(FakeResponse({"message": "ok"}))
    monkeypatch.setitem(
        sys.modules,
        "src.utils.http_client",
        types.SimpleNamespace(InterceptedHTTPClient=lambda **kwargs: client),
    )
    module = load_module(
        "test_dharma_search_module_reloaded", "src/tools/dharma_search.py"
    )

    result = await module.dharma_search("oi")

    assert result == {"message": "ok"}
    url, kwargs = client.calls[0]
    assert url.endswith("/v1/chats")
    assert kwargs["headers"]["Authorization"] == "Bearer dharma-token"
    assert kwargs["json"] == {"message": "oi"}


@pytest.mark.asyncio
async def test_surkai_search_builds_expected_request(monkeypatch):
    module = build_wrapper_module(
        monkeypatch,
        "test_surkai_module",
        "src/tools/web_search_surkai.py",
        {"SURKAI_API_KEY": "surkai-token"},
    )

    client = FakeAsyncClientContext(FakeResponse({"summary": "ok"}))
    monkeypatch.setitem(
        sys.modules,
        "src.utils.http_client",
        types.SimpleNamespace(InterceptedHTTPClient=lambda **kwargs: client),
    )
    module = load_module(
        "test_surkai_module_reloaded", "src/tools/web_search_surkai.py"
    )

    result = await module.surkai_search("chuva", k=3, lang="en")

    assert result == {"summary": "ok"}
    url, kwargs = client.calls[0]
    assert url.endswith("/web_search")
    assert kwargs["headers"]["Authorization"] == "Bearer surkai-token"
    assert kwargs["json"] == {"k": 3, "lang": "en", "query": "chuva"}


@pytest.mark.asyncio
async def test_search_uses_typesense_then_google_fallback(monkeypatch):
    ensure_package("src", PROJECT_ROOT / "src")
    ensure_package("src.tools", PROJECT_ROOT / "src" / "tools")
    ensure_package(
        "src.tools.google_search", PROJECT_ROOT / "src" / "tools" / "google_search"
    )
    ensure_package("src.utils", PROJECT_ROOT / "src" / "utils")

    env_module = types.ModuleType("src.config.env")
    env_module.TYPESENSE_ACTIVE = "true"
    env_module.TYPESENSE_HUB_SEARCH_URL = "https://typesense.local"
    env_module.TYPESENSE_PARAMETERS = '{"type":"hybrid","per_page":2}'
    env_module.GEMINI_MODEL = "gemini-test"
    monkeypatch.setitem(sys.modules, "src.config.env", env_module)
    monkeypatch.setitem(
        sys.modules, "src.config", types.SimpleNamespace(env=env_module)
    )

    save_calls = []
    created_tasks = []

    async def fake_save_response_in_bq_background(**kwargs):
        save_calls.append(kwargs)

    async def fake_hub_search(request):
        return {
            "results": [{"id": 1}],
            "results_clean": [{"title": "A"}],
        }

    async def fake_google_search(**kwargs):
        return {
            "text": "google text",
            "sources": [{"url": "https://example.com"}],
            "web_search_queries": ["x"],
            "id": "abc",
        }

    class HubSearchRequest:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    monkeypatch.setitem(
        sys.modules,
        "src.tools.google_search.gemini_service",
        types.SimpleNamespace(
            gemini_service=types.SimpleNamespace(google_search=fake_google_search)
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "src.utils.bigquery",
        types.SimpleNamespace(
            save_response_in_bq_background=fake_save_response_in_bq_background
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "src.utils.typesense_api",
        types.SimpleNamespace(
            HubSearchRequest=HubSearchRequest, hub_search=fake_hub_search
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "src.utils.error_interceptor",
        types.SimpleNamespace(interceptor=passthrough_interceptor),
    )
    monkeypatch.setitem(
        sys.modules,
        "src.utils.log",
        types.SimpleNamespace(
            logger=types.SimpleNamespace(
                info=lambda *_args, **_kwargs: None,
                error=lambda *_args, **_kwargs: None,
            )
        ),
    )
    monkeypatch.setattr(asyncio, "create_task", lambda coro: created_tasks.append(coro))

    module = load_module("test_search_module", "src/tools/search.py")

    assert module._get_typesense_params()["type"] == "hybrid"
    result = await module.get_google_search("teste")
    assert result == {"response": [{"title": "A"}]}
    assert created_tasks
    await created_tasks.pop()

    async def fake_empty_hub_search(request):
        return {"results": [], "results_clean": []}

    monkeypatch.setattr(module, "hub_search", fake_empty_hub_search)
    monkeypatch.setattr(module.gemini_service, "google_search", fake_google_search)

    fallback = await module.get_google_search("fallback")
    assert fallback["text"] == "google text"
    assert fallback["id"] == "abc"
    while created_tasks:
        await created_tasks.pop()


def test_search_typesense_params_invalid_json(monkeypatch):
    ensure_package("src", PROJECT_ROOT / "src")
    ensure_package("src.tools", PROJECT_ROOT / "src" / "tools")
    ensure_package(
        "src.tools.google_search", PROJECT_ROOT / "src" / "tools" / "google_search"
    )
    ensure_package("src.utils", PROJECT_ROOT / "src" / "utils")

    env_module = types.ModuleType("src.config.env")
    env_module.TYPESENSE_ACTIVE = "false"
    env_module.TYPESENSE_HUB_SEARCH_URL = ""
    env_module.TYPESENSE_PARAMETERS = "{invalid"
    env_module.GEMINI_MODEL = "gemini-test"
    monkeypatch.setitem(sys.modules, "src.config.env", env_module)
    monkeypatch.setitem(
        sys.modules, "src.config", types.SimpleNamespace(env=env_module)
    )
    monkeypatch.setitem(
        sys.modules,
        "src.tools.google_search.gemini_service",
        types.SimpleNamespace(gemini_service=types.SimpleNamespace(google_search=None)),
    )
    monkeypatch.setitem(
        sys.modules,
        "src.utils.bigquery",
        types.SimpleNamespace(save_response_in_bq_background=None),
    )
    monkeypatch.setitem(
        sys.modules,
        "src.utils.typesense_api",
        types.SimpleNamespace(HubSearchRequest=object, hub_search=None),
    )
    monkeypatch.setitem(
        sys.modules,
        "src.utils.error_interceptor",
        types.SimpleNamespace(interceptor=passthrough_interceptor),
    )

    logs = {"info": [], "error": []}
    monkeypatch.setitem(
        sys.modules,
        "src.utils.log",
        types.SimpleNamespace(
            logger=types.SimpleNamespace(
                info=logs["info"].append,
                error=logs["error"].append,
            )
        ),
    )

    module = load_module("test_search_params_module", "src/tools/search.py")
    defaults = module._get_typesense_params()

    assert defaults["type"] == "semantic"
    assert logs["error"]


def test_langgraph_workflows_delegate_to_orchestrator(monkeypatch):
    ensure_package("src", PROJECT_ROOT / "src")
    ensure_package("src.tools", PROJECT_ROOT / "src" / "tools")
    ensure_package(
        "src.tools.multi_step_service",
        PROJECT_ROOT / "src" / "tools" / "multi_step_service",
    )
    ensure_package(
        "src.tools.multi_step_service.core",
        PROJECT_ROOT / "src" / "tools" / "multi_step_service" / "core",
    )

    class StateMode:
        JSON = "json"
        REDIS = "redis"

    class ServiceRequest:
        def __init__(self, **kwargs):
            self.__dict__.update(kwargs)

    class FakeResponseModel:
        def model_dump(self):
            return {"ok": True}

    class Orchestrator:
        def __init__(self, backend_mode):
            self.backend_mode = backend_mode

        async def execute_workflow(self, request):
            assert request.service_name == "iptu"
            assert request.user_id == "u1"
            assert request.payload == {"x": 1}
            return FakeResponseModel()

        def save_all_workflow_graphs(self):
            return {"saved": True}

        def save_workflow_graph_image(self, service_name):
            return f"/tmp/{service_name}.png"

    monkeypatch.setitem(
        sys.modules,
        "src.tools.multi_step_service.core",
        types.SimpleNamespace(
            Orchestrator=Orchestrator,
            ServiceRequest=ServiceRequest,
            StateMode=StateMode,
            tools_description={"ok": True},
        ),
    )

    env_module = types.ModuleType("src.config.env")
    env_module.IS_LOCAL = True
    monkeypatch.setitem(sys.modules, "src.config.env", env_module)
    monkeypatch.setitem(
        sys.modules, "src.config", types.SimpleNamespace(env=env_module)
    )

    module = load_module(
        "test_langgraph_workflows_module", "src/tools/langgraph_workflows.py"
    )

    result = asyncio.run(module.multi_step_service("iptu", "u1", {"x": 1}))
    assert result == {"ok": True}
    assert module.save_workflow_graphs() == {"saved": True}
    assert module.save_single_workflow_graph("iptu") == "/tmp/iptu.png"
