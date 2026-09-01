# Runbook: Redis Sentinel failover

> Failure class 3 of 5 in plan todo 8. Index: [README.md](README.md).

## Source of truth

- Alert: `signoz_alert.mcp_redis_workload_unavailable`
  (`infra/superapp/modules/deployments/signoz-resilience-alerts.tf:198-244`,
  as read during this task — see [README.md](README.md#source-of-truth-caveat-infrasuperapp-review-state-at-the-time-of-writing)).
  This alert is explicitly documented (in its own `.tf` header comment) as a
  **generic pod/deployment-name-pattern proxy** (`k8s.deployment.name CONTAINS 'redis'`)
  rather than a Sentinel-quorum-specific metric — no dedicated Sentinel
  quorum/HAProxy backend-health SigNoz metric exists yet.
- `runbook_url` label: `https://runbooks.example.internal/mcp/redis-unavailable`
  (placeholder host, see [README.md](README.md#what-linked-from-signoz-rules-means-here-precisely)).
- Topology and failover-timing analysis: `infra/superapp`'s Task-5 worktree
  documents this in `docs/redis-sentinel-ha-mcp.md` (verified present, read
  during this task, at commit `9cc520a` of
  `https://github.com/prefeitura-rio/iac-superapp.git` in a separate
  worktree — **not yet on `infra/superapp`'s checked-out `main` branch** in
  this workspace as of this task; expect it at
  `infra/superapp/docs/redis-sentinel-ha-mcp.md` once that branch merges).
  Every number in this runbook's Recovery objectives section is copied from
  that file, not re-derived.
- Client-side retry contract this runbook's Diagnosis relies on:
  `RedisBackend` in
  `src/tools/multi_step_service/core/state.py` (plan todo 6, already merged
  into this repository — see `src/tests/unit/tools/test_redis_backend_failover.py`).

## Detection

**Signal**: same desired-vs-available formula as
[mcp-unavailable.md](mcp-unavailable.md), filtered to
`k8s.deployment.name CONTAINS 'redis'` instead of excluding it, over a
10-minute window, `Above 0`, `AtLeastOnce`. Severity `critical` (page).

Because this is a generic name-pattern match, it fires for **any** Redis-named
pod being unavailable, not specifically "Sentinel is mid-failover" — a
Sentinel failover that completes within its normal ~10-20s window (see
Recovery objectives) is very unlikely to be caught by a 10-minute-sustained
alert at all; if this alert fires, the more likely cause is a
**stuck/prolonged** failover, insufficient Sentinel quorum, or a genuine
capacity/scheduling problem with the `redis-ha` StatefulSet or HAProxy
Deployment.

## Diagnosis (read-only)

```bash
# Redis/Sentinel/HAProxy pod status.
kubectl get pods -n mcp -l app.kubernetes.io/name=redis-ha
kubectl get statefulset -n mcp   # redis-ha-server, 3 replicas expected (plan todo 5)
kubectl get deployment -n mcp -l app=redis-ha-haproxy

# Which pod Sentinel currently reports as primary (read-only INFO/SENTINEL commands).
kubectl exec -n mcp redis-redis-ha-server-0 -- redis-cli -p 26379 sentinel masters
kubectl exec -n mcp redis-redis-ha-server-0 -- redis-cli -p 26379 sentinel ckquorum mcpmaster

# HAProxy's own view of backend health.
kubectl logs -n mcp -l app=redis-ha-haproxy --tail=100

# Client-side symptom: is app-mcp-server actually seeing ConnectionError,
# and is it recovering on its own via the bounded retry budget (task 6)?
kubectl logs -n mcp -l app=mcp --since=15m | grep -i 'ConnectionError\|redis'
curl -s https://<mcp-endpoint>/health/ready   # 503 + reason=redis_unavailable while affected pod is out
```

`kubectl exec` above is a **read-only** Redis/Sentinel introspection command
(`sentinel masters`, `sentinel ckquorum`, `redis-cli INFO`) run against an
existing pod — it does not mutate cluster state. It is listed here for
completeness of the diagnosis path; per this workspace's `AGENTS.md`, running
it against a real cluster during an actual incident is normal on-call
practice, but this repository's own drills (see below) never execute it —
they simulate the same signal with a fake Redis client instead.

## Remediation

1. **Failover completed within the expected window (~10-20s, see below) and
   the app has already recovered**: no action needed. Confirm via
   `/health/ready` returning `{"status": "ready"}` again and
   `RedisBackend`'s bounded retry (task 6) having absorbed the
   `ConnectionError` window without exhausting its budget.
2. **Failover stuck / quorum not reached**: check `sentinel ckquorum` output
   above — if fewer than `quorum: 2` Sentinels are reachable (e.g. two of the
   three Sentinel-carrying pods are down simultaneously, a correlated
   failure the anti-affinity/topology-spread settings in
   `infra/superapp/modules/deployments/yamls/redis-ha-mcp/values.yaml`
   are meant to make unlikely but not impossible), failover cannot proceed
   automatically. This is an `infra/superapp` capacity/scheduling
   investigation (pod anti-affinity, node availability), not something
   `app-mcp-server` can remediate from this repository.
3. **Client retry budget exhausted (readiness stuck `not_ready` /
   `redis_unavailable` beyond ~60s)**: per
   `test_orcamento_padrao_e_limitado_e_documentado`
   (`src/tests/unit/tools/test_redis_backend_failover.py:262-279`), the
   default retry budget totals **10-60 seconds** of cumulative backoff before
   giving up on a single operation — but a *new* incoming request starts a
   *new* retry sequence, so the pod keeps trying every request, not just
   once. If it is still failing well past the ~10-20s expected Sentinel
   window, treat this the same as case 2 above (stuck failover), not as a
   client-side bug.

## Rollback

Not applicable in the deploy sense — there is no "previous Redis revision" to
roll back to. If remediation requires operator intervention on the
`redis-ha` chart itself (e.g. forcing a manual Sentinel failover via
`redis-cli sentinel failover mcpmaster`, or restarting a stuck Sentinel pod),
that is a mutating action on `infra/superapp`-managed infrastructure and
**REQUIRES EXPLICIT APPROVAL** per `AGENTS.md` before any agent runs it — it
is out of scope for an automated remediation step in this runbook.

## Recovery objectives

Copied verbatim (not re-derived) from `infra/superapp`'s
`docs/redis-sentinel-ha-mcp.md` (see Source of truth above):

- **RTO — expected client-visible write-unavailability window: roughly
  10-20 seconds** for a clean primary crash, dominated by the Sentinel
  `down-after-milliseconds: 10000` (10s) detection delay, plus low
  single-digit seconds for election/promotion, plus ~1-2s for HAProxy to stop
  routing to the dead primary and start routing to the new one.
  `failover-timeout: 180000` (180s) is a safety ceiling for one failover
  attempt/retry, not the typical case — a failover that takes materially
  longer than ~20s but less than 180s is still "working as designed," just
  slower than the common case, and should not itself be treated as an
  incident distinct from this runbook.
- **RPO — reconstructible asynchronous-write loss, bounded to the tail of the
  most recent writes**, from two additive sources on an *unclean* primary
  crash only (not a graceful shutdown, which flushes AOF via the pod's
  `preStop` hook):
  - up to ~1s of the most recently acknowledged writes, from
    `appendfsync everysec` fsyncing once per second rather than per write;
  - asynchronous replication lag, bounded to roughly the last few seconds of
    writes under sustained load (near-zero under light load), by
    `min-replicas-to-write: 1` / `min-replicas-max-lag: 5` blocking writes on
    the primary if no replica has caught up within 5s.
  This loss profile is consistent with Redis's role here as a
  workflow/session cache for `multi_step_service`
  (`src/tools/multi_step_service/core/state.py`) — state is either derived
  from BigQuery or resubmitted by the user, not the sole source of truth, so
  a few seconds of lost state is recoverable rather than catastrophic. This
  runbook does not claim strong write consistency during the failover
  window, matching plan todo 6's explicit guardrail against that claim.
- **Client reconnect budget**: 10-60s cumulative backoff per operation (see
  Diagnosis case 3 above) — deliberately covers the ~10-20s expected RTO with
  margin, without being unbounded.

These numbers are **analytically derived from the pinned chart's documented
Sentinel/HAProxy defaults**, not measured against a live failover — see
`infra/superapp`'s Task-5 evidence
(`.omo/evidence/task-5-superapp-mcp-resilience-monitoring/DoneClaim.md`) for
why a live `kubectl delete pod` failover drill was explicitly deferred to
this task (todo 8) and why this task still does not run it (no cluster
access / no pod deletion in this delegation's scope — see this runbook's
drill below for what is verified instead).

## Escalation

Simulated/no-paging check only. This repository's drill
(`src/tests/resilience/test_drill_redis_sentinel_failover.py`) drives the
real `RedisBackend` retry logic against a fake Redis client that reproduces
the ~10s Sentinel detection window as a sequence of `ConnectionError`s
followed by recovery, and asserts recovery completes within the documented
budget — it does not delete any pod, real or "isolated-namespace," and does
not contact Discord. No unacknowledged notification is treated as a passed
page test. A real Sentinel `CKQUORUM`/live-failover drill against an actual
(even isolated-namespace) cluster remains explicitly out of scope for this
delegation (see Recovery objectives above) and would need a follow-up
session with cluster-mutation rights and explicit approval, per
`infra/AGENTS.md`.

## Safety boundaries

- No `kubectl apply/exec/delete/patch` (beyond the read-only `sentinel
  masters`/`ckquorum`/`INFO` introspection listed in Diagnosis, which this
  repository's own drills never execute against a real cluster), `helm
  upgrade/install`, or `tofu apply`.
- No pod deletion — real, staging, or "isolated test namespace" — as part of
  drilling this runbook. The drill harness proves the client-side retry
  contract with a fake Redis client instead.
