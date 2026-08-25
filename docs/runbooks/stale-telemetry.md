# Runbook: Stale telemetry

> Failure class 4 of 5 in plan todo 8. Index: [README.md](README.md).

## Source of truth

- Alert: `signoz_alert.mcp_telemetry_freshness`
  (`infra/superapp/modules/deployments/signoz-resilience-alerts.tf:301-339`,
  as read during this task — see [README.md](README.md#source-of-truth-caveat-infrasuperapp-review-state-at-the-time-of-writing)).
- `runbook_url` label: `https://runbooks.example.internal/mcp/telemetry-freshness`
  (placeholder host, see [README.md](README.md#what-linked-from-signoz-rules-means-here-precisely)).
- Dashboard: `signoz_dashboard.mcp_namespace_health`, widget "app-mcp-server
  telemetry freshness (trace count)"
  (`infra/superapp/modules/deployments/signoz-resilience-dashboard.tf:128-161`).
- App-side guarantee this runbook's diagnosis relies on: readiness is
  independent of the OTel collector (plan todo 7) — see
  `src/health/state.py:26-30` and
  `src/tests/unit/health/test_state.py::test_readiness_nunca_aciona_setup_de_tracing_otel`.

## Detection

**Signal**: `count()` of `app-mcp-server` trace spans over a 15-minute
window, `Below 1`, `AtLeastOnce`, **plus** `alertOnAbsent: true` — meaning it
also fires if the query returns *no time series at all* (not just a
below-threshold one), which is the expected shape of "the collector received
nothing." Severity `warning` (ticket-level).

**Fundamental, documented ambiguity** (this is the alert's own `.tf` header
comment, not new information invented here): a drop to near-zero
`app-mcp-server` spans can mean either (a) the app is actually down, or (b)
the collector/telemetry pipeline is down while the app is fine. This single
alert cannot tell the two apart — that is why the alert is `warning`/ticket,
not `critical`/page, and why the runbook below leads with cross-checking
[mcp-unavailable.md](mcp-unavailable.md) rather than assuming either cause.

## Diagnosis (read-only)

```bash
# Step 1 — disambiguate: is the app itself actually unavailable?
# If mcp_workload_unavailable (mcp-unavailable.md) is ALSO firing, treat this
# as a symptom of that outage, not a separate telemetry-pipeline problem.
kubectl argo rollouts get rollout mcp -n mcp --watch=false
curl -s https://<mcp-endpoint>/health   # liveness — should be 200 if the process is up at all

# Step 2 — if the app is healthy but telemetry is stale, check the collector.
kubectl get pods -n signoz -l app.kubernetes.io/name=signoz-otel-collector-auth
kubectl logs -n signoz -l app.kubernetes.io/name=signoz-otel-collector-auth --tail=200

# Step 3 — confirm the app's own OTLP export path, not just the collector.
kubectl logs -n mcp -l app=mcp --since=15m | grep -i 'otel\|otlp\|export'
kubectl get pods -n mcp -l app=mcp -o jsonpath='{.items[0].spec.containers[0].env}' | \
  grep -i OTEL_EXPORTER_OTLP_TRACES_ENDPOINT
```

**Critical cross-check that this runbook exists specifically to make
explicit**: `/health/ready` returning 200 during a telemetry outage is
*expected and correct*, not a false negative — readiness deliberately never
depends on the collector (plan todo 7's guardrail: "readiness ... remains
unaffected by collector failure"). Do not use readiness as evidence that
telemetry is fine; use `/health/detail`'s absence of a `redis`/`bigquery`
failure plus the Rollout's healthy replica count instead (Step 1 above).

## Remediation

1. **App is down (Step 1 shows it)**: this is not a telemetry-pipeline
   incident — follow [mcp-unavailable.md](mcp-unavailable.md) instead.
2. **Collector pods unhealthy/restarting**: `signoz-otel-collector-auth`
   scaling and disruption settings are `infra/superapp`'s
   `modules/deployments/signoz.tf` (plan todo 3 scope) — restarting or
   scaling that Deployment is a mutating action outside this repository and
   **REQUIRES EXPLICIT APPROVAL**; this runbook only diagnoses from the app
   side.
3. **App's `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` misconfigured or unreachable
   (e.g. after a Service rename in `infra/superapp`)**: this is an
   `app-mcp-server` manifest fix
   (`k8s/prod/resources.yaml` / `k8s/staging/resources.yaml`, `env:` block) —
   a reviewed PR, not a page-time hotfix, since `setup_metrics()`/
   `setup_tracing()` are designed to fail silently and never crash the app
   (`src/observability/metrics.py:100-170`) precisely so a telemetry
   misconfiguration degrades observability, not availability.
4. **Confirmed false positive (spans are actually flowing, e.g. verified via
   a manual SigNoz UI query)**: no remediation needed; this can happen if the
   metric's own ingestion has a delay independent of the underlying trace
   pipeline — note the false positive in the alert's next review rather than
   silencing it unilaterally.

## Rollback

Not applicable — this is not a rollout/deploy signal. If remediation step 3
above is the cause, the fix is forward (correct the manifest env var) rather
than a rollback to a previous revision that had the same-or-different
(possibly also wrong) endpoint configured.

## Recovery objectives

- **Detection latency**: up to 15 minutes (`evalWindow`) plus up to 5 minutes
  (`frequency`) — worst case ~20 minutes, same cadence as
  [unhealthy-workload.md](unhealthy-workload.md).
- **Recovery time**: no fixed RTO — bounded by whichever of the three
  remediation paths above applies. A collector restart is typically fast
  (pod reschedule, seconds to low minutes); a manifest fix requires a
  reviewed deploy.
- **Blast radius while this fires**: by design, **zero impact on MCP
  availability** — the entire point of plan todo 7's readiness/telemetry
  separation is that a telemetry outage never gates traffic. This runbook's
  "recovery objective" is therefore about restoring **observability**, not
  service.

## Escalation

Simulated/no-paging check only. This repository's drill
(`src/tests/resilience/test_drill_stale_telemetry.py`) proves two things
against fixtures: (1) the alert's `Below 1` / `alertOnAbsent` threshold logic
fires on a stale/absent span-count fixture and not on a healthy one, and
(2) `/health/ready` and `/health` stay 200 even when the OTel setup path
raises (reusing the same guarantee `src/tests/unit/health/test_state.py`
already locks in for plan todo 7 — this drill cross-references rather than
duplicates that test). No Discord message is sent by any of this; no
unacknowledged notification is treated as a passed test.

## Safety boundaries

- No `kubectl apply/exec/delete/patch`, `helm upgrade`, or `tofu apply`.
- No production pod deletion or collector restart performed by this
  runbook's diagnosis path — collector remediation is explicitly called out
  above as requiring approval and living outside this repository.
