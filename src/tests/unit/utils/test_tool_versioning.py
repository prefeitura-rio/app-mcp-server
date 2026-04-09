import json
from datetime import datetime
import sys
from unittest.mock import Mock, patch

tool_versioning = sys.modules["src.utils.tool_versioning"]


def test_get_git_commit_hash_success():
    completed = Mock(returncode=0, stdout="abc123\n")

    with patch.object(
        tool_versioning.subprocess, "run", return_value=completed
    ) as mock_run:
        assert tool_versioning.get_git_commit_hash() == "abc123"
        mock_run.assert_called_once_with(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            cwd=".",
        )


def test_get_git_commit_hash_failure_returns_none():
    completed = Mock(returncode=1, stdout="", stderr="fatal")

    with patch.object(tool_versioning.subprocess, "run", return_value=completed):
        assert tool_versioning.get_git_commit_hash() is None


def test_get_current_version_reads_json_file(tmp_path, monkeypatch):
    version_file = tmp_path / "tool_version.json"
    payload = {
        "version": "vabc123",
        "last_updated": "2026-03-24T12:00:00Z",
        "description": "Tool version for cache invalidation",
    }
    version_file.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(tool_versioning, "UTILS_PATH", tmp_path)

    assert tool_versioning.get_current_version() == payload


def test_get_current_version_returns_placeholder_when_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(tool_versioning, "UTILS_PATH", tmp_path)

    result = tool_versioning.get_current_version()

    assert result["version"] == "vERROR"
    assert result["description"] == "Tool version for cache invalidation"
    assert "last_updated" in result


def test_add_tool_version_wraps_response(tmp_path, monkeypatch):
    version_file = tmp_path / "tool_version.json"
    payload = {
        "version": "vabc123",
        "last_updated": "2026-03-24T12:00:00Z",
        "description": "Tool version for cache invalidation",
    }
    version_file.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(tool_versioning, "UTILS_PATH", tmp_path)

    response = {"ok": True}
    wrapped = tool_versioning.add_tool_version(response)

    assert wrapped["_tool_metadata"] == payload
    assert wrapped["data"] == response


def test_update_version_writes_file(tmp_path, monkeypatch):
    monkeypatch.setattr(tool_versioning, "UTILS_PATH", tmp_path)
    monkeypatch.setattr(tool_versioning, "get_git_commit_hash", lambda: "abc123")

    class FixedDateTime:
        @staticmethod
        def now():
            return datetime(2026, 3, 24, 12, 0, 0)

    monkeypatch.setattr(tool_versioning, "datetime", FixedDateTime)

    assert tool_versioning.update_version() is True

    written = json.loads((tmp_path / "tool_version.json").read_text(encoding="utf-8"))
    assert written["version"] == "vabc123"
    assert written["last_updated"] == "2026-03-24T12:00:00"
    assert written["description"] == "Tool version for cache invalidation"


def test_update_version_returns_false_when_git_hash_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(tool_versioning, "UTILS_PATH", tmp_path)
    monkeypatch.setattr(tool_versioning, "get_git_commit_hash", lambda: None)

    assert tool_versioning.update_version() is False
