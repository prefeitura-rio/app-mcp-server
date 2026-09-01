"""Pure-Python reimplementation of `AnalysisTemplate/mcp-success-rate`'s
abort/promote decision (`k8s/prod/resources.yaml:260-285`), for the isolated
canary drill (plan todo 8, failure class "failed canary").

Semantics implemented — confirmed against Argo Rollouts' own documentation
during this task (`docs/features/analysis.md`, "Failure Conditions and
Failure Limit" / "ConsecutiveSuccessLimit and FailureLimit" sections), not
assumed from the field name alone:

- Neither metric in `k8s/prod/resources.yaml` sets `consecutiveSuccessLimit`,
  so this is Argo Rollouts' "only FailureLimit applicable" case: each
  metric's failed-measurement count is CUMULATIVE across the whole analysis
  run, not required to be consecutive. A metric that fails, recovers, then
  fails again still adds up toward the same `failureLimit`.
- `failureLimit: 5` on both `success-rate` and `error-rate` in this
  Rollout's `AnalysisTemplate` (verified by reading
  `k8s/prod/resources.yaml:267-284` during this task): the analysis run
  fails as soon as EITHER metric's cumulative failure count reaches 5.
- No `trafficRouting.istio` and no `count:` cap on either metric in this
  Rollout, so this evaluator does not model an "analysis reached its planned
  end without failing" success path — only the abort-on-failureLimit path,
  which is the one this failure class (and its runbook) is about.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Sequence

CanaryDecision = Literal["promote", "abort_rollback"]

SUCCESS_RATE_MIN = 0.95  # k8s/prod/resources.yaml:270 successCondition
ERROR_RATE_MAX = 0.05  # k8s/prod/resources.yaml:279 successCondition
FAILURE_LIMIT = 5  # k8s/prod/resources.yaml: failureLimit on both metrics


class MalformedCanaryFixtureError(ValueError):
    """Raised when a canary measurement fixture is not shaped as documented."""


@dataclass(frozen=True)
class CanaryMeasurement:
    success_rate: float
    error_rate: float


@dataclass(frozen=True)
class CanaryVerdict:
    decision: CanaryDecision
    failing_metric: str | None
    measurements_evaluated: int
    success_rate_failures: int
    error_rate_failures: int


def _parse_measurements(fixture: Sequence[dict]) -> list[CanaryMeasurement]:
    if not isinstance(fixture, list) or not fixture:
        raise MalformedCanaryFixtureError(
            f"canary fixture must be a non-empty list of measurements, got {fixture!r}"
        )
    measurements: list[CanaryMeasurement] = []
    for i, item in enumerate(fixture):
        if (
            not isinstance(item, dict)
            or "success_rate" not in item
            or "error_rate" not in item
        ):
            raise MalformedCanaryFixtureError(
                f"measurement #{i} must be an object with 'success_rate' and "
                f"'error_rate' keys, got {item!r}"
            )
        success_rate, error_rate = item["success_rate"], item["error_rate"]
        for name, value in (("success_rate", success_rate), ("error_rate", error_rate)):
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise MalformedCanaryFixtureError(
                    f"measurement #{i} field {name!r} must be numeric, got {value!r}"
                )
            if not (0.0 <= value <= 1.0):
                raise MalformedCanaryFixtureError(
                    f"measurement #{i} field {name!r} must be a fraction in "
                    f"[0, 1] (this evaluator's queries return rates, not "
                    f"percentages), got {value!r}"
                )
        measurements.append(
            CanaryMeasurement(success_rate=success_rate, error_rate=error_rate)
        )
    return measurements


def evaluate_canary_analysis(
    fixture: Sequence[dict],
    *,
    success_rate_min: float = SUCCESS_RATE_MIN,
    error_rate_max: float = ERROR_RATE_MAX,
    failure_limit: int = FAILURE_LIMIT,
) -> CanaryVerdict:
    """Replays `fixture` (one dict per 1-minute measurement, in order)
    through both metrics' cumulative failure counters and returns the first
    verdict reached, or "promote" if `failure_limit` is never hit by either
    metric across the whole fixture.

    Raises `MalformedCanaryFixtureError` for a fixture that is not a
    non-empty list of `{"success_rate": float, "error_rate": float}`
    objects with values in `[0, 1]` — this evaluator's queries return rates
    (Prometheus `sum(rate(...)) / sum(rate(...))`), not percentages, so a
    fixture author accidentally writing `95` instead of `0.95` is caught
    here rather than silently always-passing (`95 >= 0.95` would otherwise
    be trivially true).
    """
    measurements = _parse_measurements(fixture)

    success_rate_failures = 0
    error_rate_failures = 0
    for i, m in enumerate(measurements):
        if m.success_rate < success_rate_min:
            success_rate_failures += 1
        if m.error_rate > error_rate_max:
            error_rate_failures += 1

        if success_rate_failures >= failure_limit:
            return CanaryVerdict(
                decision="abort_rollback",
                failing_metric="success-rate",
                measurements_evaluated=i + 1,
                success_rate_failures=success_rate_failures,
                error_rate_failures=error_rate_failures,
            )
        if error_rate_failures >= failure_limit:
            return CanaryVerdict(
                decision="abort_rollback",
                failing_metric="error-rate",
                measurements_evaluated=i + 1,
                success_rate_failures=success_rate_failures,
                error_rate_failures=error_rate_failures,
            )

    return CanaryVerdict(
        decision="promote",
        failing_metric=None,
        measurements_evaluated=len(measurements),
        success_rate_failures=success_rate_failures,
        error_rate_failures=error_rate_failures,
    )
