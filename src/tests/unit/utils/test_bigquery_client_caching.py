"""
Unit tests for get_bigquery_client() caching behaviour (CHATR-114).

Verifies that the @functools.lru_cache(maxsize=1) decoration makes
get_bigquery_client() return the same Client instance across multiple calls
and that the underlying bigquery.Client constructor is invoked only once per
process lifetime (i.e. per lru_cache lifetime).

Test-isolation note
-------------------
Because lru_cache stores state at module level, each test that exercises the
real get_bigquery_client() call path must call get_bigquery_client.cache_clear()
at teardown (via autouse fixture) so that sibling tests start with a clean
slate and are not affected by previously cached values.
"""

import base64
import importlib.util
import json
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock


PROJECT_ROOT = Path(__file__).resolve().parents[4]


def _ensure_package(name: str, path: Path) -> types.ModuleType:
    pkg = types.ModuleType(name)
    pkg.__path__ = [str(path)]
    sys.modules[name] = pkg
    return pkg


def _passthrough_interceptor(*_args, **_kwargs):
    def decorator(func):
        return func

    return decorator


def _load_fresh_bigquery_module(monkeypatch, module_alias: str) -> types.ModuleType:
    """Load src/utils/bigquery.py under a unique alias to get a fresh module.

    Each call produces an independent module object with its own lru_cache
    instance, so tests are fully isolated from each other even when run in the
    same process.
    """
    _ensure_package("src", PROJECT_ROOT / "src")
    _ensure_package("src.config", PROJECT_ROOT / "src" / "config")
    _ensure_package("src.utils", PROJECT_ROOT / "src" / "utils")

    env_module = types.SimpleNamespace(
        GCP_SERVICE_ACCOUNT_CREDENTIALS=base64.b64encode(
            json.dumps({"project_id": "proj-cache-test"}).encode()
        ).decode(),
        GOOGLE_BIGQUERY_PAGE_SIZE=100,
    )
    monkeypatch.setitem(sys.modules, "src.config.env", env_module)
    monkeypatch.setitem(
        sys.modules, "src.config", types.SimpleNamespace(env=env_module)
    )
    monkeypatch.setitem(
        sys.modules,
        "src.utils.error_interceptor",
        types.SimpleNamespace(interceptor=_passthrough_interceptor),
    )
    monkeypatch.setitem(
        sys.modules,
        "src.utils.log",
        types.SimpleNamespace(
            logger=types.SimpleNamespace(
                info=lambda *_a, **_k: None,
                error=lambda *_a, **_k: None,
                warning=lambda *_a, **_k: None,
                exception=lambda *_a, **_k: None,
            )
        ),
    )

    spec = importlib.util.spec_from_file_location(
        module_alias, PROJECT_ROOT / "src" / "utils" / "bigquery.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_alias] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_get_bigquery_client_returns_same_instance_on_repeated_calls(monkeypatch):
    """Calling get_bigquery_client() twice must return the identical object.

    The bigquery.Client constructor must be invoked exactly once, not once per
    call (CHATR-114 fix: cache the client to prevent per-call memory leak).
    """
    module = _load_fresh_bigquery_module(monkeypatch, "bq_cache_test_same_instance")

    constructor_call_count = 0
    sentinel_client = object()  # unique sentinel so we can use `is`

    def _fake_client_constructor(credentials, project):
        nonlocal constructor_call_count
        constructor_call_count += 1
        return sentinel_client

    fake_credentials = types.SimpleNamespace(
        project_id="proj-cache-test",
        with_scopes=lambda scopes: types.SimpleNamespace(
            project_id="proj-cache-test", scopes=scopes
        ),
    )
    monkeypatch.setattr(
        module.service_account.Credentials,
        "from_service_account_info",
        lambda info: fake_credentials,
    )
    bigquery_stub = types.SimpleNamespace(Client=_fake_client_constructor)
    monkeypatch.setattr(module, "bigquery", bigquery_stub)

    # First call — should construct the client
    client_1 = module.get_bigquery_client()
    assert constructor_call_count == 1, "Client should be constructed on first call"

    # Second call — must return the cached instance without re-constructing
    client_2 = module.get_bigquery_client()
    assert constructor_call_count == 1, (
        "Client constructor must NOT be called again on second call — "
        "lru_cache should return the cached instance"
    )

    # Both references must point to the exact same object
    assert client_1 is client_2, (
        "get_bigquery_client() must return the same instance on every call"
    )
    assert client_1 is sentinel_client


def test_get_bigquery_client_cache_clear_allows_fresh_construction(monkeypatch):
    """After cache_clear(), the next call should construct a new client.

    This verifies that the lru_cache attribute is properly exposed (needed by
    test-isolation fixtures and for operational cache invalidation if ever
    required).
    """
    module = _load_fresh_bigquery_module(monkeypatch, "bq_cache_test_cache_clear")

    constructor_call_count = 0

    def _counting_constructor(credentials, project):
        nonlocal constructor_call_count
        constructor_call_count += 1
        return MagicMock(name=f"client-{constructor_call_count}")

    fake_credentials = types.SimpleNamespace(
        project_id="proj-cache-test",
        with_scopes=lambda scopes: types.SimpleNamespace(
            project_id="proj-cache-test", scopes=scopes
        ),
    )
    monkeypatch.setattr(
        module.service_account.Credentials,
        "from_service_account_info",
        lambda info: fake_credentials,
    )
    monkeypatch.setattr(
        module, "bigquery", types.SimpleNamespace(Client=_counting_constructor)
    )

    client_a = module.get_bigquery_client()
    assert constructor_call_count == 1

    # Clear the cache — simulates what a test-isolation fixture does
    module.get_bigquery_client.cache_clear()

    client_b = module.get_bigquery_client()
    assert constructor_call_count == 2, (
        "After cache_clear(), a new client must be constructed"
    )
    assert client_a is not client_b, (
        "After cache_clear(), a different instance should be returned"
    )


def test_get_bigquery_client_many_calls_single_construction(monkeypatch):
    """N repeated calls should result in exactly 1 constructor invocation."""
    module = _load_fresh_bigquery_module(monkeypatch, "bq_cache_test_many_calls")

    call_count = 0

    def _counting_constructor(credentials, project):
        nonlocal call_count
        call_count += 1
        return object()

    fake_credentials = types.SimpleNamespace(
        project_id="proj-cache-test",
        with_scopes=lambda scopes: types.SimpleNamespace(
            project_id="proj-cache-test", scopes=scopes
        ),
    )
    monkeypatch.setattr(
        module.service_account.Credentials,
        "from_service_account_info",
        lambda info: fake_credentials,
    )
    monkeypatch.setattr(
        module, "bigquery", types.SimpleNamespace(Client=_counting_constructor)
    )

    N = 10
    results = [module.get_bigquery_client() for _ in range(N)]

    assert call_count == 1, (
        f"Expected exactly 1 Client() construction across {N} calls, got {call_count}"
    )
    # All returned objects must be the same instance
    first = results[0]
    assert all(r is first for r in results), (
        "All calls must return the identical cached instance"
    )


def test_get_bigquery_client_has_lru_cache_interface(monkeypatch):
    """get_bigquery_client must expose the lru_cache management interface.

    Specifically, cache_clear() and cache_info() must be callable — this is
    required by test-isolation fixtures and documents the contract.
    """
    module = _load_fresh_bigquery_module(monkeypatch, "bq_cache_test_interface")

    # The decorated function must expose these lru_cache attributes
    assert callable(getattr(module.get_bigquery_client, "cache_clear", None)), (
        "get_bigquery_client must have a cache_clear() method (lru_cache interface)"
    )
    assert callable(getattr(module.get_bigquery_client, "cache_info", None)), (
        "get_bigquery_client must have a cache_info() method (lru_cache interface)"
    )
