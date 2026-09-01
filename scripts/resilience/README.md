# MCP resilience release-readiness tooling

Ops/dev-only scripts for plan todo 8 — excluded from the production image
via `.dockerignore` (same treatment as `debug_mcp_server.py`). See
`docs/runbooks/README.md` for the runbooks these validate.

## `check_runbook_links.py`

Static documentation/link checker for `docs/runbooks/`: confirms every
relative markdown link resolves and every `runbooks.example.internal`
placeholder slug matches a required signal.

```bash
uv run python3 scripts/resilience/check_runbook_links.py
```

Exit code `0` on pass, `1` on any broken link/slug, `2` if the runbooks
directory itself is missing/empty.

## `release_readiness_check.py`

Release-readiness checker: cross-checks a deployed-signals manifest and a
pytest JUnit report from the isolated drill harness against the five
required failure-class signals (`resilience_signals.py`).

```bash
# 1. Run the isolated drills and capture JUnit evidence.
uv run pytest src/tests/resilience --junitxml=/tmp/resilience-junit.xml

# 2. Run the checker against the committed manifest + that evidence.
uv run python3 scripts/resilience/release_readiness_check.py \
  --junit-xml /tmp/resilience-junit.xml
```

Exit code `0` on pass, `1` on any missing prerequisite (each printed as an
exact `MISSING:` / `STALE EVIDENCE:` / `FAILING DRILL:` line), `2` if the
manifest itself is malformed. `--manifest` defaults to
`scripts/resilience/signals_manifest.json`; pass a different path to check a
mutated/simulated manifest (e.g. with one alert removed) without touching
the committed one.

**This is a simulated, offline check.** It never queries a live SigNoz,
Kubernetes, or Terraform-state endpoint, and it never sends a real
notification — see `signals_manifest.json`'s own `_comment` field and
`docs/runbooks/README.md#common-to-every-runbook-below`.
