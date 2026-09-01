"""Drill: unhealthy namespace workload / OOM & eviction (plan todo 8,
failure class 2 of 5).

Reimplements `signoz_alert.mcp_pod_lifecycle_unhealthy`'s threshold logic
(`signoz_alert_evaluators.evaluate_pod_lifecycle_unhealthy`) against fixture
OOM/eviction counts — see
`docs/runbooks/unhealthy-workload.md#safety-boundaries` for why this stays a
pure-fixture check rather than a live cluster query.
"""

from __future__ import annotations

import pytest

from src.tests.resilience.signoz_alert_evaluators import (
    MalformedFixtureError,
    evaluate_pod_lifecycle_unhealthy,
)


def test_healthy_fixture_does_not_fire(fixture_loader):
    verdict = evaluate_pod_lifecycle_unhealthy(fixture_loader("pod_lifecycle_healthy"))

    assert verdict.firing is False
    assert verdict.severity == "warning"  # ticket, not page


def test_single_oom_event_fires(fixture_loader):
    verdict = evaluate_pod_lifecycle_unhealthy(fixture_loader("pod_lifecycle_firing"))

    assert verdict.firing is True
    assert verdict.alert_name == "mcp_pod_lifecycle_unhealthy"
    assert verdict.runbook_url == "https://runbooks.example.internal/mcp/pod-lifecycle"


def test_eviction_alone_also_fires():
    """The alert's formula is OOM + eviction, `Above 0` — an eviction with
    zero OOM kills must fire exactly like an OOM with zero evictions."""
    verdict = evaluate_pod_lifecycle_unhealthy(
        {"oom_count": [0, 0, 0], "eviction_count": [0, 1, 0]}
    )

    assert verdict.firing is True


@pytest.mark.parametrize(
    "fixture,expected_message_fragment",
    [
        ({"oom_count": [1], "eviction_count": [1, 2]}, "same length"),
        ({"oom_count": None, "eviction_count": [0]}, "oom_count"),
        ({"oom_count": [1], "eviction_count": ["x"]}, "non-numeric"),
    ],
)
def test_malformed_fixture_raises_clear_error(fixture, expected_message_fragment):
    with pytest.raises(MalformedFixtureError, match=expected_message_fragment):
        evaluate_pod_lifecycle_unhealthy(fixture)
