"""Tests for chain.skills.listed export claiming."""

from __future__ import annotations

from honeyhive_daemon.state import (
    append_spool_event,
    buffer_pending_tool_event,
    claim_tool_usage_request_id,
    claim_skills_listed_export,
    drain_spool_events,
    get_expired_tool_events,
    pop_pending_tool_event,
    read_spool_events,
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


def test_spool_drain_is_atomic_clear(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HH_DAEMON_HOME", str(tmp_path / "daemon-home"))
    append_spool_event({"event_id": "e1"})
    append_spool_event({"event_id": "e2"})

    drained = drain_spool_events()

    assert [event["event_id"] for event in drained] == ["e1", "e2"]
    assert read_spool_events() == []


def test_pending_tool_events_are_session_scoped(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HH_DAEMON_HOME", str(tmp_path / "daemon-home"))
    buffer_pending_tool_event("sess-1", "tool-1", {"event_id": "e1", "start_time": 100})
    buffer_pending_tool_event("sess-2", "tool-1", {"event_id": "e2", "start_time": 100})

    assert pop_pending_tool_event("sess-1", "tool-1")["event_id"] == "e1"
    assert pop_pending_tool_event("sess-1", "tool-1") is None
    assert get_expired_tool_events(now_ms=61_000, timeout_ms=60_000) == [
        {"event_id": "e2", "start_time": 100}
    ]
