#!/usr/bin/env python3
"""Release-readiness checker for MCP production resilience (plan todo 8).

Validates a production change review against four categories of
prerequisite, one per required signal in `resilience_signals.REQUIRED_SIGNALS`
(the five plan failure classes):

1. Required SigNoz alert resources are present in the deployed-signals
   manifest.
2. Required dashboard widgets (and the dashboard resource itself) are
   present.
3. The route policy that pages on critical-severity alerts is present.
4. Each failure class's isolated drill (`src/tests/resilience/`) has a
   PASSING result in the supplied pytest JUnit XML, and that evidence is not
   older than the drill test files it claims to cover (stale-evidence
   check).

Every failure is printed as a single, greppable `MISSING: ...` or
`STALE EVIDENCE: ...` or `FAILING DRILL: ...` line naming the exact
prerequisite — see `docs/runbooks/README.md#isolated-drill-harness-and-release-readiness-checker`
and this task's manual QA transcript
(`.omo/evidence/task-8-superapp-mcp-resilience-monitoring/`) for a captured
pass/fail pair.

This is a SIMULATED, offline check against a manually-maintained signals
manifest — see `signals_manifest.json`'s own `_comment` field. It never
queries a live SigNoz/Kubernetes/Terraform-state endpoint, sends a real
notification, or constitutes a verified production page test. Exit code 0
means every checked prerequisite is present in the supplied manifest and
JUnit results; it does not mean production has been paged and acknowledged.
"""

from __future__ import annotations

import argparse
import json
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
from resilience_signals import (  # noqa: E402
    CRITICAL_FAILURE_CLASSES,
    REQUIRED_DASHBOARD_RESOURCE,
    REQUIRED_ROUTE_POLICY_RESOURCE,
    REQUIRED_SIGNALS,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
RUNBOOKS_DIR = REPO_ROOT / "docs" / "runbooks"
DRILLS_DIR = REPO_ROOT / "src" / "tests" / "resilience"

REQUIRED_RUNBOOK_SECTION_MARKERS: tuple[str, ...] = (
    "## Detection",
    "## Diagnosis",
    "## Remediation",  # matches both "## Remediation" and "## Remediation / Rollback"
    "Rollback",
    "## Recovery objectives",
    "## Escalation",
    "## Safety boundaries",
)


class MalformedManifestError(ValueError):
    """Raised when the signals manifest is not valid JSON or is missing/
    mistyped a required field — always names the exact field or parse
    error, never a bare `KeyError`/`TypeError`."""


class MalformedJunitError(ValueError):
    """Raised when the supplied JUnit XML cannot be parsed as pytest's
    output shape."""


@dataclass
class Finding:
    kind: str  # "MISSING" | "STALE EVIDENCE" | "FAILING DRILL"
    message: str

    def __str__(self) -> str:
        return f"{self.kind}: {self.message}"


# ---------------------------------------------------------------------------
# Manifest loading/validation
# ---------------------------------------------------------------------------


def load_manifest(path: Path) -> dict:
    if not path.exists():
        raise MalformedManifestError(f"manifest file not found: {path}")
    raw_text = path.read_text(encoding="utf-8")
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise MalformedManifestError(
            f"manifest {path} is not valid JSON: {exc}"
        ) from exc

    if not isinstance(data, dict):
        raise MalformedManifestError(
            f"manifest {path} must be a JSON object, got {type(data).__name__}"
        )

    required_fields = {
        "deployed_alerts": list,
        "deployed_dashboard_widgets": list,
        "deployed_dashboard_resource": (str, type(None)),
        "deployed_route_policy_resource": (str, type(None)),
    }
    for field, expected_type in required_fields.items():
        if field not in data:
            raise MalformedManifestError(
                f"manifest {path} is missing required field {field!r}"
            )
        if not isinstance(data[field], expected_type):
            raise MalformedManifestError(
                f"manifest {path} field {field!r} must be of type "
                f"{expected_type!r}, got {type(data[field]).__name__}"
            )
    for i, item in enumerate(data["deployed_alerts"]):
        if not isinstance(item, str):
            raise MalformedManifestError(
                f"manifest {path} field 'deployed_alerts[{i}]' must be a "
                f"string, got {item!r}"
            )
    for i, item in enumerate(data["deployed_dashboard_widgets"]):
        if not isinstance(item, str):
            raise MalformedManifestError(
                f"manifest {path} field 'deployed_dashboard_widgets[{i}]' "
                f"must be a string, got {item!r}"
            )
    return data


# ---------------------------------------------------------------------------
# JUnit XML parsing
# ---------------------------------------------------------------------------


def parse_junit_passed_files(path: Path) -> set[str]:
    """Returns the set of source file basenames (e.g.
    `"test_drill_mcp_unavailable.py"`) that had at least one PASSING
    testcase in the JUnit report at `path`.

    A testcase counts as passing only if it has neither a `<failure>` nor an
    `<error>` child element — pytest's JUnit exporter nests those, it never
    uses a `status` attribute.
    """
    if not path.exists():
        raise MalformedJunitError(f"JUnit XML file not found: {path}")
    try:
        tree = ET.parse(path)
    except ET.ParseError as exc:
        raise MalformedJunitError(f"{path} is not valid XML: {exc}") from exc

    root = tree.getroot()
    testcases = root.findall(".//testcase")
    if not testcases:
        raise MalformedJunitError(
            f"{path} parses as XML but contains no <testcase> elements — "
            "not a recognizable pytest JUnit report"
        )

    passed_files: set[str] = set()
    for tc in testcases:
        classname = tc.get("classname", "")
        # pytest's JUnit classname for src/tests/resilience/test_x.py is
        # "src.tests.resilience.test_x" (dots, no .py) — reconstruct the
        # basename rather than assuming a specific separator convention.
        file_stem = classname.rsplit(".", 1)[-1] if classname else ""
        failed = tc.find("failure") is not None or tc.find("error") is not None
        skipped = tc.find("skipped") is not None
        if file_stem and not failed and not skipped:
            passed_files.add(f"{file_stem}.py")
    return passed_files


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------


def check_alerts(manifest: dict) -> list[Finding]:
    findings = []
    deployed = set(manifest["deployed_alerts"])
    for signal in REQUIRED_SIGNALS:
        if signal.alert_resource is None:
            continue  # e.g. failed-canary — see resilience_signals.py
        if signal.alert_resource not in deployed:
            findings.append(
                Finding(
                    "MISSING",
                    f"SigNoz alert '{signal.alert_resource}' required for "
                    f"failure class '{signal.failure_class}' "
                    f"(docs/runbooks/{signal.runbook_doc}) not found in "
                    "deployed signals manifest",
                )
            )
    return findings


def check_dashboard(manifest: dict) -> list[Finding]:
    findings = []
    if manifest["deployed_dashboard_resource"] != REQUIRED_DASHBOARD_RESOURCE:
        findings.append(
            Finding(
                "MISSING",
                f"dashboard resource '{REQUIRED_DASHBOARD_RESOURCE}' not "
                "found in deployed signals manifest "
                f"(got {manifest['deployed_dashboard_resource']!r})",
            )
        )
    deployed_widgets = set(manifest["deployed_dashboard_widgets"])
    for signal in REQUIRED_SIGNALS:
        if signal.dashboard_widget is None:
            continue  # documented gap — see the signal's runbook
        if signal.dashboard_widget not in deployed_widgets:
            findings.append(
                Finding(
                    "MISSING",
                    f"dashboard widget '{signal.dashboard_widget}' required "
                    f"for failure class '{signal.failure_class}' "
                    f"(docs/runbooks/{signal.runbook_doc}) not found in "
                    "deployed signals manifest",
                )
            )
    return findings


def check_route_policy(manifest: dict) -> list[Finding]:
    if manifest["deployed_route_policy_resource"] == REQUIRED_ROUTE_POLICY_RESOURCE:
        return []
    return [
        Finding(
            "MISSING",
            f"route policy '{REQUIRED_ROUTE_POLICY_RESOURCE}' required to "
            f"page on critical-severity alerts for failure classes "
            f"{list(CRITICAL_FAILURE_CLASSES)} not found in deployed "
            f"signals manifest (got "
            f"{manifest['deployed_route_policy_resource']!r})",
        )
    ]


def check_drills(
    junit_path: Optional[Path], drills_dir: Path = DRILLS_DIR
) -> list[Finding]:
    if junit_path is None:
        return [
            Finding(
                "MISSING",
                "no --junit-xml supplied — cannot confirm any isolated "
                "drill actually passed; run "
                "`uv run pytest src/tests/resilience --junitxml=<path>` "
                "first and pass its output here",
            )
        ]

    findings: list[Finding] = []
    try:
        passed_files = parse_junit_passed_files(junit_path)
    except MalformedJunitError as exc:
        return [Finding("MISSING", f"drill evidence unusable: {exc}")]

    junit_mtime = junit_path.stat().st_mtime
    for signal in REQUIRED_SIGNALS:
        drill_path = drills_dir / signal.drill_test_file
        if signal.drill_test_file not in passed_files:
            findings.append(
                Finding(
                    "FAILING DRILL",
                    f"no passing test found in {junit_path} for failure "
                    f"class '{signal.failure_class}' (expected at least "
                    f"one passing testcase from {signal.drill_test_file})",
                )
            )
            continue
        if drill_path.exists() and drill_path.stat().st_mtime > junit_mtime:
            findings.append(
                Finding(
                    "STALE EVIDENCE",
                    f"{junit_path} is older than {drill_path} — the drill "
                    f"file for failure class '{signal.failure_class}' "
                    "changed since these results were generated; re-run "
                    "`uv run pytest src/tests/resilience --junitxml=...`",
                )
            )
    return findings


def check_runbook_docs(runbooks_dir: Path = RUNBOOKS_DIR) -> list[Finding]:
    findings = []
    for signal in REQUIRED_SIGNALS:
        doc_path = runbooks_dir / signal.runbook_doc
        if not doc_path.exists():
            findings.append(
                Finding(
                    "MISSING",
                    f"runbook doc docs/runbooks/{signal.runbook_doc} for "
                    f"failure class '{signal.failure_class}' does not exist",
                )
            )
            continue
        content = doc_path.read_text(encoding="utf-8")
        for marker in REQUIRED_RUNBOOK_SECTION_MARKERS:
            if marker not in content:
                findings.append(
                    Finding(
                        "MISSING",
                        f"docs/runbooks/{signal.runbook_doc} is missing "
                        f"required section marker {marker!r}",
                    )
                )
    return findings


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def run(manifest_path: Path, junit_path: Optional[Path]) -> int:
    try:
        manifest = load_manifest(manifest_path)
    except MalformedManifestError as exc:
        print(f"MALFORMED MANIFEST: {exc}", file=sys.stderr)
        return 2

    findings: list[Finding] = []
    findings += check_alerts(manifest)
    findings += check_dashboard(manifest)
    findings += check_route_policy(manifest)
    findings += check_drills(junit_path)
    findings += check_runbook_docs()

    print(f"Release readiness check against manifest: {manifest_path}")
    print(
        f"  {len(REQUIRED_SIGNALS)} required failure-class signals checked "
        f"({', '.join(s.failure_class for s in REQUIRED_SIGNALS)})"
    )
    print(f"  junit evidence: {junit_path if junit_path else '(none supplied)'}")
    print()

    if not findings:
        print(
            "PASS — all checked prerequisites present in the supplied manifest/evidence."
        )
        print(
            "SIMULATED result, not a live check: no SigNoz/Kubernetes/Terraform "
            "endpoint was queried and no real notification was sent. See "
            "docs/runbooks/README.md for what this does and does not verify."
        )
        return 0

    print(f"FAIL — {len(findings)} missing prerequisite(s):")
    for f in findings:
        print(f"  {f}")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path(__file__).resolve().parent / "signals_manifest.json",
        help="Path to the deployed-signals manifest JSON.",
    )
    parser.add_argument(
        "--junit-xml",
        type=Path,
        default=None,
        help=(
            "Path to a pytest JUnit XML report from "
            "`uv run pytest src/tests/resilience --junitxml=<path>`. "
            "Without this, drill prerequisites are reported as missing."
        ),
    )
    args = parser.parse_args()
    return run(args.manifest, args.junit_xml)


if __name__ == "__main__":
    raise SystemExit(main())
