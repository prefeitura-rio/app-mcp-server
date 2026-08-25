"""Drill: stale telemetry (plan todo 8, failure class 4 of 5).

Two things are proven here, isolated and non-destructive:

1. `signoz_alert.mcp_telemetry_freshness`'s threshold logic (`Below 1`,
   `alertOnAbsent: true`) fires on a stale or entirely-absent span-count
   fixture, and not on a healthy one.
2. `/health` and `/health/ready` stay 200 even when the OTel setup path
   raises — cross-referencing (not duplicating)
   `src/tests/unit/health/test_state.py::test_readiness_nunca_aciona_setup_de_tracing_otel`,
   which already locks in that readiness never even calls
   `tracing.setup_tracing`. This drill additionally exercises the actual
   route handlers (`routes.health`/`routes.ready`), not just
   `state.evaluate_readiness` directly, to prove the guarantee holds at the
   layer an operator actually curls during an incident.
"""

from __future__ import annotations

import json
import sys
import types

import pytest

from src.tests.resilience.signoz_alert_evaluators import (
    MalformedFixtureError,
    evaluate_telemetry_freshness,
)


# ---------------------------------------------------------------------------
# 1. Alert threshold logic
# ---------------------------------------------------------------------------


def test_healthy_span_count_does_not_fire(fixture_loader):
    verdict = evaluate_telemetry_freshness(
        fixture_loader("telemetry_freshness_healthy")
    )

    assert verdict.firing is False


def test_collapsed_span_count_fires(fixture_loader):
    verdict = evaluate_telemetry_freshness(fixture_loader("telemetry_freshness_stale"))

    assert verdict.firing is True
    assert verdict.severity == "warning"  # ticket, not page — see runbook
    assert (
        verdict.runbook_url
        == "https://runbooks.example.internal/mcp/telemetry-freshness"
    )


def test_absent_series_fires_via_alert_on_absent(fixture_loader):
    """`alertOnAbsent: true` means a query that returns no time series at
    all fires exactly like a below-threshold one — this is a distinct code
    path in the evaluator (empty/absent is a valid fixture, not malformed)."""
    verdict = evaluate_telemetry_freshness(fixture_loader("telemetry_freshness_absent"))

    assert verdict.firing is True


@pytest.mark.parametrize(
    "fixture,expected_message_fragment",
    [
        ({"span_count": "not-a-list"}, "span_count"),
        ({"span_count": [1, "oops", 2]}, "non-numeric"),
    ],
)
def test_malformed_fixture_raises_clear_error(fixture, expected_message_fragment):
    with pytest.raises(MalformedFixtureError, match=expected_message_fragment):
        evaluate_telemetry_freshness(fixture)


def test_missing_key_is_treated_as_absent_not_malformed(fixture_loader):
    """A fixture with no `span_count` key at all (`{}`) is deliberately
    treated the same as `{"span_count": []}`/`{"span_count": null}` —
    "absent" is exactly what `alertOnAbsent` models, so this is a valid
    fixture shape, not a malformed one. Distinct from the genuinely
    malformed cases above (wrong type / non-numeric samples)."""
    verdict = evaluate_telemetry_freshness({})

    assert verdict.firing is True


# ---------------------------------------------------------------------------
# 2. App-side independence from the OTel collector, at the route layer
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_env_local(monkeypatch):
    env = types.SimpleNamespace(ENVIRONMENT="test", IS_LOCAL=True)
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
async def test_health_and_ready_survive_a_collector_outage(fake_env_local, monkeypatch):
    """Simulates the collector being completely unreachable by making
    `setup_tracing`/`setup_metrics` explode if called at all — if either
    liveness or readiness accidentally started depending on telemetry setup
    succeeding, this test would fail loudly instead of the failure being
    discovered live in production during an actual collector outage."""
    import src.observability.metrics as metrics_module
    import src.observability.tracing as tracing_module
    from src.health import state

    def _explode(*_args, **_kwargs):
        raise AssertionError(
            "liveness/readiness must never depend on OTel collector setup"
        )

    monkeypatch.setattr(tracing_module, "setup_tracing", _explode)
    monkeypatch.setattr(metrics_module, "setup_metrics", _explode)

    state.set_ready(True)

    from src.health import routes

    liveness = await routes.health(None)
    readiness = await routes.ready(None)

    assert liveness.status_code == 200
    assert json.loads(readiness.body) == {"status": "ready"}
