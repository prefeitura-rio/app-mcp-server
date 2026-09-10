# MCP production resilience runbooks

Plan todo 8 (`.omo/plans/superapp-mcp-resilience-monitoring.md:124-130`) of the
"superapp-mcp-resilience-monitoring" work plan. One runbook per failure class
the plan names, each linked from the SigNoz alert (or Argo Rollouts resource)
that is expected to detect it in production.

## Index

| Failure class | Runbook | Detected by | Severity | Page or ticket? |
|---|---|---|---|---|
| MCP unavailable | [mcp-unavailable.md](mcp-unavailable.md) | `signoz_alert.mcp_workload_unavailable` (`runbook_url` slug `mcp/workload-unavailable`) | critical | Page |
| Unhealthy namespace workload | [unhealthy-workload.md](unhealthy-workload.md) | `signoz_alert.mcp_pod_lifecycle_unhealthy` (`runbook_url` slug `mcp/pod-lifecycle`) | warning | Ticket |
| Redis Sentinel failover | [redis-sentinel-failover.md](redis-sentinel-failover.md) | `signoz_alert.mcp_redis_workload_unavailable` (`runbook_url` slug `mcp/redis-unavailable`) | critical | Page |
| Stale telemetry | [stale-telemetry.md](stale-telemetry.md) | `signoz_alert.mcp_telemetry_freshness` (`runbook_url` slug `mcp/telemetry-freshness`) | warning | Ticket (ambiguous with an outage — see runbook) |
| Failed canary | [failed-canary.md](failed-canary.md) | `AnalysisTemplate/mcp-success-rate` referenced by `Rollout/mcp`'s canary `strategy.canary.analysis` (`k8s/prod/resources.yaml`) — **not** a SigNoz alert, see runbook for why | n/a (Rollout aborts automatically) | Ticket (auto-remediated) |

All five source resources live in `infra/superapp` (a sibling repository in
this workspace), which this repository (`app-mcp-server`) does not own or
modify. Every runbook below cites the exact resource address and, where the
resource file was inspected during this task, the line range it was read at
— so a reviewer can re-verify the claim against the current `infra/superapp`
tree instead of trusting this document blindly. Every resource referenced here
is committed to `infra/superapp`'s `main` branch and present in production's
Terraform state.

## What "linked from SigNoz rules" means here, precisely

Every alert rule in `infra/superapp` carries a `labels.runbook_url` pointing
directly at this repository's rendered docs, e.g.
`https://github.com/prefeitura-rio/app-mcp-server/blob/main/docs/runbooks/mcp-unavailable.md`
(see `modules/deployments/signoz-resilience-alerts.tf`). The placeholder host
`runbooks.example.internal` described in earlier versions of this document is
**obsolete** — the label resolves to a real URL today.

Two consequences not yet reconciled in this repository, tracked separately:
the resource type in the table below is still written as `signoz_alert` when
the applied resources are `signoz_rule`, and
`scripts/resilience/check_runbook_links.py` still validates the retired
`runbooks.example.internal/mcp/<slug>` scheme against
`scripts/resilience/resilience_signals.py`.

The `failed-canary` row has no `runbook_url` label to link from because no
`signoz_alert` models canary failure today (see
[failed-canary.md](failed-canary.md#source-of-truth)); the link instead runs
through a code comment placed next to `AnalysisTemplate/mcp-success-rate` in
`k8s/prod/resources.yaml` (added by this task, comment-only, no behavior
change).

## Common to every runbook below

- **No page-test claims from this repository.** None of the drills in
  `src/tests/resilience/` send a real Discord notification or otherwise page
  anyone. Every "page" scenario described in a runbook is a **simulated,
  no-paging** rehearsal of the detection/remediation logic, run entirely
  against fixtures and fakes. An unacknowledged notification is never treated
  as a passed test, because none of this ever sends one — this is stated
  explicitly in each runbook's Escalation section, not left implicit.
- **No cluster mutation from this repository.** Every "Diagnosis" command
  listed is read-only (`kubectl get/describe/logs`, `helm template`,
  `kubectl kustomize`/`kustomize build --dry-run`, `tofu plan`/`validate`).
  Any command that would mutate a cluster or apply infrastructure
  (`kubectl apply/exec/delete/patch`, `helm upgrade/install`, `tofu apply`) is
  marked **REQUIRES EXPLICIT APPROVAL** and is never run by an agent
  automatically, per `AGENTS.md` and `infra/AGENTS.md` in this workspace.
- **Recovery objectives are documented, not (all) measured live.** Where a
  number comes from a live measurement, the runbook says so and cites the
  evidence file. Where it is derived analytically from a chart/library's
  documented defaults (e.g. Redis Sentinel failover timing), the runbook says
  that instead — the same honesty convention used by
  `infra/superapp/modules/deployments/signoz-resilience-alerts.tf`'s own
  header comment and by this workspace's Task 5 evidence
  (`.omo/evidence/task-5-superapp-mcp-resilience-monitoring/DoneClaim.md`).

## Isolated drill harness and release-readiness checker

- `src/tests/resilience/` — pytest-based, fixture-driven drills. No real
  Kubernetes cluster, Redis, or SigNoz instance is contacted; `asyncio.sleep`
  is monkeypatched out everywhere so no drill actually waits. Run with:

  ```bash
  uv run pytest src/tests/resilience -v
  ```

- `scripts/resilience/release_readiness_check.py` — a release-readiness
  checker that fails a change review when a required alert, dashboard widget,
  route policy, or rollback/failover drill outcome is missing from a signals
  manifest + drill-results pair. See `scripts/resilience/README.md` for exact
  invocation and the manual QA transcript under
  `.omo/evidence/task-8-superapp-mcp-resilience-monitoring/` for a captured
  pass/fail pair.

- `scripts/resilience/check_runbook_links.py` — a documentation/link checker:
  confirms every relative link between files in `docs/runbooks/` resolves,
  and that every `runbook_url` slug referenced by a runbook's "Source of
  truth" section is present (once) in `scripts/resilience/signals_manifest.json`.
