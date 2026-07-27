# Decision: Sentry vs. `error_interceptor` for error tracking

- **Jira**: CHATR-113 (subtask of epic CHATR-101 — "observabilidade e APM real em produção")
- **Status**: Decided — do not adopt Sentry now
- **Date**: 2026-07-24
- **Related**: CHATR-110 (OpenTelemetry → SigNoz tracing, shipped, PR #139), CHATR-112 (SigNoz alerts, concurrent)
- **Scope note**: this is a decision/documentation artifact only. No code, dependency, or config changes were made as part of this ticket.

## Location of this document

This repo has no existing ADR/decision-log directory (checked: no `docs/`, `decisions/`, or `adr*` directory; the only prior convention is flat topic-named markdown at the repo root — `AUTHENTICATION.md`, `TOOL_VERSIONING.md`, `README.md`). Rather than add a fifth root-level file, this introduces `docs/decisions/` as a lightweight ADR convention, since none exists yet and the epic will likely produce more of these. Filed under the ticket ID for traceability.

---

## 1. How `error_interceptor` works today

Source: `src/utils/error_interceptor.py` (462 lines), config: `src/config/env.py:43-44`, ~19 call sites across `src/tools/**` and `src/utils/**` (found via `grep -r error_interceptor src/` / `grep -r ERROR_INTERCEPTOR src/`).

### What it is

A single-purpose, in-house **outbound webhook reporter**. It has no storage, no query UI, and no dashboard of its own — it POSTs a JSON payload to an external URL and forgets. Whatever consumes `ERROR_INTERCEPTOR_URL` (a separate, external "sistema de monitoramento" not present in this repo) is where any grouping/alerting/viewing would have to happen; that receiving system is a black box from this codebase's point of view.

### Configuration

```python
# src/config/env.py:43-44
ERROR_INTERCEPTOR_URL = getenv_or_action("ERROR_INTERCEPTOR_URL")
ERROR_INTERCEPTOR_TOKEN = getenv_or_action("ERROR_INTERCEPTOR_TOKEN")
```

Both are optional (opt-in, same posture as OTel tracing). If either is missing, `send_error_to_interceptor()` logs a warning and returns `False` — no exception, no blocked startup (`error_interceptor.py:61-67`). In the cluster, both are injected via `envFrom: secretRef: name: mcp-secrets` in `k8s/prod/resources.yaml` / `k8s/staging/resources.yaml` (not committed as plaintext — consistent with them being credentials to a third-party/internal endpoint).

### What it captures and sends

Core function `send_error_to_interceptor()` (`error_interceptor.py:22-137`) POSTs this payload via `httpx.AsyncClient(timeout=10.0)`, header `x-api-key: ERROR_INTERCEPTOR_TOKEN`:

```json
{
  "customer_whatsapp_number": "<user id, PII>",
  "source": "<flattened 'key=value | key=value' string, e.g. 'source=mcp | tool=multi_step_service | workflow=iptu_pagamento | function=consultar_guias'>",
  "flowname": "<same as source, human label>",
  "api_endpoint": "<URL called, or 'internal://<ErrorType>' for non-HTTP errors>",
  "input_body": "<stringified request args/body>",
  "http_status_code": 500,
  "error_response": "{\"error_message\": \"...\", \"traceback\": \"...\"}"
}
```

Three call patterns sit on top of this primitive:

- **`send_api_error()`** — for failed calls to external APIs (has real `api_endpoint`/`status_code`).
- **`send_general_error()`** — for internal errors; fakes `api_endpoint` as `internal://<ErrorType>`.
- **`@interceptor(source=..., error_types=..., extract_user_id=..., extract_source=...)`** — a decorator used on ~12 tool functions (`feedback_tools.py`, `cor_alert_tools.py`, `dharma_search.py`, `pluscode_service.py`, `search.py`, `memory.py`, `crawl_pages/*.py`, `divida_ativa.py`, `google_search/gemini_service.py`, `web_search_surkai.py`, etc.). It catches the given exception types, builds `source`/`user_id`/`input_body` best-effort from the function's own arguments, calls `traceback.format_exc()`, fires `send_general_error()`, and **always re-raises** — it is purely a side-channel reporter, never swallows errors.

### Concrete gaps vs. a dedicated error-tracking product

1. **No grouping/deduplication.** Every exception is one HTTP POST. A hot-loop bug that fires 500 times produces 500 webhook calls with no fingerprinting; whatever dedup exists is entirely up to the unknown receiving system.
2. **No correlation with traces.** The payload carries no `trace_id`/`span_id`, so a WhatsApp-reported error and its corresponding CHATR-110 SigNoz trace cannot be cross-referenced today. This is the single cheapest, highest-leverage gap to close (see §5).
3. **Fragile fire-and-forget for sync call sites.** `interceptor()`'s sync wrapper (`error_interceptor.py:367-385`) does `loop.create_task(_handle_error(...))` without awaiting or tracking the task — if the loop shuts down before the task runs (plausible in short-lived sync contexts), the report silently vanishes with no error surfaced. This is an existing reliability bug, independent of the Sentry decision.
4. **No retries, no local buffering.** Single POST, 10s timeout, no retry. If `ERROR_INTERCEPTOR_URL` is down, the error report is lost — only a local `logger.warning` remains.
5. **No PII scrubbing.** `customer_whatsapp_number` (a phone number) and raw `input_body` (which can contain user-provided strings, potentially CPF/address data depending on the tool) go out verbatim to an external endpoint, gated only by an API key header. No `beforeSend`-style filtering exists.
6. **No release/deploy correlation, no breadcrumbs, no performance data.** It is errors-only, single-event, with no notion of "this error started after deploy X" or "here's what happened in the 30 seconds before this crash."
7. **No alerting or visualization owned by this repo.** Whether anyone gets notified, and how errors are triaged, depends entirely on the external receiver — not something this codebase controls or can improve directly.

---

## 2. What Sentry would add (current SDK, verified against live docs — not training-data assumptions)

Checked via Context7 (`/getsentry/sentry-python`, `/getsentry/sentry-docs`) and `docs.sentry.io` (fetched 2026-07-24; Sentry's pricing/quota model changed as recently as August 2025, so this was worth verifying fresh).

### Core product capabilities
- **Issue grouping/deduplication** — fingerprint-based grouping of occurrences into "Issues," with counts, first/last seen, assignment, and regression detection.
- **Breadcrumbs** — an automatic trail of preceding events (log calls, HTTP requests, DB queries) attached to each error event.
- **Release tracking** — first-class "Releases" concept; can flag "new in this release," tie errors to commits/deploys, and track regression across versions. This is Sentry's most polished capability that has **no out-of-the-box equivalent** in the current SigNoz setup (SigNoz can filter/group by a `service.version` resource attribute manually, but there's no dedicated Releases UI or regression detector).
- **Per-frame stack trace context** — for Python, this means captured local variables at each frame, not just a flat formatted traceback string (which is all `error_interceptor` and raw OTel `record_exception` capture today). This is a genuine, if secondary, capability gap.
- **Seer** (AI-assisted issue triage/auto-fix suggestions) — a newer paid add-on (see pricing below), not evaluated in depth here since it's orthogonal to the core error-tracking decision.
- **Performance monitoring / APM** — full distributed tracing product (spans, latency percentiles, throughput), i.e. the same product category as SigNoz.
- **Alerting** — mature per-project alert rules (new issue, regression, frequency threshold) to Slack/email/PagerDuty/etc.

### Integration paths for a codebase that already has OpenTelemetry (this one does, via CHATR-110)

Sentry's Python SDK docs are explicit that if OTel is already in place, you should **not** run Sentry's native tracing in parallel. Three paths exist, in order of current recommendation:

1. **`OTLPIntegration`** (current, recommended — `pip install "sentry-sdk[opentelemetry-otlp]"`). Attaches an additional `SpanExporter` to the **same existing `TracerProvider`** that `src/observability/tracing.py` already configures — it does not replace or wrap it, it adds a second span destination. Also supports `capture_exceptions=True`, which makes Sentry automatically create Issues from OTel's `Span.record_exception()` calls — meaning **`tracing.py`'s existing `span.record_exception(e)` call (line 172) would need zero modification** to start feeding Sentry Issues; only an SDK-init call would be added at process startup. This is the lowest-friction technical path if this were adopted, but it means every span already sent to SigNoz would *also* be sent to Sentry (double egress, see §3).
2. **Legacy `SentrySpanProcessor`/`SentryPropagator` bridge** — older API, explicitly marked deprecated in favor of (1). Same double-export characteristic.
3. **Sentry's own native instrumentation** (`traces_sample_rate`, auto-instrumented libraries) — would run as a fully separate, third tracing pipeline alongside the existing OTel→SigNoz one and any of (1)/(2). Not sensible here; mentioned only for completeness.

In short: there is no way to "just get Sentry's error grouping" without either (a) also duplicating trace/span export into Sentry, or (b) using Sentry purely for `capture_exception()`-style manual error reporting decoupled from tracing (functionally closer to what `error_interceptor` already does, just SaaS-hosted with better grouping).

### Pricing (verified against `docs.sentry.io/pricing`, current as of this writing; Sentry changed its quota model on 2025-08-27, so older figures — e.g. commonly-cited "10M spans included" — are stale)

| Plan | Price (annual / monthly) | Included per month | Notes |
|---|---|---|---|
| Developer | $0 | 5K errors, 1 user | No team features |
| Team | $26/mo / $29/mo | 50K errors, **5M spans** (reduced from 10M in Aug 2025), 50 replays, 5GB logs, 5GB app metrics, 1 cron + 1 uptime monitor | 30-day data retention |
| Business | $80/mo / $89/mo | Same base quota as Team | SSO/SAML/SCIM, unlimited dashboards, anomaly detection, higher overage rates |
| Enterprise | Custom | Custom | — |

Overage (pay-as-you-go), Team plan: **errors** ~$0.00029–0.00036/event depending on volume tier; **spans** ~$0.0000016–0.000004/span. Seer (AI triage add-on) is billed separately, roughly $20+/mo plus per-use credits. None of this is prohibitive at this app's current volume, but it is a new, uncapped-by-default recurring SaaS line item scaling with traffic — see §5 for the concrete number for this service.

---

## 3. Overlap with the OTel/SigNoz setup from CHATR-110

This is not a hypothetical comparison — the CHATR-110 pipeline is live in production today:

- `src/observability/tracing.py` configures a global `TracerProvider` exporting via OTLP/HTTP to `http://signoz-otel-collector.signoz.svc.cluster.local:4318` (`k8s/prod/resources.yaml:43`, `k8s/staging/resources.yaml`), and `ToolCallTracingMiddleware.on_call_tool()` wraps **every** FastMCP tool call in a `mcp.tool_call` span with `mcp.tool.name` / `mcp.tool.user_id` / `mcp.tool.success` attributes.
- On exception, it already calls `span.record_exception(e)` + `span.set_status(Status(StatusCode.ERROR, str(e)))` (`tracing.py:171-173`) — this is the **exact same OTel API** Sentry's own docs point to for feeding its Issues product.
- Verified live via the SigNoz MCP tools connected to this environment: `app-mcp-server` already exists as a tracked service in the `signoz-superapp` SigNoz instance, with real traffic (2,870 calls in the trailing 7 days at time of writing; p99 ≈ 11.6s; top-level operations include `POST /mcp`, `GET /health`, `BigQuery.insertRowsJson`). The pipe is not aspirational — it is shipping data today.
- SigNoz's own documentation (`signoz.io/docs/userguide/exceptions/`, fetched live) confirms a dedicated **Exceptions** feature: a list view sortable by Last Seen / First Seen / Count / Exception Type / Application, grouped by default on service + exception type + message, with a detail page showing the stack trace and a direct link back to the originating trace/span — and SigNoz also documents **exception-based alerting** as a first-class feature.

Mapping this to Sentry's headline capabilities:

| Sentry capability | SigNoz/OTel today (CHATR-110) | Overlap |
|---|---|---|
| Issue grouping/dedup | Exceptions tab, grouped by service+type+message | **Redundant** |
| Stack trace on error | Captured via `record_exception`, shown on exception detail page | **Redundant** (flat trace only, no per-frame locals — see gap above) |
| Trace/span correlation | Native — same product, same trace_id | **Redundant** (arguably stronger, since it's one system) |
| Performance/APM | Already live: call rate, p99, error rate per service/operation for `app-mcp-server` | **Redundant** |
| Alerting on new/frequent errors | SigNoz supports exception-based alerts; CHATR-112 (concurrent sibling ticket) is standing this up now | **Redundant / actively duplicated effort** if built in both places |
| Release/regression tracking | No first-class equivalent (manual `service.version` attribute only) | **Not covered** — Sentry-only |
| Breadcrumbs UI | Approximated via trace waterfall + log correlation, different UX | **Partially covered**, different presentation |
| Per-frame local variables | Not captured by either OTel `record_exception` or `error_interceptor` | **Not covered** — Sentry-only |

The mechanical way to wire Sentry into an already-OTel-instrumented service (`OTLPIntegration`, §2) makes the redundancy concrete rather than abstract: it attaches a second exporter to the **same `TracerProvider`** already sending spans to SigNoz. Every tool-call span — and every exception recorded on it — would be shipped to two competing backends simultaneously. That's not "complementary," it's **double-instrumentation**: two systems both claiming to be the place to look at traces/errors for this service, double the span egress (and Sentry bills per span), and an ongoing tax of keeping two sets of dashboards/alerts consistent as the app evolves.

---

## 4. Recommendation

**Do not adopt Sentry for `app-mcp-server`.** Instead, close the specific, real gaps in `error_interceptor` identified in §1, and lean on the SigNoz Exceptions/alerting capability that CHATR-110 already pays for and CHATR-112 is already building alerting on top of.

### Why

- Sentry's three headline capabilities most commonly used to justify adopting it — issue grouping, APM/performance, and alerting — are **already delivered today** by the shipped OTel→SigNoz pipeline, for the same service, with real production data. Adopting Sentry for those would mean paying for and maintaining a second product that duplicates existing coverage.
- The technically "clean" way to connect Sentry to this codebase (`OTLPIntegration`) works specifically by adding a second exporter to the exact `TracerProvider` `tracing.py` already owns — i.e., adopting Sentry here is definitionally double-instrumentation, not a complementary layer.
- Sentry's genuinely unique value (release/regression tracking as a polished feature, per-frame local variable capture, richer breadcrumbs, Seer AI triage) is real but secondary for a backend MCP tool server with no frontend/mobile surface — none of it is a "must have" today, and none of it is blocked by choosing not to adopt Sentry now.
- `error_interceptor`'s actual differentiated value — WhatsApp user identity and business-flow/workflow/step attribution on every reported error — is a **business-domain concern that neither Sentry nor SigNoz provide out of the box**, and is cheap to keep improving in place rather than migrate into a new vendor's data model.
- This is a government system handling WhatsApp numbers and citizen-submitted data; the existing PII exposure gap in `error_interceptor` (§1, gap 5) is a reason to fix that pipe's hygiene, not a reason to add a *third* external party (Sentry) receiving the same class of data.

### What to actually do (follow-up work, not part of this ticket)

Ranked by leverage/cost:

1. **Correlate `error_interceptor` reports with SigNoz traces.** Add `trace_id`/`span_id` (from `opentelemetry.trace.get_current_span()`) to the webhook payload when tracing is active. Turns "a WhatsApp user hit an error" and "here's the full trace" into one click instead of two disconnected systems. Small, isolated change.
2. **Promote business context (user id, flow/workflow/step) onto the OTel span itself**, not just the `error_interceptor` payload — e.g. as span attributes in `ToolCallTracingMiddleware` or at `@interceptor` call sites. Lets SigNoz's Exceptions view and CHATR-112's alerts filter/group by business flow, which is currently only available in the `error_interceptor` side-channel.
3. **Fix the sync-path fire-and-forget bug** in `interceptor()` (`error_interceptor.py:367-385`) — untracked `loop.create_task()` can silently drop reports. Independent bugfix, worth doing regardless of this decision.
4. **Add basic PII redaction** before payloads leave the process (mask/hash phone numbers, avoid echoing raw free-text `input_body` fields that may carry citizen data).
5. **Rely on SigNoz exception-based alerting** (CHATR-112) as the "notify us about new/frequent prod errors" mechanism, rather than standing up a parallel alerting configuration in a new tool.

### When to revisit this decision

Reconsider Sentry (or an equivalent) if any of these become true:
- A frontend/mobile client is added to this product surface needing session replay or JS source-map symbolication — an area where SigNoz is materially weaker and Sentry's core value proposition is strongest.
- After CHATR-112 ships, the team still cannot reliably triage/get alerted on production errors using SigNoz — i.e., the "redundant" capabilities above turn out not to work well in practice, not just in theory.
- Release/regression tracking becomes a concrete, recurring pain point that manual `service.version` filtering in SigNoz can't address.

---

## 5. Cost/effort tradeoff

| | Adopt Sentry | Extend `error_interceptor` (recommended) |
|---|---|---|
| Recurring cost | New SaaS line item: $26–29/mo (Team) minimum, scaling with error/span volume. At current volume (~2.9K calls/7d ≈ ~12K/mo) this stays well inside the free/Team tier today, but span cost scales with the *duplicated* full-fidelity trace export described in §3, not just error count — so the bill grows exactly as the "redundant" overlap grows. | $0 recurring. |
| Engineering effort | SDK install + dependency addition, DSN/secret provisioning through existing secrets flow, `OTLPIntegration` wiring, dashboards/alerts setup in a new tool, cross-checking against SigNoz to avoid dashboard drift, legal/data-processing review for a new third party handling citizen data (WhatsApp numbers). | One small, self-contained PR: trace/span-id correlation + a few span attributes + one bugfix + basic redaction. All changes are localized to `error_interceptor.py` and `tracing.py`, no new infra. |
| Ongoing maintenance | Two observability systems to keep consistent (SigNoz for traces/APM, Sentry for issues/alerts) — real risk of "which tool is the source of truth" confusion. | One system (SigNoz) as source of truth for traces/APM/exceptions/alerts; `error_interceptor` remains the business-context side-channel it already is, just better connected. |
| Net | Pays for overlapping capability, adds vendor/compliance surface, doesn't close the gaps that actually matter (business-context correlation, PII hygiene). | Closes the concrete gaps identified in §1 at near-zero cost, using infrastructure already paid for under CHATR-110. |

---

## Appendix: files referenced

- `src/utils/error_interceptor.py` — full implementation (462 lines)
- `src/config/env.py:43-44` — `ERROR_INTERCEPTOR_URL` / `ERROR_INTERCEPTOR_TOKEN`
- `src/config/env.py:160-167` — `OTEL_SERVICE_NAME` / `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` / `OTEL_EXPORTER_OTLP_TRACES_HEADERS`
- `src/observability/tracing.py` — OTel `TracerProvider` setup + `ToolCallTracingMiddleware` (CHATR-110, PR #139)
- `k8s/prod/resources.yaml`, `k8s/staging/resources.yaml` — runtime wiring (OTel endpoint inline, error-interceptor secrets via `mcp-secrets`)
- External (fetched live, 2026-07-24): `docs.sentry.io/pricing`, `docs.sentry.io/platforms/python/integrations/otlp/`, `signoz.io/docs/userguide/exceptions/`; SigNoz `signoz-superapp` instance service list (confirms `app-mcp-server` is already an actively-traced service)
