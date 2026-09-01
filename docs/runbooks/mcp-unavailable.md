# Runbook: MCP unavailable

> Failure class 1 of 5 in plan todo 8. Index: [README.md](README.md).

## Source of truth

- Alert: `signoz_alert.mcp_workload_unavailable`
  (`infra/superapp/modules/deployments/signoz-resilience-alerts.tf:142-188`,
  as read during this task — see [README.md](README.md#source-of-truth-caveat-infrasuperapp-review-state-at-the-time-of-writing)
  for its current (uncommitted) review state in `infra/superapp`).
- `runbook_url` label on that resource: `https://runbooks.example.internal/mcp/workload-unavailable`
  (placeholder host — this file is the intended target; see
  [README.md](README.md#what-linked-from-signoz-rules-means-here-precisely)).
- Dashboard: `signoz_dashboard.mcp_namespace_health`, widget "MCP workload:
  desired vs available" (`infra/superapp/modules/deployments/signoz-resilience-dashboard.tf:34-88`).
- Rollout/capacity resources this runbook's remediation operates on:
  `app-mcp-server/k8s/prod/resources.yaml` (`Rollout/mcp`, `HorizontalPodAutoscaler/mcp-hpa`,
  `PodDisruptionBudget/mcp-pdb`).

## Detection

**Signal**: `k8s.deployment.desired − k8s.deployment.available > 0`, sustained
for 10 minutes, for any non-Redis deployment in `namespace=mcp`. Evaluated
every 1 minute (`evaluation.spec.frequency`), `matchType: AtLeastOnce` — i.e.
it fires the first time the gap is seen at all within the rolling 10-minute
window, not only if it holds for the entire window. Severity `critical`,
routed to the existing `Discord #alerts-signoz` channel via
`signoz_route_policy.mcp_page_level_alerts`
(`infra/superapp/modules/deployments/signoz-resilience-route-policy.tf`).

This is distinct from [unhealthy-workload.md](unhealthy-workload.md): this
alert fires when the Rollout cannot keep enough pods *Ready* (whatever the
cause); the OOM/eviction alert is a diagnostic symptom that may explain *why*.
Check both — a workload-unavailable page with a concurrent pod-lifecycle
ticket usually means "OOM-looping pods, insufficient replicas to absorb it."

**Prerequisite this alert depends on**: the `k8s.deployment.desired`/
`k8s.deployment.available` metrics require `signoz.tf`'s `helm_release.k8s_infra`
clusterMetrics preset to actually be collecting and ingesting data. If this
alert has never fired and the dashboard widget shows no data at all (not
just zero), treat that as [stale-telemetry.md](stale-telemetry.md) first —
an app that is actually down produces both alerts; a telemetry-only outage
produces neither, which looks deceptively like "everything is fine."

## Diagnosis (read-only)

```bash
# Rollout status: current step, stable/canary replica counts, abort state.
kubectl argo rollouts get rollout mcp -n mcp --watch=false

# Raw desired/available/ready counts, independent of the Rollout CRD view.
kubectl get rollout mcp -n mcp -o jsonpath='{.status.replicas}{"\n"}{.status.availableReplicas}{"\n"}{.status.readyReplicas}{"\n"}'

# Per-pod state: CrashLoopBackOff, ImagePullBackOff, Pending (unschedulable), OOMKilled.
kubectl get pods -n mcp -l app=mcp -o wide
kubectl describe pod -n mcp <pod-name>   # Events section explains Pending/CrashLoop
kubectl logs -n mcp <pod-name> --previous --tail=200   # last logs before a restart

# HPA/PDB state: is the autoscaler trying to scale up and failing to schedule?
kubectl get hpa mcp-hpa -n mcp
kubectl get pdb mcp-pdb -n mcp

# Node/zone capacity, if pods are Pending: is the pods-pool node pool out of room?
kubectl get nodes -l cloud.google.com/gke-nodepool=pods-pool
kubectl top nodes
```

## Remediation

1. **CrashLoopBackOff / config error** (deterministic — see
   `docs/decisions/health-checks-e-preflight.md` D1): the previous stable
   Rollout revision is still serving traffic (canary strategy never routes
   more than the current step's weight to a broken revision, and Argo
   Rollouts' analysis — see [failed-canary.md](failed-canary.md) — should
   already have aborted it). Confirm the stable ReplicaSet is healthy before
   doing anything else:
   ```bash
   kubectl argo rollouts get rollout mcp -n mcp --watch=false | grep -A5 'STABLE\|Stable'
   ```
2. **Insufficient replicas due to node-pool capacity** (Pending pods): this is
   an infra/superapp `gke` module change (node pool min/max), not an
   app-mcp-server change — escalate per below rather than editing
   `k8s/prod/resources.yaml`'s replica floor to "fix" a scheduling problem.
3. **Genuine spike beyond `HorizontalPodAutoscaler.maxReplicas` (8)**: confirm
   via `kubectl top pods -n mcp` that CPU/memory utilization is the limiting
   factor, not Redis or an upstream API. Raising `maxReplicas` is a reviewed
   `app-mcp-server` PR (`k8s/prod/resources.yaml`), not a page-time hotfix.
4. **If the Rollout itself is stuck mid-canary and not auto-aborting**: see
   [failed-canary.md](failed-canary.md) for the abort/rollback path.

None of the above requires `kubectl apply/exec/delete/patch` under normal
operation — the Rollout, HPA, and PDB are designed to self-heal once the
underlying cause (bad image, capacity) is fixed. If a manual
`kubectl delete pod` (to force a reschedule) or `kubectl rollout restart` is
genuinely needed, that is a mutating action requiring explicit human
approval per `AGENTS.md`; it is not part of this runbook's automated
diagnosis path.

## Rollback

The canary strategy (`k8s/prod/resources.yaml`, `Rollout/mcp`) already routes
through `analysis.templates: [mcp-success-rate]` with automatic abort — see
[failed-canary.md](failed-canary.md#remediation--rollback) for the exact
abort/rollback mechanics. If workload-unavailable fires **during** an active
rollout, that is the expected trigger for that automatic path; no manual
`kubectl argo rollouts abort` should be necessary. If it fires **outside** an
active rollout (steady state, `stableService` at 100%), there is no "previous
revision" distinct from the current one to roll back to — the fix is
capacity/config remediation above, not a rollback.

## Recovery objectives

- **Detection latency**: up to 10 minutes (the alert's `evalWindow`) plus up
  to 1 minute (evaluation `frequency`) — i.e. worst case ~11 minutes from the
  first unavailable pod to the page firing. This is a page-level SLO
  contributor; if it needs to be faster, the tradeoff is more alert noise
  from transient single-pod blips during normal rolling updates (documented
  here, not silently changed).
- **Recovery time**: bounded by whatever the underlying cause is (see
  Remediation) — this alert does not have a fixed RTO of its own, unlike
  [redis-sentinel-failover.md](redis-sentinel-failover.md)'s Sentinel-driven
  RTO.
- **Capacity floor while degraded**: `PodDisruptionBudget/mcp-pdb`
  (`minAvailable: 2`) and `HorizontalPodAutoscaler/mcp-hpa`
  (`minReplicas: 3`) mean losing one pod never drops below 2 Ready
  replicas — this alert is about the Rollout failing to *maintain* that
  floor, not about the floor itself being adequate.

## Escalation

Simulated/no-paging check only (see [README.md](README.md#common-to-every-runbook-below)):
this repository's drills (`src/tests/resilience/test_drill_mcp_unavailable.py`)
prove the alert's threshold logic fires on the documented fixture and that
the app's `/health` vs `/health/ready` split behaves as designed under a
Redis-unavailable pod — they do not send a real Discord message, and no
unacknowledged notification anywhere in this repository's evidence is treated
as a passed page test. A real production page still requires a human
on-call response per the existing (unchanged by this task) Discord
`#alerts-signoz` on-call process.

## Safety boundaries

- No `kubectl apply/exec/delete/patch`, `helm upgrade`, or `tofu apply` in
  this runbook's automated diagnosis path.
- No production pod deletion, ever, as part of drilling this runbook — the
  isolated drill harness (`src/tests/resilience/test_drill_mcp_unavailable.py`)
  proves the detection/readiness logic against fixtures and fakes only.
