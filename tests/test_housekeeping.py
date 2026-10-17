"""Tests for daemon log rotation and state retention."""

from __future__ import annotations

from honeyhive_daemon.config import get_log_path, get_sessions_path
from honeyhive_daemon.housekeeping import run_housekeeping
from honeyhive_daemon.state import (
    append_spool_event,
    claim_tool_usage_request_id,
    discard_acked_session_events,
    buffer_pending_tool_event,
    append_chat_history,
    get_chat_history,
    load_session_index,
    log_message,
    prune_finished_sessions,
    read_spool_events,
    rotate_log,
    save_session_index,
    trim_spool,
)


DAY_MS = 24 * 60 * 60 * 1000


def _use_home(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HH_DAEMON_HOME", str(tmp_path / "daemon-home"))


def test_log_message_rotates_when_oversized(monkeypatch, tmp_path) -> None:
    _use_home(monkeypatch, tmp_path)
    monkeypatch.setenv("HH_DAEMON_LOG_MAX_BYTES", "200")
    monkeypatch.setenv("HH_DAEMON_LOG_BACKUPS", "2")

    for index in range(50):
        log_message(f"message {index}")

    log_path = get_log_path()
    log_message("after rotation")
    assert log_path.stat().st_size < 200
    assert log_path.with_name(f"{log_path.name}.1").exists()
    assert log_path.with_name(f"{log_path.name}.2").exists()
    # Only `backups` rotated files are kept.
    assert not log_path.with_name(f"{log_path.name}.3").exists()


def test_rotation_disabled_when_max_bytes_zero(monkeypatch, tmp_path) -> None:
    _use_home(monkeypatch, tmp_path)
    monkeypatch.setenv("HH_DAEMON_LOG_MAX_BYTES", "0")

    for index in range(20):
        log_message(f"message {index}")

    log_path = get_log_path()
    assert len(log_path.read_text(encoding="utf-8").splitlines()) == 20
    assert not log_path.with_name(f"{log_path.name}.1").exists()


def test_rotate_log_truncates_without_backups(monkeypatch, tmp_path) -> None:
    _use_home(monkeypatch, tmp_path)
    log_message("x" * 500)

    assert rotate_log(max_bytes=100, backups=0) is True

    log_path = get_log_path()
    assert log_path.read_text(encoding="utf-8") == ""
    assert not log_path.with_name(f"{log_path.name}.1").exists()


def test_trim_spool_keeps_newest_events(monkeypatch, tmp_path) -> None:
    _use_home(monkeypatch, tmp_path)
    for index in range(5):
        append_spool_event({"event_id": f"e{index}"})

    assert trim_spool(2) == 3
    assert [event["event_id"] for event in read_spool_events()] == ["e3", "e4"]
    assert trim_spool(2) == 0


def test_prune_finished_sessions_drops_stale_state(monkeypatch, tmp_path) -> None:
    _use_home(monkeypatch, tmp_path)
    now_ms = 100 * DAY_MS
    save_session_index(
        {
            "old": {
                "session_id": "old",
                "artifact_pushed": True,
                "last_activity_ms": now_ms - 30 * DAY_MS,
                "artifact_pushed_at_ms": now_ms - 30 * DAY_MS,
            },
            "recent": {
                "session_id": "recent",
                "artifact_pushed": True,
                "last_activity_ms": now_ms - 1000,
                "artifact_pushed_at_ms": now_ms - 1000,
            },
            "unfinished": {
                "session_id": "unfinished",
                "artifact_pushed": False,
                "last_activity_ms": now_ms - 30 * DAY_MS,
            },
        }
    )
    append_chat_history("old", "user", "hello")
    append_chat_history("recent", "user", "hi")
    buffer_pending_tool_event("old", "tool-1", {"event_id": "t1", "start_time": 0})

    assert prune_finished_sessions(7 * DAY_MS, now_ms=now_ms) == 1

    assert sorted(load_session_index()) == ["recent", "unfinished"]
    assert get_chat_history("old") == []
    assert get_chat_history("recent") == [{"role": "user", "content": "hi"}]
    from honeyhive_daemon.state import _load_pending_tools

    assert _load_pending_tools() == {}


def test_discard_acked_session_events(monkeypatch, tmp_path) -> None:
    _use_home(monkeypatch, tmp_path)
    save_session_index(
        {
            "acked": {"session_id": "acked", "artifact_pushed": True},
            "other": {"session_id": "other"},
        }
    )
    append_chat_history("acked", "user", "hello")
    append_chat_history("other", "user", "hi")
    buffer_pending_tool_event("acked", "tool-1", {"event_id": "t1", "start_time": 0})
    buffer_pending_tool_event("other", "tool-1", {"event_id": "t2", "start_time": 0})
    claim_tool_usage_request_id("acked", "req-1")

    discard_acked_session_events("acked")

    assert get_chat_history("acked") == []
    assert get_chat_history("other") == [{"role": "user", "content": "hi"}]
    from honeyhive_daemon.state import _load_pending_tools

    assert list(_load_pending_tools()) == ["other:tool-1"]
    index = load_session_index()
    # The index entry survives so a resumed session isn't re-exported.
    assert index["acked"]["artifact_pushed"] is True
    assert "tool_usage_request_ids" not in index["acked"]


def test_prune_disabled_when_retention_zero(monkeypatch, tmp_path) -> None:
    _use_home(monkeypatch, tmp_path)
    save_session_index(
        {"old": {"session_id": "old", "artifact_pushed": True, "last_activity_ms": 0}}
    )

    assert prune_finished_sessions(0, now_ms=100 * DAY_MS) == 0
    assert list(load_session_index()) == ["old"]


def test_run_housekeeping_reports_work(monkeypatch, tmp_path) -> None:
    _use_home(monkeypatch, tmp_path)
    monkeypatch.setenv("HH_DAEMON_LOG_MAX_BYTES", "100")
    monkeypatch.setenv("HH_DAEMON_LOG_BACKUPS", "1")
    monkeypatch.setenv("HH_DAEMON_SPOOL_MAX_EVENTS", "1")
    monkeypatch.setenv("HH_DAEMON_STATE_RETENTION_DAYS", "7")
    for index in range(3):
        append_spool_event({"event_id": f"e{index}"})
    save_session_index(
        {
            "old": {
                "session_id": "old",
                "artifact_pushed": True,
                "last_activity_ms": 0,
                "artifact_pushed_at_ms": 0,
            }
        }
    )
    # Written with rotation disabled so housekeeping is what rotates it.
    monkeypatch.setenv("HH_DAEMON_LOG_MAX_BYTES", "0")
    log_message("x" * 500)
    monkeypatch.setenv("HH_DAEMON_LOG_MAX_BYTES", "100")

    report = run_housekeeping()

    assert report.log_rotated is True
    assert report.spool_events_dropped == 2
    assert report.sessions_pruned == 1
    assert report.did_work() is True
    assert get_sessions_path().exists()
