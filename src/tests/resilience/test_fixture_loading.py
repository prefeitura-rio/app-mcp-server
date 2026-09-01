"""Adversarial class: malformed/absent fixture input, at the loader level
(as opposed to the evaluator level, covered per-drill in each
`test_drill_*.py`). A missing or unreadable fixture file must fail with a
plain, unambiguous error naming the path — never silently return `None`/`{}`
and let a drill pass "by accident" on empty data.
"""

from __future__ import annotations

import json

import pytest

from src.tests.resilience.fakes import FIXTURES_DIR, load_fixture


def test_missing_fixture_file_raises_file_not_found():
    with pytest.raises(FileNotFoundError):
        load_fixture("this_fixture_does_not_exist")


def test_every_committed_fixture_file_is_valid_json():
    """Guards against a hand-edited fixture silently breaking every drill
    that reads it with an opaque `json.JSONDecodeError` deep inside a test
    — enumerates and parses every `*.json` under `fixtures/` directly."""
    fixture_files = sorted(FIXTURES_DIR.glob("*.json"))

    assert fixture_files, "expected at least one committed fixture file"

    for path in fixture_files:
        with open(path, "r", encoding="utf-8") as f:
            try:
                json.load(f)
            except json.JSONDecodeError as exc:
                pytest.fail(f"{path.name} is not valid JSON: {exc}")
