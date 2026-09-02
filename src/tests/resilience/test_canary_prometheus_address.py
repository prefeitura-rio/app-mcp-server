"""Regression test for the prod canary Prometheus hostname (plan todo 8
follow-up): release v2026.09.01-1's `AnalysisTemplate/mcp-success-rate`
pointed at `prometheus.istio-system.svc.cluster.local`, a Service that does
not exist in the cluster — the real Service, created by
`helm_release.prometheus` in `infra/superapp` `modules/deployments/istio.tf`,
is `prometheus-server` (Helm's `fullname` template collapses
`<release>-<server.name>` when release and chart name coincide, as they do
here: both are `prometheus`). Both canary AnalysisRuns aborted with an
unresolvable-address error and the Rollout auto-rolled back.

This test parses the real `k8s/prod/resources.yaml` manifest (not a copy)
so a future edit reintroducing the wrong hostname fails CI instead of only
being caught during a live canary.
"""

from __future__ import annotations

from pathlib import Path

import yaml

_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_PROD_MANIFEST_PATH = _PROJECT_ROOT / "k8s" / "prod" / "resources.yaml"

EXPECTED_PROMETHEUS_ADDRESS = (
    "http://prometheus-server.istio-system.svc.cluster.local:9090"
)


def _load_analysis_template() -> dict:
    documents = yaml.safe_load_all(_PROD_MANIFEST_PATH.read_text(encoding="utf-8"))
    for document in documents:
        if document and document.get("kind") == "AnalysisTemplate":
            return document
    raise AssertionError(f"no AnalysisTemplate document found in {_PROD_MANIFEST_PATH}")


def test_canary_analysis_template_points_at_the_real_prometheus_service():
    """Both metrics' `provider.prometheus.address` must resolve to the
    Service that actually exists in `istio-system`
    (`prometheus-server`, not `prometheus`)."""
    analysis_template = _load_analysis_template()

    addresses = {
        metric["name"]: metric["provider"]["prometheus"]["address"]
        for metric in analysis_template["spec"]["metrics"]
    }

    assert addresses == {
        "success-rate": EXPECTED_PROMETHEUS_ADDRESS,
        "error-rate": EXPECTED_PROMETHEUS_ADDRESS,
    }
