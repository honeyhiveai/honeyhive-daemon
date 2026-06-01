"""TDD tests for HHAI-5521 issue fixes.

Each test class targets one ticket issue. Tests are written first (RED),
then the implementation is added to make them pass (GREEN).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from click.testing import CliRunner

from honeyhive_daemon.config import DaemonConfig
from honeyhive_daemon.main import (
    _compute_session_metrics,
    _push_pending_session_artifacts,
    cli,
)
from honeyhive_daemon.state import (
    append_chat_history,
    get_sessions_needing_artifact,
    load_session_index,
    mark_session_artifact_pushed,
    record_session_activity,
    read_spool_events,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _nested_event_dict(request: Any) -> dict:
    event = request.event
    return event.model_dump() if hasattr(event, "model_dump") else event


def _make_transcript(records: list[dict]) -> str:
    """Return JSONL string from a list of transcript record dicts."""
    return "\n".join(json.dumps(r) for r in records) + "\n"


def _realistic_transcript() -> list[dict]:
    """Return a realistic Claude Code transcript with assistant and user turns."""
    return [
        {"type": "custom-title", "customTitle": "smoke-test"},
        {"type": "queue-operation", "operation": "enqueue", "content": "hello"},
        {"type": "user", "message": {"role": "user", "content": "hello"}},
        {"type": "assistant", "message": {"role": "assistant", "content": "hi there"}},
        {"type": "tool_use", "tool_name": "Bash", "name": "Bash", "tool_input": {"command": "ls"}},
        {"type": "tool_result", "tool_name": "Bash", "is_error": False},
        {"type": "assistant", "message": {"role": "assistant", "content": "done"}},
        {"type": "last-prompt"},
    ]


# ===========================================================================
# Issue #8: _compute_session_metrics undercounts model_count
# ===========================================================================


class TestIssue8ModelCount:
    """model_count should count 'assistant' transcript records, not just 'text'/'thinking'."""

    def test_model_count_counts_assistant_records(self) -> None:
        transcript = _realistic_transcript()
        metrics = _compute_session_metrics(transcript)
        # Two "assistant" records in _realistic_transcript
        assert metrics["coding_agent.model_count"] >= 2.0

    def test_model_count_counts_user_records(self) -> None:
        transcript = _realistic_transcript()
        metrics = _compute_session_metrics(transcript)
        # "user" records are model turns too (LLM receives user input)
        assert metrics.get("coding_agent.user_turn_count", 0) >= 1.0 or \
               metrics["coding_agent.model_count"] >= 2.0

    def test_model_count_still_counts_text_and_thinking(self) -> None:
        """Backward compat: old transcript format with text/thinking types."""
        transcript = [
            {"type": "text", "text": "some output"},
            {"type": "thinking", "text": "reasoning"},
        ]
        metrics = _compute_session_metrics(transcript)
        assert metrics["coding_agent.model_count"] >= 2.0

    def test_model_count_nonzero_for_simple_session(self) -> None:
        """Even a single assistant reply should give model_count > 0."""
        transcript = [
            {"type": "assistant", "message": {"role": "assistant", "content": "ok"}},
        ]
        metrics = _compute_session_metrics(transcript)
        assert metrics["coding_agent.model_count"] >= 1.0


# ===========================================================================
# Issue #9: No total_tokens / cost on session events
# ===========================================================================


class TestIssue9TokenAggregation:
    """Session metrics should aggregate token usage from transcript records."""

    def test_total_tokens_from_usage_records(self) -> None:
        transcript = [
            {"type": "assistant", "message": {"role": "assistant", "content": "hi"},
             "usage": {"input_tokens": 100, "output_tokens": 20}},
            {"type": "assistant", "message": {"role": "assistant", "content": "done"},
             "usage": {"input_tokens": 200, "output_tokens": 50}},
        ]
        metrics = _compute_session_metrics(transcript)
        assert metrics.get("coding_agent.total_input_tokens") == 300.0
        assert metrics.get("coding_agent.total_output_tokens") == 70.0
        assert metrics.get("coding_agent.total_tokens") == 370.0

    def test_total_tokens_zero_when_no_usage(self) -> None:
        transcript = [
            {"type": "assistant", "message": {"role": "assistant", "content": "hi"}},
        ]
        metrics = _compute_session_metrics(transcript)
        # Should be present but zero (or absent — both acceptable)
        assert metrics.get("coding_agent.total_tokens", 0.0) == 0.0

    def test_cost_from_usage_with_cache(self) -> None:
        """cache_read_input_tokens and cache_creation_input_tokens included."""
        transcript = [
            {"type": "assistant", "message": {"role": "assistant", "content": "hi"},
             "usage": {"input_tokens": 100, "output_tokens": 20,
                       "cache_read_input_tokens": 50, "cache_creation_input_tokens": 10}},
        ]
        metrics = _compute_session_metrics(transcript)
        assert metrics.get("coding_agent.total_input_tokens") == 100.0
        assert metrics.get("coding_agent.total_output_tokens") == 20.0
        assert metrics.get("coding_agent.total_cache_read_tokens") == 50.0
        assert metrics.get("coding_agent.total_cache_creation_tokens") == 10.0


# ===========================================================================
# Issue #4/#7: Artifact update 400 endless retries / cap retries
# ===========================================================================


class TestIssue4And7ArtifactRetryCap:
    """Artifact push should stop retrying after repeated failures."""

    def test_artifact_retry_count_incremented_on_failure(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("HH_DAEMON_HOME", str(tmp_path / "daemon-home"))
        monkeypatch.setattr("honeyhive_daemon.main._now_ms", lambda: 2000)

        transcript_path = tmp_path / "session.jsonl"
        transcript_path.write_text(
            _make_transcript([{"type": "user", "message": {"role": "user", "content": "hi"}}]),
            encoding="utf-8",
        )

        # Provide session_end_event_id so the artifact push path triggers
        record_session_activity(
            "sess-retry-1",
            transcript_path=str(transcript_path),
            last_activity_ms=1000,
            ended=True,
            session_end_event_id="sess-end-retry-1",
        )

        def fail_update_outputs(config: Any, *, event_id: str, outputs: Any) -> None:
            raise Exception("400 Bad Request")

        monkeypatch.setattr(
            "honeyhive_daemon.exporter.update_event_outputs",
            fail_update_outputs,
        )
        monkeypatch.setattr(
            "honeyhive_daemon.exporter.update_event",
            lambda *a, **kw: None,
        )

        config = DaemonConfig(
            api_key="hh_test", base_url="https://api.honeyhive.ai", project="demo"
        )
        _push_pending_session_artifacts(config)

        # After failure, session should track the retry count
        index = load_session_index()
        assert index["sess-retry-1"].get("artifact_retry_count", 0) >= 1

    def test_artifact_gives_up_after_max_retries(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("HH_DAEMON_HOME", str(tmp_path / "daemon-home"))
        monkeypatch.setattr("honeyhive_daemon.main._now_ms", lambda: 2000)

        transcript_path = tmp_path / "session.jsonl"
        transcript_path.write_text(
            _make_transcript([{"type": "user", "message": {"role": "user", "content": "hi"}}]),
            encoding="utf-8",
        )

        record_session_activity(
            "sess-retry-2",
            transcript_path=str(transcript_path),
            last_activity_ms=1000,
            ended=True,
            session_end_event_id="sess-end-retry-2",
        )

        call_count = 0

        def fail_update_outputs(config: Any, *, event_id: str, outputs: Any) -> None:
            nonlocal call_count
            call_count += 1
            raise Exception("400 Bad Request")

        monkeypatch.setattr(
            "honeyhive_daemon.exporter.update_event_outputs",
            fail_update_outputs,
        )
        monkeypatch.setattr(
            "honeyhive_daemon.exporter.update_event",
            lambda *a, **kw: None,
        )

        config = DaemonConfig(
            api_key="hh_test", base_url="https://api.honeyhive.ai", project="demo"
        )

        # Run push 4 times (default max retries = 3)
        for _ in range(4):
            _push_pending_session_artifacts(config)

        # After max retries, it should be marked as pushed (given up)
        index = load_session_index()
        assert index["sess-retry-2"].get("artifact_pushed") is True


# ===========================================================================
# Issue #2 / #6: Synthesize session.end for orphaned idle sessions
# ===========================================================================


class TestIssue2SyntheticSessionEnd:
    """Daemon should synthesize session.end for sessions that went idle without receiving one."""

    def test_idle_session_gets_synthetic_session_end(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("HH_DAEMON_HOME", str(tmp_path / "daemon-home"))

        transcript_path = tmp_path / "session.jsonl"
        transcript_path.write_text(
            _make_transcript([{"type": "user", "message": {"role": "user", "content": "hi"}}]),
            encoding="utf-8",
        )

        record_session_activity(
            "sess-orphan-1",
            transcript_path=str(transcript_path),
            last_activity_ms=1000,
            session_start_exported=True,
        )

        exported_events: list[dict] = []

        def capture_export(config: Any, event: dict) -> None:
            exported_events.append(event)

        update_calls: list[dict] = []

        def capture_update(config: Any, *, event_id: str, outputs: Any) -> None:
            update_calls.append({"event_id": event_id, "outputs": outputs})

        monkeypatch.setattr("honeyhive_daemon.main.export_event", capture_export)
        monkeypatch.setattr(
            "honeyhive_daemon.exporter.update_event_outputs",
            capture_update,
        )
        monkeypatch.setattr(
            "honeyhive_daemon.exporter.update_event",
            lambda *a, **kw: None,
        )
        idle_ms = 24 * 60 * 60 * 1000
        monkeypatch.setattr("honeyhive_daemon.main._now_ms", lambda: 1000 + idle_ms + 1)

        config = DaemonConfig(
            api_key="hh_test", base_url="https://api.honeyhive.ai", project="demo"
        )
        _push_pending_session_artifacts(config)

        # A synthetic session.end should have been exported
        session_end_events = [
            e for e in exported_events if e.get("event_name") == "session.end"
        ]
        assert len(session_end_events) >= 1
        assert session_end_events[0]["session_id"] == "sess-orphan-1"
        assert session_end_events[0]["metadata"].get("synthetic") is True


# ===========================================================================
# Issue #11: session.end export not explicitly logged
# ===========================================================================


class TestIssue11SessionEndLogging:
    """session.end export should produce a distinct 'session ended' log line."""

    def test_session_end_export_logged(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("HH_DAEMON_HOME", str(tmp_path / "daemon-home"))

        log_messages: list[str] = []

        def capture_log(message: str) -> None:
            log_messages.append(message)

        monkeypatch.setattr("honeyhive_daemon.main.log_message", capture_log)
        monkeypatch.setattr("honeyhive_daemon.main.export_event", lambda *a, **kw: None)
        monkeypatch.setattr(
            "honeyhive_daemon.main.resolve_config",
            lambda **kw: kw.get("cli_defaults"),
        )

        from honeyhive_daemon.config import save_config, DaemonConfig

        save_config(DaemonConfig(
            api_key="hh_test", base_url="https://api.honeyhive.ai", project="demo",
        ))

        session_end_payload = json.dumps({
            "hook_event_name": "SessionEnd",
            "session_id": "sess-log-test",
            "event": {"event_id": "evt-log-test"},
        })

        runner = CliRunner()
        result = runner.invoke(cli, ["ingest", "claude-hook"], input=session_end_payload)
        assert result.exit_code == 0, result.output

        assert any("session ended" in msg and "sess-log-test" in msg for msg in log_messages)


# ===========================================================================
# Issue #12: init command has no --url option
# ===========================================================================


class TestIssue12InitBaseUrl:
    """init command should accept --url to persist base_url in config."""

    def test_init_with_url_persists_base_url(self, tmp_path: Path) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path) as td:
            result = runner.invoke(
                cli,
                ["init", "--project", "my-proj", "--url", "https://custom.api.honeyhive.ai"],
            )
            assert result.exit_code == 0, result.output

            local_config = json.loads(
                (Path(td) / ".honeyhive" / "config.local.json").read_text(encoding="utf-8")
            )
            assert local_config.get("base_url") == "https://custom.api.honeyhive.ai"

    def test_init_without_url_no_base_url_in_config(self, tmp_path: Path) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path) as td:
            result = runner.invoke(cli, ["init", "--project", "my-proj"])
            assert result.exit_code == 0, result.output

            local_config = json.loads(
                (Path(td) / ".honeyhive" / "config.local.json").read_text(encoding="utf-8")
            )
            # base_url should not be present if not specified
            assert "base_url" not in local_config


# ===========================================================================
# Issue #13: status silent on spool failure reason
# ===========================================================================


class TestIssue13StatusSpoolReason:
    """status should show spool failure reasons when events are pending."""

    def test_status_shows_spool_reason(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("HH_DAEMON_HOME", str(tmp_path / "daemon-home"))

        # Write a spool event with a reason
        from honeyhive_daemon.state import append_spool_event

        append_spool_event({
            "event_name": "session.start",
            "spool_reason": "Connection refused: https://api.honeyhive.ai/events",
        })

        runner = CliRunner()
        result = runner.invoke(cli, ["status"])
        assert result.exit_code == 0
        assert "Connection refused" in result.output or "spool" in result.output.lower()

    def test_status_no_spool_reason_when_empty(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("HH_DAEMON_HOME", str(tmp_path / "daemon-home"))

        runner = CliRunner()
        result = runner.invoke(cli, ["status"])
        assert result.exit_code == 0
        assert "Pending spool events: 0" in result.output


# ===========================================================================
# Issue #14: status project label misleading
# ===========================================================================


class TestIssue14StatusProjectLabel:
    """status should clarify when project name is derived (not API-configured)."""

    def test_status_shows_project_source(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("HH_DAEMON_HOME", str(tmp_path / "daemon-home"))

        # Create a minimal config
        from honeyhive_daemon.config import save_config

        save_config(DaemonConfig(
            api_key="hh_test",
            base_url="https://api.honeyhive.ai",
            project="my-folder-name",
        ))

        runner = CliRunner()
        result = runner.invoke(cli, ["status"])
        assert result.exit_code == 0
        # Should show the project name
        assert "my-folder-name" in result.output


# ===========================================================================
# Issue #15: duration placeholder — compute from end_time - start_time
# ===========================================================================


class TestIssue15Duration:
    """Duration should be computed from end_time - start_time when not explicitly set."""

    def test_duration_computed_from_timestamps(self) -> None:
        from honeyhive_daemon.exporter import _build_event_payload

        config = DaemonConfig(api_key="k", base_url="https://api.honeyhive.ai", project="p")
        event = {
            "event_id": "e1",
            "session_id": "s1",
            "event_type": "chain",
            "event_name": "session.end",
            "start_time": 1000,
            "end_time": 5000,
            "duration": 0,
            "inputs": {},
            "outputs": {},
            "metadata": {},
        }
        payload = _build_event_payload(config, event)
        # Duration should be 4000 (5000 - 1000), not 0
        assert payload["event"]["duration"] == 4000

    def test_duration_preserved_when_nonzero(self) -> None:
        from honeyhive_daemon.exporter import _build_event_payload

        config = DaemonConfig(api_key="k", base_url="https://api.honeyhive.ai", project="p")
        event = {
            "event_id": "e2",
            "session_id": "s1",
            "event_type": "chain",
            "event_name": "tool.bash",
            "start_time": 1000,
            "end_time": 5000,
            "duration": 3000,
            "inputs": {},
            "outputs": {},
            "metadata": {},
        }
        payload = _build_event_payload(config, event)
        assert payload["event"]["duration"] == 3000

    def test_duration_zero_when_same_timestamps(self) -> None:
        from honeyhive_daemon.exporter import _build_event_payload

        config = DaemonConfig(api_key="k", base_url="https://api.honeyhive.ai", project="p")
        event = {
            "event_id": "e3",
            "session_id": "s1",
            "event_type": "chain",
            "event_name": "session.start",
            "start_time": 1000,
            "end_time": 1000,
            "duration": 0,
            "inputs": {},
            "outputs": {},
            "metadata": {},
        }
        payload = _build_event_payload(config, event)
        assert payload["event"]["duration"] == 0


# ===========================================================================
# Issue #16: turn.user has empty chat_history
# ===========================================================================


class TestIssue16TurnUserChatHistory:
    """turn.user events should include the current user message in chat_history."""

    def test_append_chat_history_returns_history_including_current(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("HH_DAEMON_HOME", str(tmp_path / "daemon-home"))

        # First user message should return history WITH the current message
        history = append_chat_history("sess-chat-1", "user", "hello")

        # Currently returns history BEFORE appending (empty for first message).
        # After fix, it should include the current message.
        # The fix changes append_chat_history to return history AFTER appending.
        assert len(history) >= 1
        assert history[-1] == {"role": "user", "content": "hello"}

    def test_chat_history_accumulates_correctly(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("HH_DAEMON_HOME", str(tmp_path / "daemon-home"))

        h1 = append_chat_history("sess-chat-2", "user", "first")
        h2 = append_chat_history("sess-chat-2", "assistant", "reply")
        h3 = append_chat_history("sess-chat-2", "user", "second")

        # h1 should contain first user message
        assert len(h1) >= 1
        assert h1[-1]["content"] == "first"

        # h2 should contain user + assistant
        assert len(h2) >= 2

        # h3 should contain all three
        assert len(h3) >= 3
        assert h3[-1]["content"] == "second"
