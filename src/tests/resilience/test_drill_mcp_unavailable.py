"""Drill: MCP unavailable (plan todo 8, failure class 1 of 5).

Two independent things are proven here, isolated and non-destructive:

1. `signoz_alert.mcp_workload_unavailable`'s threshold logic (reimplemented
   in `signoz_alert_evaluators.py`) fires on a fixture that reproduces a
   sustained desired-vs-available mismatch, and does not fire on a healthy
   fixture.
2. The app's own readiness/liveness split (plan todo 7, already merged)
   behaves as `docs/runbooks/mcp-unavailable.md` describes: a pod that
   cannot reach Redis is marked not-ready (removed from the Service) while
   staying alive (not restarted) — this is what lets the Rollout's
   remaining healthy replicas absorb traffic instead of the whole Rollout
   going down. This reuses `src.health.state.evaluate_readiness` directly
   rather than re-deriving its logic, the same way
   `src/tests/unit/health/test_state.py` does.

No `kubectl`, no real Redis, no real cluster — see
`docs/runbooks/mcp-unavailable.md#safety-boundaries`.
"""

from __future__ import annotations

import sys
import types

import pytest

from src.tests.resilience.signoz_alert_evaluators import (
    MalformedFixtureError,
    evaluate_workload_unavailable,
)


# ---------------------------------------------------------------------------
# 1. Alert threshold logic
# ---------------------------------------------------------------------------


def test_healthy_fixture_does_not_fire(fixture_loader):
    verdict = evaluate_workload_unavailable(
        fixture_loader("workload_unavailable_healthy")
    )

    assert verdict.firing is False
    assert verdict.alert_name == "mcp_workload_unavailable"


def test_sustained_mismatch_fires(fixture_loader):
    verdict = evaluate_workload_unavailable(
        fixture_loader("workload_unavailable_firing")
    )

    assert verdict.firing is True
    assert verdict.severity == "critical"
    assert (
        verdict.runbook_url
        == "https://runbooks.example.internal/mcp/workload-unavailable"
    )


@pytest.mark.parametrize(
    "fixture,expected_message_fragment",
    [
        ({"desired": [3, 3], "available": "not-a-list"}, "available"),
        ({"desired": [3, 3], "available": [3]}, "same length"),
        ({"desired": [], "available": []}, "non-empty"),
        ({"desired": [3, "oops"], "available": [3, 3]}, "non-numeric"),
        ({}, "desired"),
    ],
)
def test_malformed_fixture_raises_clear_error(fixture, expected_message_fragment):
    """Adversarial class: malformed fixture input. Each of these must fail
    loudly with a message naming the actual problem — never silently return
    a (possibly wrong) verdict, and never an opaque `KeyError`/`TypeError`
    with no field name in it."""
    with pytest.raises(MalformedFixtureError, match=expected_message_fragment):
        evaluate_workload_unavailable(fixture)


# ---------------------------------------------------------------------------
# 2. App-side readiness/liveness split under the same failure
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_env_prod(monkeypatch):
    """Same technique as `src/tests/unit/health/test_state.py`'s
    `fake_env_prod`: mutate the already-imported `src.config` module's
    `env` attribute in place (not replace the module in `sys.modules`),
    so anything else that already holds a reference to the real package
    keeps working."""
    env = types.SimpleNamespace(IS_LOCAL=False)
    monkeypatch.setattr(sys.modules["src.config"], "env", env, raising=False)
    monkeypatch.setitem(sys.modules, "src.config.env", env)
    return env


@pytest.fixture(autouse=True)
def _restore_ready_state():
    from src.health import state

    original = state.is_ready()
    yield
    state.set_ready(original)


@pytest.mark.asyncio
async def test_unreachable_dependency_marks_pod_not_ready_without_killing_it(
    fake_env_prod, monkeypatch
):
    """This is the exact scenario `docs/runbooks/mcp-unavailable.md` assumes
    is already correct app-side behavior: readiness fails (pod leaves the
    Service) while liveness (`/health`) stays OK (pod is not restarted),
    so the Rollout's remaining Ready replicas — not a full outage — absorb
    the traffic."""
    from src.health import routes, state
    from src.health.models import HealthCheckError

    state.set_ready(True)

    fake_checks = types.ModuleType("src.health.checks")

    async def check_redis():
        raise HealthCheckError("ping sem resposta")

    fake_checks.check_redis = check_redis
    monkeypatch.setitem(sys.modules, "src.health.checks", fake_checks)

    liveness = await routes.health(None)
    readiness = await routes.ready(None)

    assert liveness.status_code == 200
    assert readiness.status_code == 503

    import json

    assert json.loads(readiness.body) == {
        "status": "not_ready",
        "reason": "redis_unavailable",
    }


@pytest.mark.asyncio
async def test_recovery_flips_readiness_back_without_a_new_rollout(
    fake_env_prod, monkeypatch
):
    """Recovery path: once the dependency is reachable again, the very next
    readiness probe succeeds — no restart, no new deploy needed. Mirrors
    `k8s/prod/resources.yaml`'s readinessProbe comment: "quando o Redis volta
    o próximo sucesso já religa o pod ao Service"."""
    from src.health import routes, state
    from src.health.models import CheckStatus

    state.set_ready(True)

    fake_checks = types.ModuleType("src.health.checks")

    async def check_redis_up():
        return CheckStatus.UP

    fake_checks.check_redis = check_redis_up
    monkeypatch.setitem(sys.modules, "src.health.checks", fake_checks)

    readiness = await routes.ready(None)

    assert readiness.status_code == 200
