"""Tests for chain.skills.listed export claiming."""

from __future__ import annotations

from honeyhive_daemon.state import (
    claim_tool_usage_request_id,
    claim_skills_listed_export,
    release_skills_listed_export,
)


def test_claim_skills_listed_export_once(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HH_DAEMON_HOME", str(tmp_path / "daemon-home"))

    assert claim_skills_listed_export("sess-1") is True
    assert claim_skills_listed_export("sess-1") is False
    assert claim_skills_listed_export("sess-2") is True


def test_release_skills_listed_export_allows_retry(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HH_DAEMON_HOME", str(tmp_path / "daemon-home"))

    assert claim_skills_listed_export("sess-1") is True
    release_skills_listed_export("sess-1")
    assert claim_skills_listed_export("sess-1") is True


def test_claim_tool_usage_request_id_once(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HH_DAEMON_HOME", str(tmp_path / "daemon-home"))

    assert claim_tool_usage_request_id("sess-1", "req-1") is True
    assert claim_tool_usage_request_id("sess-1", "req-1") is False
    assert claim_tool_usage_request_id("sess-1", "req-2") is True
