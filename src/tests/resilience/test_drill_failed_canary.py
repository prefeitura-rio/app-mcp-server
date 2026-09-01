"""Drill: failed canary (plan todo 8, failure class 5 of 5).

Reimplements `AnalysisTemplate/mcp-success-rate`'s abort/promote decision
(`canary_analysis_evaluator.py`, values copied from
`k8s/prod/resources.yaml`) against fixture measurement sequences — see
`docs/runbooks/failed-canary.md#safety-boundaries`: no real Argo Rollouts
controller or live Rollout is exercised.
"""

from __future__ import annotations

import pytest

from src.tests.resilience.canary_analysis_evaluator import (
    ERROR_RATE_MAX,
    FAILURE_LIMIT,
    SUCCESS_RATE_MIN,
    MalformedCanaryFixtureError,
    evaluate_canary_analysis,
)


def test_good_canary_promotes(fixture_loader):
    fixture = fixture_loader("canary_good")

    verdict = evaluate_canary_analysis(fixture["measurements"])

    assert verdict.decision == "promote"
    assert verdict.failing_metric is None
    assert verdict.measurements_evaluated == len(fixture["measurements"])


def test_bad_canary_aborts_and_signals_rollback(fixture_loader):
    fixture = fixture_loader("canary_bad")

    verdict = evaluate_canary_analysis(fixture["measurements"])

    assert verdict.decision == "abort_rollback"
    assert verdict.failing_metric == "error-rate"
    assert verdict.error_rate_failures == FAILURE_LIMIT
    # Stops replaying measurements the instant failureLimit is reached —
    # never claims to have evaluated the whole fixture once aborted.
    assert verdict.measurements_evaluated == 7


def test_threshold_constants_match_the_rollout_manifest():
    """Restates the values copied from `k8s/prod/resources.yaml` as
    assertions on named constants, so a future edit to that manifest's
    `successCondition`/`failureLimit` that is not mirrored here fails a test
    instead of silently making this drill test the wrong thresholds."""
    assert SUCCESS_RATE_MIN == 0.95  # k8s/prod/resources.yaml:270
    assert ERROR_RATE_MAX == 0.05  # k8s/prod/resources.yaml:279
    assert FAILURE_LIMIT == 5  # k8s/prod/resources.yaml: failureLimit (both metrics)


def test_only_success_rate_failing_also_aborts():
    """Either metric independently reaching failureLimit aborts the run —
    not just error-rate."""
    measurements = [{"success_rate": 0.50, "error_rate": 0.01}] * 5

    verdict = evaluate_canary_analysis(measurements)

    assert verdict.decision == "abort_rollback"
    assert verdict.failing_metric == "success-rate"


def test_intermittent_non_consecutive_failures_still_accumulate():
    """Argo Rollouts' failureLimit is cumulative, not consecutive (confirmed
    against Argo Rollouts' own docs during this task — see
    canary_analysis_evaluator.py's module docstring): failures separated by
    healthy measurements must still add up to the same limit."""
    measurements = [
        {"success_rate": 0.50, "error_rate": 0.01},  # fail 1
        {"success_rate": 0.99, "error_rate": 0.01},  # recovers
        {"success_rate": 0.50, "error_rate": 0.01},  # fail 2
        {"success_rate": 0.99, "error_rate": 0.01},  # recovers
        {"success_rate": 0.50, "error_rate": 0.01},  # fail 3
        {"success_rate": 0.50, "error_rate": 0.01},  # fail 4
        {"success_rate": 0.50, "error_rate": 0.01},  # fail 5 -> abort
    ]

    verdict = evaluate_canary_analysis(measurements)

    assert verdict.decision == "abort_rollback"
    assert verdict.measurements_evaluated == 7


@pytest.mark.parametrize(
    "fixture,expected_message_fragment",
    [
        ([], "non-empty"),
        ([{"success_rate": 0.9}], "error_rate"),
        ([{"success_rate": "high", "error_rate": 0.01}], "numeric"),
        ([{"success_rate": 95, "error_rate": 1}], r"\[0, 1\]"),
    ],
)
def test_malformed_fixture_raises_clear_error(fixture, expected_message_fragment):
    """Adversarial class: malformed fixture input — including the specific
    "percentage instead of fraction" mistake (95 instead of 0.95), which
    would otherwise silently evaluate as trivially always-passing
    (`95 >= 0.95`) instead of being rejected."""
    with pytest.raises(MalformedCanaryFixtureError, match=expected_message_fragment):
        evaluate_canary_analysis(fixture)
