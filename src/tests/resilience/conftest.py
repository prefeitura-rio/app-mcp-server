"""Shared pytest fixtures for the isolated resilience drill harness (plan
todo 8). Reusable fakes/helpers themselves live in `fakes.py` (importable
directly by test modules); this module only wires them into fixtures.

No real Kubernetes, Redis, or SigNoz instance is contacted by anything under
`src/tests/resilience/`. `no_sleep` (below) monkeypatches `asyncio.sleep`
wherever a drill needs it, the same way
`src/tests/unit/tools/test_redis_backend_failover.py` already does for
task 6 — no drill in this package actually waits in real time, including the
one that models a ~10-20s Sentinel failover window.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path

import pytest

from src.tests.resilience.fakes import REDIS_URL_FIXTURE, load_fixture


@pytest.fixture
def fixture_loader():
    return load_fixture


# ---------------------------------------------------------------------------
# Isolated import of RedisBackend, reusing the exact technique already
# established by src/tests/unit/tools/test_redis_backend_failover.py: a
# direct file-path import bypasses `src.tools.multi_step_service.core.models`
# (Pydantic models RedisBackend never touches) and `src.config.env` (which
# requires 27 production env vars to import cleanly, per
# docs/decisions/health-checks-e-preflight.md).
# ---------------------------------------------------------------------------

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_STATE_MODULE_PATH = (
    _PROJECT_ROOT / "src" / "tools" / "multi_step_service" / "core" / "state.py"
)


def _load_state_module(monkeypatch: pytest.MonkeyPatch):
    env_stub = types.SimpleNamespace(
        REDIS_URL=REDIS_URL_FIXTURE, REDIS_TTL_SECONDS=3600
    )
    monkeypatch.setitem(sys.modules, "src.config", types.SimpleNamespace(env=env_stub))
    monkeypatch.setitem(sys.modules, "src.config.env", env_stub)
    monkeypatch.setitem(
        sys.modules,
        "src.tools.multi_step_service.core.models",
        types.SimpleNamespace(ServiceState=object, ServiceMetadata=object),
    )

    spec = importlib.util.spec_from_file_location(
        "resilience_drill_state_module", _STATE_MODULE_PATH
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def state_module(monkeypatch: pytest.MonkeyPatch):
    return _load_state_module(monkeypatch)


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch, state_module):
    """Records requested sleep durations instead of actually sleeping —
    every drill that models a multi-second/minute failover window runs in
    well under a second of wall-clock test time."""
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr(state_module.asyncio, "sleep", fake_sleep)
    return slept
