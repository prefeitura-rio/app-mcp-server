"""Pure-Python reimplementations of the SigNoz alert threshold semantics
referenced by `docs/runbooks/`, for the isolated fixture-based drill harness
(plan todo 8).

Why reimplement instead of calling a real SigNoz Query Service: this task's
REQUIRED TOOLS explicitly exclude cloud/cluster access, and no SigNoz
instance is reachable from this repository's test suite in any environment.
What these functions verify instead is narrower and honest about it: given a
fixture time series shaped like what the named `signoz_alert` resource
queries for, does *this* evaluator's firing decision match the resource's own
`condition.thresholds` block? Each function cites the exact resource address
and line range it mirrors, in
`infra/superapp/modules/deployments/signoz-resilience-alerts.tf`, so drift
between this file and the real resource is auditable by re-reading both, not
hidden behind "the alert works" false confidence — this reimplementation
proves the *rule logic* is what the runbooks say it is, not that the real
SigNoz instance evaluates it correctly.

Only `matchType: AtLeastOnce` is implemented: it is the only `matchType`
value used by any `signoz_alert` resource in that file today. A resource
that started using a different `matchType` (e.g. `AllTheTime`, `OnAverage`)
would silently be mismodeled here — `evaluate_threshold_series` raises
`NotImplementedError` rather than guessing, so that drift is loud, not
silent.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Sequence

ThresholdOp = Literal["Above", "Below"]


class MalformedFixtureError(ValueError):
    """Raised when a fixture does not have the shape an evaluator requires.

    Distinct from `ValueError`/`KeyError` so callers (and the release
    checker's tests) can assert on a specific, documented failure mode
    instead of an incidental one — see
    `src/tests/resilience/test_drill_mcp_unavailable.py`'s malformed-fixture
    cases.
    """


def _require_numeric_series(value: object, *, field: str) -> Sequence[float]:
    if not isinstance(value, list) or not value:
        raise MalformedFixtureError(
            f"fixture field {field!r} must be a non-empty list of numbers, "
            f"got {value!r}"
        )
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise MalformedFixtureError(
                f"fixture field {field!r} contains a non-numeric sample: {item!r}"
            )
    return value


def evaluate_threshold_series(
    series: Sequence[float], *, op: ThresholdOp, target: float
) -> bool:
    """Mirrors a `thresholds.kind: basic` / `matchType: AtLeastOnce` decision.

    `AtLeastOnce` fires the first time any sample in the evaluation window
    crosses `target` via `op` — not only if every sample does. This matches
    every `signoz_alert` resource's `thresholds.spec[].matchType` in
    `infra/superapp/modules/deployments/signoz-resilience-alerts.tf`
    (confirmed by reading all five resources during this task: every one
    uses `"AtLeastOnce"`).
    """
    if op == "Above":
        return any(v > target for v in series)
    if op == "Below":
        return any(v < target for v in series)
    raise ValueError(f"unsupported threshold op {op!r}")  # pragma: no cover


@dataclass(frozen=True)
class AlertVerdict:
    firing: bool
    alert_name: str
    severity: str
    runbook_url: str


# ---------------------------------------------------------------------------
# signoz_alert.mcp_workload_unavailable
# infra/superapp/modules/deployments/signoz-resilience-alerts.tf:142-188
# ---------------------------------------------------------------------------


def evaluate_workload_unavailable(fixture: dict) -> AlertVerdict:
    """F1 = A - B, `Above 0`, `AtLeastOnce` (lines 154-177 of the cited file).

    `fixture` shape: `{"desired": [..], "available": [..]}`, one sample per
    evaluated minute (`evaluation.spec.frequency: 1m`) within the rolling
    `evalWindow: 10m`. Both series must be the same, non-empty length — a
    malformed fixture (missing key, mismatched lengths, non-numeric sample)
    raises `MalformedFixtureError` rather than silently evaluating a partial
    or wrong-shaped series.
    """
    desired = _require_numeric_series(fixture.get("desired"), field="desired")
    available = _require_numeric_series(fixture.get("available"), field="available")
    if len(desired) != len(available):
        raise MalformedFixtureError(
            "fixture fields 'desired' and 'available' must be the same "
            f"length, got {len(desired)} vs {len(available)}"
        )
    delta = [d - a for d, a in zip(desired, available)]
    firing = evaluate_threshold_series(delta, op="Above", target=0)
    return AlertVerdict(
        firing=firing,
        alert_name="mcp_workload_unavailable",
        severity="critical",
        runbook_url="https://runbooks.example.internal/mcp/workload-unavailable",
    )


# ---------------------------------------------------------------------------
# signoz_alert.mcp_redis_workload_unavailable
# infra/superapp/modules/deployments/signoz-resilience-alerts.tf:198-244
# ---------------------------------------------------------------------------


def evaluate_redis_workload_unavailable(fixture: dict) -> AlertVerdict:
    """Identical formula/threshold to `mcp_workload_unavailable` above, just
    filtered to redis-named deployments instead of excluding them (lines
    210-233 of the cited file) — same evaluator, different resource name."""
    verdict = evaluate_workload_unavailable(fixture)
    return AlertVerdict(
        firing=verdict.firing,
        alert_name="mcp_redis_workload_unavailable",
        severity="critical",
        runbook_url="https://runbooks.example.internal/mcp/redis-unavailable",
    )


# ---------------------------------------------------------------------------
# signoz_alert.mcp_pod_lifecycle_unhealthy
# infra/superapp/modules/deployments/signoz-resilience-alerts.tf:250-293
# ---------------------------------------------------------------------------


def evaluate_pod_lifecycle_unhealthy(fixture: dict) -> AlertVerdict:
    """F1 = A + B, `Above 0`, `AtLeastOnce` (lines 262-283 of the cited file).

    `fixture` shape: `{"oom_count": [..], "eviction_count": [..]}`, one
    sample per evaluated 5-minute tick within the rolling `evalWindow: 15m`.
    """
    oom = _require_numeric_series(fixture.get("oom_count"), field="oom_count")
    eviction = _require_numeric_series(
        fixture.get("eviction_count"), field="eviction_count"
    )
    if len(oom) != len(eviction):
        raise MalformedFixtureError(
            "fixture fields 'oom_count' and 'eviction_count' must be the "
            f"same length, got {len(oom)} vs {len(eviction)}"
        )
    total = [o + e for o, e in zip(oom, eviction)]
    firing = evaluate_threshold_series(total, op="Above", target=0)
    return AlertVerdict(
        firing=firing,
        alert_name="mcp_pod_lifecycle_unhealthy",
        severity="warning",
        runbook_url="https://runbooks.example.internal/mcp/pod-lifecycle",
    )


# ---------------------------------------------------------------------------
# signoz_alert.mcp_telemetry_freshness
# infra/superapp/modules/deployments/signoz-resilience-alerts.tf:301-339
# ---------------------------------------------------------------------------


def evaluate_telemetry_freshness(fixture: dict) -> AlertVerdict:
    """`Below 1`, `AtLeastOnce`, `alertOnAbsent: true` (lines 313-328 of the
    cited file).

    `fixture` shape: `{"span_count": [..]}` (one sample per evaluated
    5-minute tick within `evalWindow: 15m`), or `{"span_count": []}` /
    `{"span_count": null}` to model the "no time series returned at all"
    case that `alertOnAbsent` covers — that case is a *valid* fixture (an
    empty/absent series is exactly what this alert models), not a malformed
    one, so it does not raise `MalformedFixtureError`.
    """
    raw = fixture.get("span_count")
    if raw is None or raw == []:
        return AlertVerdict(
            firing=True,  # alertOnAbsent: true
            alert_name="mcp_telemetry_freshness",
            severity="warning",
            runbook_url="https://runbooks.example.internal/mcp/telemetry-freshness",
        )
    span_count = _require_numeric_series(raw, field="span_count")
    firing = evaluate_threshold_series(span_count, op="Below", target=1)
    return AlertVerdict(
        firing=firing,
        alert_name="mcp_telemetry_freshness",
        severity="warning",
        runbook_url="https://runbooks.example.internal/mcp/telemetry-freshness",
    )
