"""Tests for `scripts/resilience/check_runbook_links.py` (plan todo 8).

Loaded the same isolated-file-path way as
`test_release_readiness_checker.py` loads its script.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

_SCRIPT_PATH = (
    Path(__file__).resolve().parents[3]
    / "scripts"
    / "resilience"
    / "check_runbook_links.py"
)


def _load_link_checker_module():
    spec = importlib.util.spec_from_file_location(
        "check_runbook_links_module", _SCRIPT_PATH
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def link_checker():
    return _load_link_checker_module()


def test_real_runbooks_have_no_broken_links(link_checker, monkeypatch):
    """The committed `docs/runbooks/*.md` files must themselves pass this
    checker — this is exactly what the manual QA happy-path run exercises."""
    exit_code = link_checker.run()

    assert exit_code == 0


def test_missing_relative_link_is_reported(link_checker, monkeypatch, tmp_path):
    monkeypatch.setattr(link_checker, "RUNBOOKS_DIR", tmp_path)
    (tmp_path / "a.md").write_text(
        "See [b](b.md) and [ghost](does-not-exist.md).\n", encoding="utf-8"
    )
    (tmp_path / "b.md").write_text("# B\n", encoding="utf-8")

    broken_relative, broken_slugs, referenced = link_checker.check_file(
        tmp_path / "a.md"
    )

    assert any("does-not-exist.md" in msg for msg in broken_relative)
    assert broken_slugs == []


def test_unknown_slug_is_reported(link_checker, tmp_path):
    doc = tmp_path / "a.md"
    doc.write_text(
        "Runbook: `https://runbooks.example.internal/mcp/totally-made-up-slug`\n",
        encoding="utf-8",
    )

    broken_relative, broken_slugs, referenced = link_checker.check_file(doc)

    assert broken_relative == []
    assert len(broken_slugs) == 1
    assert "totally-made-up-slug" in broken_slugs[0]
    assert referenced == {"totally-made-up-slug"}


def test_known_slug_is_accepted(link_checker, tmp_path):
    doc = tmp_path / "a.md"
    doc.write_text(
        "Runbook: `https://runbooks.example.internal/mcp/workload-unavailable`\n",
        encoding="utf-8",
    )

    broken_relative, broken_slugs, referenced = link_checker.check_file(doc)

    assert broken_relative == []
    assert broken_slugs == []
    assert referenced == {"workload-unavailable"}


def test_external_non_placeholder_link_is_never_checked(link_checker, tmp_path):
    """A link to a real, unrelated external host (e.g. Argo Rollouts' own
    docs) must never be reported as broken — this checker does not fetch
    anything, by design (see the script's module docstring)."""
    doc = tmp_path / "a.md"
    doc.write_text(
        "See [Argo Rollouts docs](https://argoproj.github.io/argo-rollouts/features/analysis/).\n",
        encoding="utf-8",
    )

    broken_relative, broken_slugs, referenced = link_checker.check_file(doc)

    assert broken_relative == []
    assert broken_slugs == []


def test_pure_anchor_link_is_not_treated_as_a_file(link_checker, tmp_path):
    doc = tmp_path / "a.md"
    doc.write_text("See [detection](#detection).\n", encoding="utf-8")

    broken_relative, _, _ = link_checker.check_file(doc)

    assert broken_relative == []


def test_missing_runbooks_directory_raises_clearly(link_checker, monkeypatch, tmp_path):
    monkeypatch.setattr(link_checker, "RUNBOOKS_DIR", tmp_path / "does-not-exist")

    with pytest.raises(FileNotFoundError, match="runbooks directory not found"):
        link_checker.find_markdown_files()


def test_empty_runbooks_directory_raises_clearly(link_checker, monkeypatch, tmp_path):
    monkeypatch.setattr(link_checker, "RUNBOOKS_DIR", tmp_path)

    with pytest.raises(FileNotFoundError, match="no markdown files found"):
        link_checker.find_markdown_files()


def test_required_slug_never_referenced_fails_run(link_checker, monkeypatch, tmp_path):
    """If a runbook is renamed/rewritten and stops citing its own required
    `runbook_url` slug, that must fail the check — the slug is what a real
    SigNoz alert's `runbook_url` label would point at."""
    monkeypatch.setattr(link_checker, "RUNBOOKS_DIR", tmp_path)
    (tmp_path / "incomplete.md").write_text(
        "# no slugs referenced here\n", encoding="utf-8"
    )

    exit_code = link_checker.run()

    assert exit_code == 1
