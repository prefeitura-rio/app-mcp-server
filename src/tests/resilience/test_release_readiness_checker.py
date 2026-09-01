"""Tests for `scripts/resilience/release_readiness_check.py` (plan todo 8).

`scripts/` is deliberately not a Python package importable via the normal
`from scripts...` path (it is excluded from the runtime image via
`.dockerignore` and is not part of `app-mcp-server`'s installable package —
see `pyproject.toml`'s `[tool.setuptools.packages.find]`). Tests load it the
same isolated-file-path way `src/tests/unit/tools/test_redis_backend_failover.py`
already loads `state.py`.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

_SCRIPT_PATH = (
    Path(__file__).resolve().parents[3]
    / "scripts"
    / "resilience"
    / "release_readiness_check.py"
)


def _load_checker_module():
    """Registers into `sys.modules` *before* executing, the same way
    `src/tests/unit/tools/test_redis_backend_failover.py` loads `state.py` —
    required here too: `Finding` is a `@dataclass` with a string type
    annotation, and `dataclasses` resolves those via
    `sys.modules[cls.__module__]`, which fails with a confusing
    `AttributeError` if the module was never registered."""
    spec = importlib.util.spec_from_file_location(
        "release_readiness_check_module", _SCRIPT_PATH
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def checker():
    return _load_checker_module()


def _write_junit(path: Path, passing_files: list[str], failing_files: list[str] = ()):
    """Builds a minimal pytest-shaped JUnit XML: one passing `<testcase>`
    per file in `passing_files`, one failing (with a `<failure>` child) per
    file in `failing_files`. `classname` uses dotted-module form, matching
    real pytest JUnit output for `src/tests/resilience/test_x.py`."""
    root = ET.Element("testsuites")
    suite = ET.SubElement(root, "testsuite", name="pytest")
    for f in passing_files:
        classname = f"src.tests.resilience.{Path(f).stem}"
        ET.SubElement(suite, "testcase", classname=classname, name="test_ok")
    for f in failing_files:
        classname = f"src.tests.resilience.{Path(f).stem}"
        tc = ET.SubElement(suite, "testcase", classname=classname, name="test_bad")
        ET.SubElement(tc, "failure", message="boom")
    ET.ElementTree(root).write(path)


# ---------------------------------------------------------------------------
# Manifest loading — adversarial classes: malformed fixture input
# ---------------------------------------------------------------------------


def test_load_manifest_rejects_invalid_json(checker, tmp_path):
    bad = tmp_path / "manifest.json"
    bad.write_text("{ not valid json", encoding="utf-8")

    with pytest.raises(checker.MalformedManifestError, match="not valid JSON"):
        checker.load_manifest(bad)


def test_load_manifest_rejects_missing_field(checker, tmp_path):
    bad = tmp_path / "manifest.json"
    bad.write_text(json.dumps({"deployed_alerts": []}), encoding="utf-8")

    with pytest.raises(
        checker.MalformedManifestError, match="deployed_dashboard_widgets"
    ):
        checker.load_manifest(bad)


def test_load_manifest_rejects_wrong_type(checker, tmp_path):
    bad = tmp_path / "manifest.json"
    bad.write_text(
        json.dumps(
            {
                "deployed_alerts": "not-a-list",
                "deployed_dashboard_widgets": [],
                "deployed_dashboard_resource": None,
                "deployed_route_policy_resource": None,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(checker.MalformedManifestError, match="deployed_alerts"):
        checker.load_manifest(bad)


def test_load_manifest_rejects_missing_file(checker, tmp_path):
    with pytest.raises(checker.MalformedManifestError, match="not found"):
        checker.load_manifest(tmp_path / "does-not-exist.json")


def test_real_committed_manifest_loads_cleanly(checker):
    """The actual `scripts/resilience/signals_manifest.json` shipped in this
    repository must itself be valid against the schema `load_manifest`
    enforces — this is the "happy fixture" every other test in this module
    treats as the known-good baseline."""
    real_manifest_path = _SCRIPT_PATH.parent / "signals_manifest.json"

    manifest = checker.load_manifest(real_manifest_path)

    assert "signoz_alert.mcp_workload_unavailable" in manifest["deployed_alerts"]


# ---------------------------------------------------------------------------
# Alert / dashboard / route-policy checks
# ---------------------------------------------------------------------------


@pytest.fixture
def complete_manifest(checker):
    real_manifest_path = _SCRIPT_PATH.parent / "signals_manifest.json"
    return checker.load_manifest(real_manifest_path)


def test_complete_manifest_has_no_alert_findings(checker, complete_manifest):
    assert checker.check_alerts(complete_manifest) == []


def test_complete_manifest_has_no_dashboard_findings(checker, complete_manifest):
    assert checker.check_dashboard(complete_manifest) == []


def test_complete_manifest_has_no_route_policy_findings(checker, complete_manifest):
    assert checker.check_route_policy(complete_manifest) == []


def test_missing_alert_produces_exact_message(checker, complete_manifest):
    """The manual QA scenario this task's DoneClaim captures: removing one
    required signal must name it exactly, not just say "something is
    missing"."""
    complete_manifest["deployed_alerts"] = [
        a
        for a in complete_manifest["deployed_alerts"]
        if a != "signoz_alert.mcp_telemetry_freshness"
    ]

    findings = checker.check_alerts(complete_manifest)

    assert len(findings) == 1
    assert findings[0].kind == "MISSING"
    assert "signoz_alert.mcp_telemetry_freshness" in findings[0].message
    assert "stale-telemetry" in findings[0].message


def test_missing_dashboard_widget_produces_exact_message(checker, complete_manifest):
    complete_manifest["deployed_dashboard_widgets"] = [
        w
        for w in complete_manifest["deployed_dashboard_widgets"]
        if w != "MCP workload: desired vs available"
    ]

    findings = checker.check_dashboard(complete_manifest)

    assert len(findings) == 1
    assert "MCP workload: desired vs available" in findings[0].message
    assert "mcp-unavailable" in findings[0].message


def test_missing_route_policy_produces_exact_message(checker, complete_manifest):
    complete_manifest["deployed_route_policy_resource"] = None

    findings = checker.check_route_policy(complete_manifest)

    assert len(findings) == 1
    assert "signoz_route_policy.mcp_page_level_alerts" in findings[0].message


# ---------------------------------------------------------------------------
# Drill / JUnit evidence checks
# ---------------------------------------------------------------------------


def _all_drill_files(checker):
    return [s.drill_test_file for s in checker.REQUIRED_SIGNALS]


def test_no_junit_supplied_reports_missing(checker):
    findings = checker.check_drills(None)

    assert len(findings) == 1
    assert "no --junit-xml supplied" in findings[0].message


def test_all_drills_passing_produces_no_findings(checker, tmp_path):
    junit_path = tmp_path / "junit.xml"
    _write_junit(junit_path, passing_files=_all_drill_files(checker))

    findings = checker.check_drills(junit_path, drills_dir=tmp_path)

    assert findings == []


def test_one_missing_drill_result_is_reported_by_name(checker, tmp_path):
    all_files = _all_drill_files(checker)
    junit_path = tmp_path / "junit.xml"
    _write_junit(junit_path, passing_files=all_files[1:])  # omit the first

    findings = checker.check_drills(junit_path, drills_dir=tmp_path)

    assert len(findings) == 1
    assert findings[0].kind == "FAILING DRILL"
    assert all_files[0] in findings[0].message


def test_a_failing_drill_testcase_is_not_counted_as_passing(checker, tmp_path):
    all_files = _all_drill_files(checker)
    junit_path = tmp_path / "junit.xml"
    _write_junit(junit_path, passing_files=all_files[1:], failing_files=[all_files[0]])

    findings = checker.check_drills(junit_path, drills_dir=tmp_path)

    assert len(findings) == 1
    assert all_files[0] in findings[0].message


def test_malformed_junit_xml_is_reported_clearly(checker, tmp_path):
    junit_path = tmp_path / "junit.xml"
    junit_path.write_text("<not><valid", encoding="utf-8")

    findings = checker.check_drills(junit_path, drills_dir=tmp_path)

    assert len(findings) == 1
    assert "not valid XML" in findings[0].message


def test_junit_with_no_testcases_is_reported_clearly(checker, tmp_path):
    junit_path = tmp_path / "junit.xml"
    ET.ElementTree(ET.Element("testsuites")).write(junit_path)

    findings = checker.check_drills(junit_path, drills_dir=tmp_path)

    assert len(findings) == 1
    assert "no <testcase>" in findings[0].message


def test_stale_evidence_detected_when_drill_file_newer_than_junit(checker, tmp_path):
    """Adversarial class: stale generated evidence. A JUnit report that
    predates a change to the drill file it claims to cover must not be
    silently accepted as current."""
    import os
    import time

    all_files = _all_drill_files(checker)
    junit_path = tmp_path / "junit.xml"
    _write_junit(junit_path, passing_files=all_files)

    # Make the "drill file" strictly newer than the junit report.
    drill_file = tmp_path / all_files[0]
    drill_file.write_text("# changed after the junit report was generated\n")
    newer = os.path.getmtime(junit_path) + 10
    os.utime(drill_file, (newer, newer))
    time.sleep(0)  # no real wait — just documents intent, mtimes already set

    findings = checker.check_drills(junit_path, drills_dir=tmp_path)

    assert len(findings) == 1
    assert findings[0].kind == "STALE EVIDENCE"
    assert all_files[0] in findings[0].message


# ---------------------------------------------------------------------------
# Runbook doc section checks
# ---------------------------------------------------------------------------


def test_real_runbooks_satisfy_required_sections(checker):
    """The committed `docs/runbooks/*.md` files must pass this checker's
    own section-completeness check — this is what the manual QA "happy
    path" run of the checker actually exercises against real docs."""
    findings = checker.check_runbook_docs()

    assert findings == []


def test_runbook_missing_a_required_section_is_reported(checker, tmp_path):
    incomplete_doc = tmp_path / "mcp-unavailable.md"
    incomplete_doc.write_text(
        "# Runbook\n\n## Detection\n\nsomething\n", encoding="utf-8"
    )
    # Write placeholder files for the other four so only the first signal's
    # doc is exercised meaningfully; the rest exist but are also incomplete
    # (acceptable here — we only assert on the one we care about).
    for name in (
        "unhealthy-workload.md",
        "redis-sentinel-failover.md",
        "stale-telemetry.md",
        "failed-canary.md",
    ):
        (tmp_path / name).write_text("# stub\n", encoding="utf-8")

    findings = checker.check_runbook_docs(runbooks_dir=tmp_path)

    messages = [f.message for f in findings]
    assert any(
        "mcp-unavailable.md is missing required section marker" in m for m in messages
    )


def test_runbook_file_entirely_absent_is_reported(checker, tmp_path):
    findings = checker.check_runbook_docs(runbooks_dir=tmp_path)

    assert any("does not exist" in f.message for f in findings)


# ---------------------------------------------------------------------------
# End-to-end `run()` — the exact CLI path used by manual QA
# ---------------------------------------------------------------------------


def test_run_end_to_end_pass_and_fail(checker, tmp_path):
    real_manifest_path = _SCRIPT_PATH.parent / "signals_manifest.json"
    manifest_data = checker.load_manifest(real_manifest_path)

    junit_path = tmp_path / "junit.xml"
    _write_junit(junit_path, passing_files=_all_drill_files(checker))

    complete_manifest_path = tmp_path / "complete.json"
    complete_manifest_path.write_text(json.dumps(manifest_data), encoding="utf-8")

    assert checker.run(complete_manifest_path, junit_path) == 0

    manifest_data["deployed_alerts"] = [
        a
        for a in manifest_data["deployed_alerts"]
        if a != "signoz_alert.mcp_telemetry_freshness"
    ]
    broken_manifest_path = tmp_path / "broken.json"
    broken_manifest_path.write_text(json.dumps(manifest_data), encoding="utf-8")

    assert checker.run(broken_manifest_path, junit_path) == 1


def test_run_returns_2_for_malformed_manifest(checker, tmp_path):
    bad = tmp_path / "manifest.json"
    bad.write_text("not json at all", encoding="utf-8")

    assert checker.run(bad, None) == 2
