# Runbook: Unhealthy namespace workload (OOM / eviction)

> Failure class 2 of 5 in plan todo 8. Index: [README.md](README.md).

## Source of truth

- Alert: `signoz_alert.mcp_pod_lifecycle_unhealthy`
  (`infra/superapp/modules/deployments/signoz-resilience-alerts.tf:250-293`,
  as read during this task — see [README.md](README.md#source-of-truth-caveat-infrasuperapp-review-state-at-the-time-of-writing)).
- `runbook_url` label: `https://runbooks.example.internal/mcp/pod-lifecycle`
  (placeholder host, see [README.md](README.md#what-linked-from-signoz-rules-means-here-precisely)).
- No dedicated dashboard widget for this signal exists yet in
  `signoz_dashboard.mcp_namespace_health` — the closest is the "MCP container
  restarts (delta, 10m window)" widget
  (`infra/superapp/modules/deployments/signoz-resilience-dashboard.tf:89-127`),
  which shows restart counts generally, not specifically OOM/eviction
  reasons. Called out as a known gap below, not silently assumed to exist.

## Detection

**Signal**: `count(k8s.container.restarts where last_terminated_reason=OOMKilled) + count(k8s.pod.status_reason in {Evicted, NodeLost, Shutdown})`,
namespace `mcp`, over a 15-minute window, `Above 0`, `AtLeastOnce`. Severity
`warning` (ticket-level, **not** page) — a single OOM/eviction is a
diagnostic symptom of a specific pod, not necessarily a current
service-wide outage; if it is causing an outage, that shows up separately
and at `critical` severity via [mcp-unavailable.md](mcp-unavailable.md).

**Known gap, documented rather than hidden**: this alert cannot distinguish
"MCP application pod OOMKilled" from "MCP Redis pod OOMKilled" — the query
filters only on `k8s.namespace.name = 'mcp'`, not on
`k8s.deployment.name NOT CONTAINS 'redis'` the way
[mcp-unavailable.md](mcp-unavailable.md)'s query does. Diagnosis below
includes an explicit step to disambiguate which workload it was.

## Diagnosis (read-only)

```bash
# Which pod(s) restarted and why — look for OOMKilled / Evicted specifically.
kubectl get pods -n mcp -o json | \
  jq -r '.items[] | select(.status.containerStatuses[]?.lastState.terminated.reason=="OOMKilled") | .metadata.name'
kubectl get pods -n mcp -o wide | grep -E 'Evicted|OOMKilled' || true
kubectl get events -n mcp --sort-by='.lastTimestamp' | grep -iE 'oom|evict' 

# Disambiguate MCP application pod vs Redis pod (this alert's known gap above).
kubectl get pods -n mcp -l app=mcp
kubectl get pods -n mcp -l app.kubernetes.io/name=redis-ha    # DandyDeveloper chart label, per plan todo 5

# For an OOMKilled MCP pod: was it actually exceeding its memory limit,
# or a transient spike? Compare against the configured limit
# (k8s/prod/resources.yaml: resources.limits.memory).
kubectl describe pod -n mcp <pod-name> | grep -A3 'Last State'
kubectl top pod -n mcp <pod-name> --containers
```

## Remediation

1. **Isolated MCP application-pod OOM, not repeating**: no action required —
   Kubernetes already restarted the container; `PodDisruptionBudget/mcp-pdb`
   and `HorizontalPodAutoscaler/mcp-hpa` (`k8s/prod/resources.yaml`) absorb
   the transient capacity loss. Confirm via the diagnosis commands above that
   [mcp-unavailable.md](mcp-unavailable.md) did not also fire.
2. **Repeating MCP application-pod OOM (CrashLoopBackOff pattern)**: the
   configured `resources.limits.memory` (`1536Mi` prod / `1536Mi` staging,
   `k8s/prod/resources.yaml` / `k8s/staging/resources.yaml`) is genuinely
   insufficient for current load (e.g. a workload doing heavier
   geopandas/pandas processing than usual) — raising the limit is a reviewed
   `app-mcp-server` PR, not a page-time hotfix.
3. **Redis pod OOM/eviction**: this is `infra/superapp`'s DandyDeveloper
   `redis-ha` chart resource sizing — see
   [redis-sentinel-failover.md](redis-sentinel-failover.md) for what happens
   to client availability while a Redis pod cycles, and escalate the sizing
   question per that runbook's Escalation section (infra/superapp change,
   out of this repository's scope).
4. **Eviction (not OOM)**: check `kubectl describe node` for the affected
   node — evictions are usually node-level disk/memory pressure, a
   `gke`-module (node pool) concern, not an application concern.

## Rollback

Not applicable in the usual sense — this alert does not correspond to a
rollout step. If a repeating OOM was introduced by the currently-serving
Rollout revision, treat it as a [failed-canary.md](failed-canary.md) scenario
for the next deploy (the error-rate/success-rate analysis may not directly
catch a slow-onset OOM within a single canary step's pause window, which is
itself a known gap — a memory-limit regression that only manifests after
sustained load past the canary's `pause: 5m`/`10m` windows could reach 100%
weight before OOMing). No automatic rollback exists for this specific signal
today; it is ticket-level exactly because remediation is a reviewed capacity
change, not an automated action.

## Recovery objectives

- **Detection latency**: up to 15 minutes (`evalWindow`) plus up to 5 minutes
  (`frequency`) — worst case ~20 minutes. This is intentionally slower than
  [mcp-unavailable.md](mcp-unavailable.md)'s ~11 minutes because it is a
  ticket-level diagnostic signal, not a page.
- **Recovery time**: bounded by Kubernetes' own container-restart behavior
  (seconds) for an isolated event; unbounded for a repeating pattern until a
  human raises the resource limit (see Remediation #2).

## Escalation

Simulated/no-paging check only. This repository's drill
(`src/tests/resilience/test_drill_unhealthy_workload.py`) proves the alert's
threshold-firing logic against fixture OOM/eviction counts; it does not
contact Discord, and no unacknowledged notification is treated as a passed
test anywhere in this repository. Ticket-level severity means routing goes to
the team's issue tracker (Jira, per this workspace's existing conventions),
not the on-call page path — that routing is `infra/superapp`'s
`signoz_route_policy` configuration (out of this repository's scope) and is
not asserted to exist by anything in this runbook.

## Safety boundaries

- No `kubectl apply/exec/delete/patch`, `helm upgrade`, or `tofu apply` in
  this runbook's automated diagnosis path.
- No production pod deletion as part of drilling this runbook.
