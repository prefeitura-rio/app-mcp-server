# Runbook: Failed canary

> Failure class 5 of 5 in plan todo 8. Index: [README.md](README.md).

## Source of truth

- Resource: `AnalysisTemplate/mcp-success-rate`, referenced by
  `Rollout/mcp`'s `strategy.canary.analysis.templates`
  (`k8s/prod/resources.yaml:19-48,260-285`, this repository). A comment
  linking to this runbook was added next to that resource by this task
  (comment-only, no behavior change — see this file's git history).
- **No `signoz_alert` models this failure class.** This is not an omission
  in this runbook — `infra/superapp/modules/deployments/signoz-resilience-alerts.tf`
  has no canary-specific resource, and the Rollout does not use
  `trafficRouting.istio`, so a canary abort is entirely an Argo Rollouts
  controller decision based on its own Prometheus queries against
  `prometheus-server.istio-system.svc.cluster.local:9090`, independent of SigNoz.
  The two `mcp_slo_error_budget_long_burn_*` SigNoz alerts
  (`infra/superapp/modules/deployments/signoz-resilience-alerts.tf:44-136`)
  measure a related but distinct signal (trace-based error rate over 1h/6h
  windows, all traffic) and may corroborate a canary failure if the bad
  canary's errors are frequent/severe enough to move the 1h aggregate, but
  are not expected to fire reliably for a canary confined to 25-50% traffic
  weight for a few minutes — this is stated explicitly rather than implied.

## Detection

**Signal**: `Rollout/mcp`'s canary strategy
(`k8s/prod/resources.yaml:19-48`) runs `AnalysisTemplate/mcp-success-rate`
in the background starting at `startingStep: 2`, with two independently
evaluated metrics, each polled every `interval: 1m`:

| Metric | Query (simplified) | `successCondition` | `failureLimit` |
|---|---|---|---|
| `success-rate` | non-5xx / total `istio_requests_total` for `mcp-canary`, rate over 2m | `result[0] >= 0.95` | `5` |
| `error-rate` | 5xx / total `istio_requests_total` for `mcp-canary`, rate over 2m | `result[0] <= 0.05` | `5` |

Per Argo Rollouts' documented `failureLimit` semantics (no
`consecutiveSuccessLimit` is set here, so this is the "only FailureLimit
applicable" case): each metric independently accumulates a **cumulative**
count of failed measurements (not required to be consecutive), and the
analysis run — and therefore the Rollout — is marked `Failed` once either
metric's cumulative failure count reaches `5`. On `Failed`, "the basic
canary strategy always rolls back to the stable version upon an abort"
(cited in this repository's own code comment,
`k8s/prod/resources.yaml:26-33`, from Argo Rollouts' `docs/features/analysis.md`
and `docs/features/scaledown-aborted-rs.md`).

Both metric queries have an `or vector(1)` / `or vector(0)` fallback for when
`mcp-canary` receives little or no real traffic (documented in this
repository's own comment, `k8s/prod/resources.yaml:250-259`) — a canary step
with near-zero traffic evaluates as "healthy" by construction, which is a
known limitation of using replica-count-based traffic splitting instead of
Istio `trafficRouting` (also documented in that same comment), not something
this runbook can remediate.

## Diagnosis (read-only)

```bash
# Current rollout/analysis state: step, canary weight, analysis run status.
kubectl argo rollouts get rollout mcp -n mcp --watch=false

# The AnalysisRun's per-metric measurement history (this is what decided abort/promote).
kubectl get analysisrun -n mcp -l rollouts-pod-template-hash=<canary-hash> -o yaml

# Canary pod logs/errors during the window that failed.
kubectl logs -n mcp -l app=mcp,rollouts-pod-template-hash=<canary-hash> --tail=200

# Corroborating signal (see Source of truth above — best-effort, not guaranteed to fire).
# Check the SigNoz "app-mcp-server SLO error-budget long-window burn" alerts/dashboard
# for a matching uptick during the same window.
```

## Remediation / Rollback

If the analysis has already reached `Failed`: **nothing to do** — Argo
Rollouts has already automatically scaled down the canary ReplicaSet and
`stableService`/`mcp` continues serving 100% of traffic on the previous
revision. This is the intended, already-implemented rollback path; this
runbook's job at that point is root-causing the failing revision (via
Diagnosis above) before re-attempting a fixed rollout, not performing a
rollback action.

If the analysis is `Inconclusive` (neither `failureLimit` violated nor a
`consecutiveSuccessLimit` — not configured here — satisfied) or stuck: per
Argo Rollouts' documented behavior, a background analysis terminated
prematurely (e.g. rollout aborted, or reaching the end without `count`
specified) is treated as `Successful` **unless** `failureLimit` was already
violated. An operator manually promoting or aborting a stuck rollout
(`kubectl argo rollouts promote|abort mcp -n mcp`) is a mutating action and
**REQUIRES EXPLICIT APPROVAL** per `AGENTS.md` — it is not performed
automatically by this runbook.

## Recovery objectives

- **Detection latency**: each metric is polled every `interval: 1m`; with
  `failureLimit: 5` and no `consecutiveSuccessLimit`, the fastest possible
  abort is 5 consecutive failing 1-minute measurements ≈ **5 minutes** of
  sustained failure (cumulative, so intermittent failures spread across a
  longer window still eventually trip it, just slower).
- **Recovery time (RTO)**: effectively **immediate** once the analysis run
  reaches `Failed` — Argo Rollouts' native abort behavior scales the canary
  to zero and leaves the stable revision serving 100%, with no additional
  action required. The bulk of the "recovery time" in practice is the
  ~5-minute detection window above, not the rollback action itself.
- **Data loss (RPO)**: not applicable — this is a traffic-routing rollback,
  not a data-layer failover; no user data is at risk from an aborted canary
  by construction (canary and stable pods share the same `mcp-redis`
  backend).

## Escalation

Simulated/no-paging check only. This repository's drill
(`src/tests/resilience/test_drill_failed_canary.py`) reimplements the
`failureLimit`-based abort/promote decision (per-metric cumulative failure
count against `success-rate ≥ 0.95` / `error-rate ≤ 0.05`, `failureLimit: 5`,
values copied from `k8s/prod/resources.yaml` rather than re-invented) against
fixture measurement sequences, and asserts a genuinely bad canary aborts
while a genuinely good one promotes. It does not run against a real Argo
Rollouts controller, does not exercise the live `or vector(1)`/`or vector(0)`
low-traffic fallback against real Istio metrics, and does not contact
Discord. No unacknowledged notification is treated as a passed test.

## Safety boundaries

- No `kubectl argo rollouts promote/abort/retry`, `kubectl apply/exec/delete/patch`,
  `helm upgrade`, or `tofu apply` performed by this runbook or its drill.
- No production canary is triggered, forced to fail, or force-promoted by
  this task — the drill exercises the abort/promote *decision logic* against
  fixtures, never a live Rollout.
