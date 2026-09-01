"""Single source of truth for the five plan-todo-8 failure classes, shared by
`release_readiness_check.py` and `check_runbook_links.py` so the two scripts
can never silently disagree about what a "required signal" is.

Every field below is copied from a file this task read directly — see each
runbook's own "Source of truth" section for the exact citation. This module
does not invent thresholds or resource names; it only re-states them as
importable Python data so both checker scripts share one definition.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class RequiredSignal:
    failure_class: str
    runbook_doc: str  # relative to docs/runbooks/
    alert_resource: Optional[str]  # infra/superapp signoz_alert address, or None
    runbook_url: Optional[str]  # the alert's labels.runbook_url slug, or None
    dashboard_widget: Optional[str]  # widget title in mcp_namespace_health, or None
    severity: Optional[str]  # None for failed-canary (no signoz_alert)
    drill_test_file: str  # relative to src/tests/resilience/


REQUIRED_SIGNALS: tuple[RequiredSignal, ...] = (
    RequiredSignal(
        failure_class="mcp-unavailable",
        runbook_doc="mcp-unavailable.md",
        alert_resource="signoz_alert.mcp_workload_unavailable",
        runbook_url="https://runbooks.example.internal/mcp/workload-unavailable",
        dashboard_widget="MCP workload: desired vs available",
        severity="critical",
        drill_test_file="test_drill_mcp_unavailable.py",
    ),
    RequiredSignal(
        failure_class="unhealthy-workload",
        runbook_doc="unhealthy-workload.md",
        alert_resource="signoz_alert.mcp_pod_lifecycle_unhealthy",
        runbook_url="https://runbooks.example.internal/mcp/pod-lifecycle",
        dashboard_widget=None,  # known gap — see unhealthy-workload.md Source of truth
        severity="warning",
        drill_test_file="test_drill_unhealthy_workload.py",
    ),
    RequiredSignal(
        failure_class="redis-sentinel-failover",
        runbook_doc="redis-sentinel-failover.md",
        alert_resource="signoz_alert.mcp_redis_workload_unavailable",
        runbook_url="https://runbooks.example.internal/mcp/redis-unavailable",
        dashboard_widget=None,  # no dedicated Sentinel-quorum widget exists yet
        severity="critical",
        drill_test_file="test_drill_redis_sentinel_failover.py",
    ),
    RequiredSignal(
        failure_class="stale-telemetry",
        runbook_doc="stale-telemetry.md",
        alert_resource="signoz_alert.mcp_telemetry_freshness",
        runbook_url="https://runbooks.example.internal/mcp/telemetry-freshness",
        dashboard_widget="app-mcp-server telemetry freshness (trace count)",
        severity="warning",
        drill_test_file="test_drill_stale_telemetry.py",
    ),
    RequiredSignal(
        failure_class="failed-canary",
        runbook_doc="failed-canary.md",
        alert_resource=None,  # no signoz_alert models this — see runbook
        runbook_url=None,
        dashboard_widget=None,
        severity=None,
        drill_test_file="test_drill_failed_canary.py",
    ),
)

# infra/superapp/modules/deployments/signoz-resilience-dashboard.tf:14
REQUIRED_DASHBOARD_RESOURCE = "signoz_dashboard.mcp_namespace_health"

# infra/superapp/modules/deployments/signoz-resilience-route-policy.tf:13
REQUIRED_ROUTE_POLICY_RESOURCE = "signoz_route_policy.mcp_page_level_alerts"

# Runbook "Detection" sections require every alert this route policy is
# expected to cover — i.e. every REQUIRED_SIGNALS entry with severity
# "critical" — to actually route through it.
CRITICAL_FAILURE_CLASSES = tuple(
    s.failure_class for s in REQUIRED_SIGNALS if s.severity == "critical"
)

PLACEHOLDER_RUNBOOK_HOST = "runbooks.example.internal"


def required_signal_for_slug(slug: str) -> Optional[RequiredSignal]:
    """Looks up a `RequiredSignal` by its `runbook_url` path suffix (the part
    after `/mcp/`), e.g. `"workload-unavailable"`."""
    target = f"https://{PLACEHOLDER_RUNBOOK_HOST}/mcp/{slug}"
    for signal in REQUIRED_SIGNALS:
        if signal.runbook_url == target:
            return signal
    return None
