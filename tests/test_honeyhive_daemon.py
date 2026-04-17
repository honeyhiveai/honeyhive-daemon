"""Unit tests for the minimal HoneyHive daemon."""

from __future__ import annotations

import json
from pathlib import Path

from click.testing import CliRunner

from honeyhive_daemon.ci import _extract_error
from honeyhive_daemon.claude_hooks import install_claude_hooks, normalize_claude_payload
from honeyhive_daemon.config import DaemonConfig
from honeyhive_daemon.exporter import export_event
from honeyhive_daemon.git_hooks import HOOK_MARKER_START, install_post_commit_hook
from honeyhive_daemon.main import _push_pending_session_artifacts, cli
from honeyhive_daemon.state import (
    get_sessions_needing_artifact,
    mark_session_artifact_pushed,
    record_session_activity,
)


def test_install_claude_hooks_idempotent(tmp_path: Path) -> None:
    settings_path = tmp_path / "settings.json"

    changed = install_claude_hooks(settings_path, "honeyhive-daemon ingest claude-hook")
    changed_again = install_claude_hooks(
        settings_path, "honeyhive-daemon ingest claude-hook"
    )

    data = json.loads(settings_path.read_text(encoding="utf-8"))
    assert changed is True
    assert changed_again is False
    assert "SessionStart" in data["hooks"]
    assert "UserPromptSubmit" in data["hooks"]
    assert "PreToolUse" in data["hooks"]
    assert "PermissionRequest" in data["hooks"]
    assert "PostToolUse" in data["hooks"]
    assert "PostToolUseFailure" in data["hooks"]
    assert "Notification" in data["hooks"]
    assert "SubagentStart" in data["hooks"]
    assert "SubagentStop" in data["hooks"]
    assert "PreCompact" in data["hooks"]
    assert "Stop" in data["hooks"]
    assert "SessionEnd" in data["hooks"]
    assert "TeammateIdle" in data["hooks"]
    assert "TaskCompleted" in data["hooks"]
    assert "ConfigChange" in data["hooks"]
    assert "WorktreeCreate" in data["hooks"]
    assert "WorktreeRemove" in data["hooks"]


def test_normalize_claude_session_start() -> None:
    event = normalize_claude_payload(
        {
            "hook_event_name": "SessionStart",
            "session_id": "sess-1",
            "cwd": "/tmp/demo",
        }
    )

    assert event is not None
    assert event["event_id"] == "sess-1"
    assert event["event_name"] == "session.start"
    assert event["event_type"] == "session"
    assert event["parent_id"] is None
    assert event["metadata"]["agent.provider"] == "anthropic"
    assert event["metadata"]["agent.product"] == "claude-code"


def test_normalize_claude_session_start_with_transcript_session_name(
    tmp_path: Path,
) -> None:
    """Session name is extracted from the transcript's custom-title line."""
    from honeyhive_daemon.claude_hooks import _read_session_name_from_transcript

    # Clear the LRU cache so our tmp file is read fresh
    _read_session_name_from_transcript.cache_clear()

    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "custom-title",
                "customTitle": "waggle-focus-watcher",
                "sessionId": "sess-name-1",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    event = normalize_claude_payload(
        {
            "hook_event_name": "SessionStart",
            "session_id": "sess-name-1",
            "cwd": "/tmp/demo",
            "transcript_path": str(transcript),
        }
    )

    assert event is not None
    assert event["metadata"]["session_name"] == "waggle-focus-watcher"


def test_normalize_claude_session_name_from_payload() -> None:
    """If session_name is in the hook payload, prefer it over transcript."""
    event = normalize_claude_payload(
        {
            "hook_event_name": "SessionStart",
            "session_id": "sess-name-2",
            "cwd": "/tmp/demo",
            "session_name": "my-named-session",
        }
    )

    assert event is not None
    assert event["metadata"]["session_name"] == "my-named-session"


def test_session_name_propagated_to_all_events(tmp_path: Path) -> None:
    """Session name appears in metadata for non-session events too."""
    from honeyhive_daemon.claude_hooks import _read_session_name_from_transcript

    _read_session_name_from_transcript.cache_clear()

    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "custom-title",
                "customTitle": "my-tool-session",
                "sessionId": "sess-tool-1",
            }
        )
        + "\n",
        encoding="utf-8",
    )

    event = normalize_claude_payload(
        {
            "hook_event_name": "PostToolUse",
            "session_id": "sess-tool-1",
            "tool_name": "Bash",
            "tool_input": {"command": "ls"},
            "tool_response": {"exit_code": 0},
            "transcript_path": str(transcript),
        }
    )

    assert event is not None
    assert event["metadata"]["session_name"] == "my-tool-session"


def test_normalize_claude_bash_tool() -> None:
    event = normalize_claude_payload(
        {
            "hook_event_name": "PostToolUse",
            "session_id": "sess-2",
            "tool_name": "Bash",
            "tool_input": {"command": "pytest"},
            "tool_response": {"exit_code": 0},
        }
    )

    assert event is not None
    assert event["event_name"] == "tool.Bash"
    assert event["parent_id"] == "sess-2"
    assert event["metadata"]["tool.kind"] == "bash"
    assert event["metadata"]["tool.command"] == "pytest"
    assert event["metadata"]["tool.phase"] == "post"
    assert event["outputs"]["tool_response"] == {"exit_code": 0}


def test_normalize_claude_stop_event_is_chain() -> None:
    event = normalize_claude_payload(
        {
            "hook_event_name": "Stop",
            "session_id": "sess-stop-1",
            "cwd": "/tmp/demo",
        }
    )

    assert event is not None
    assert event["event_name"] == "turn.agent"
    assert event["event_type"] == "model"
    assert event["parent_id"] == "sess-stop-1"


def test_normalize_claude_user_prompt_event() -> None:
    event = normalize_claude_payload(
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "sess-prompt-1",
            "prompt": "show me the failing tests",
        }
    )

    assert event is not None
    assert event["event_name"] == "turn.user"
    assert event["event_type"] == "model"
    assert event["parent_id"] == "sess-prompt-1"
    # chat_history is injected at ingest time from session state, not at normalize time
    assert "chat_history" not in event.get("inputs", {})
    assert event["outputs"]["role"] == "user"
    assert event["outputs"]["content"] == "show me the failing tests"


def test_normalize_claude_pretool_generic_fallback() -> None:
    event = normalize_claude_payload(
        {
            "hook_event_name": "PreToolUse",
            "session_id": "sess-generic-1",
            "tool_name": "TodoWrite",
            "tool_input": {"items": ["a", "b"]},
        }
    )

    assert event is not None
    assert event["event_name"] == "tool.TodoWrite"
    assert event["event_type"] == "tool"
    assert event["metadata"]["tool.kind"] == "generic"
    assert event["metadata"]["tool.name"] == "TodoWrite"


def test_normalize_claude_subagent_stop_outputs_message() -> None:
    event = normalize_claude_payload(
        {
            "hook_event_name": "SubagentStop",
            "session_id": "sess-subagent-1",
            "agent_id": "agent-1",
            "agent_type": "reviewer",
            "last_assistant_message": "I found one issue.",
        }
    )

    assert event is not None
    assert event["event_name"] == "chain.subagent.stop"
    assert event["outputs"]["message"] == "I found one issue."
    assert event["metadata"]["agent.subagent_id"] == "agent-1"


def test_normalize_claude_file_events() -> None:
    edit_event = normalize_claude_payload(
        {
            "hook_event_name": "PostToolUse",
            "session_id": "sess-3",
            "tool_name": "Edit",
            "tool_input": {"file_path": "/tmp/app.py"},
            "tool_response": {"status": "ok"},
        }
    )
    create_event = normalize_claude_payload(
        {
            "hook_event_name": "PostToolUse",
            "session_id": "sess-3",
            "tool_name": "Write",
            "tool_input": {"file_path": "/tmp/new.py"},
            "tool_response": {"type": "create"},
        }
    )

    assert edit_event is not None
    assert create_event is not None
    assert edit_event["event_name"] == "tool.Edit"
    assert create_event["event_name"] == "tool.Write"
    assert edit_event["metadata"]["file.operation"] == "edit"
    assert create_event["metadata"]["file.operation"] == "create"


def test_install_post_commit_hook_idempotent(tmp_path: Path, monkeypatch) -> None:
    repo_root = tmp_path / "repo"
    hook_path = repo_root / ".git" / "hooks" / "post-commit"
    hook_path.parent.mkdir(parents=True)

    def fake_run(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        class Result:
            stdout = ".git/hooks/post-commit\n"

        return Result()

    monkeypatch.setattr("subprocess.run", fake_run)

    changed = install_post_commit_hook(
        repo_root, "honeyhive-daemon ingest git-post-commit"
    )
    changed_again = install_post_commit_hook(
        repo_root, "honeyhive-daemon ingest git-post-commit"
    )

    assert changed is True
    assert changed_again is False
    assert HOOK_MARKER_START in hook_path.read_text(encoding="utf-8")


def test_cli_status_without_config(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("HH_DAEMON_HOME", str(tmp_path / "daemon-home"))
    runner = CliRunner()
    result = runner.invoke(cli, ["status"])

    assert result.exit_code == 0
    assert "Configured: no" in result.output


def test_export_session_event_includes_session_name(monkeypatch, tmp_path: Path) -> None:
    """session_name is promoted to a top-level field on session events."""
    captured = {}
    monkeypatch.setenv("HH_DAEMON_HOME", str(tmp_path / "daemon-home"))

    class FakeEventsAPI:
        def create_event(self, request) -> None:  # type: ignore[no-untyped-def]
            captured["event"] = request.event

    class FakeHoneyHive:
        def __init__(self, api_key: str, base_url: str) -> None:
            self.events = FakeEventsAPI()

    monkeypatch.setattr("honeyhive_daemon.exporter.HoneyHive", FakeHoneyHive)

    config = DaemonConfig(
        api_key="hh_test",
        base_url="https://api.honeyhive.ai",
        project="test-project",
    )
    event = {
        "event_id": "sess-1",
        "session_id": "sess-1",
        "event_type": "session",
        "event_name": "session.start",
        "start_time": 1000,
        "end_time": 1000,
        "duration": 0,
        "metadata": {"session_name": "waggle-focus-watcher"},
        "inputs": {},
        "outputs": {},
    }

    export_event(config, event)

    assert captured["event"]["session_name"] == "waggle-focus-watcher"
    # Also preserved in metadata
    assert captured["event"]["metadata"]["session_name"] == "waggle-focus-watcher"


def test_export_tool_event_no_session_name_field(monkeypatch, tmp_path: Path) -> None:
    """session_name is NOT promoted to top-level on non-session events."""
    captured = {}
    monkeypatch.setenv("HH_DAEMON_HOME", str(tmp_path / "daemon-home"))

    class FakeEventsAPI:
        def create_event(self, request) -> None:  # type: ignore[no-untyped-def]
            captured["event"] = request.event

    class FakeHoneyHive:
        def __init__(self, api_key: str, base_url: str) -> None:
            self.events = FakeEventsAPI()

    monkeypatch.setattr("honeyhive_daemon.exporter.HoneyHive", FakeHoneyHive)

    config = DaemonConfig(
        api_key="hh_test",
        base_url="https://api.honeyhive.ai",
        project="test-project",
    )
    event = {
        "event_id": "evt-1",
        "session_id": "sess-1",
        "parent_id": "sess-1",
        "event_type": "tool",
        "event_name": "tool.Bash",
        "start_time": 1000,
        "end_time": 1000,
        "duration": 0,
        "metadata": {"session_name": "waggle-focus-watcher"},
        "inputs": {},
        "outputs": {},
    }

    export_event(config, event)

    assert "session_name" not in captured["event"]
    # But still in metadata for queryability
    assert captured["event"]["metadata"]["session_name"] == "waggle-focus-watcher"


def test_export_event_posts_honeyhive_event(monkeypatch, tmp_path: Path) -> None:
    captured = {}
    monkeypatch.setenv("HH_DAEMON_HOME", str(tmp_path / "daemon-home"))

    class FakeEventsAPI:
        def create_event(self, request) -> None:  # type: ignore[no-untyped-def]
            captured["event"] = request.event

    class FakeHoneyHive:
        def __init__(self, api_key: str, base_url: str) -> None:
            captured["api_key"] = api_key
            captured["base_url"] = base_url
            self.events = FakeEventsAPI()

    monkeypatch.setattr("honeyhive_daemon.exporter.HoneyHive", FakeHoneyHive)

    config = DaemonConfig(
        api_key="hh_test",
        base_url="https://api.honeyhive.ai",
        project="ignored-project",
    )
    event = {
        "event_id": "evt-1",
        "session_id": "sess-1",
        "parent_id": "sess-1",
        "event_type": "tool",
        "event_name": "tool.bash",
        "start_time": 1000,
        "end_time": 1000,
        "duration": 0,
        "metadata": {"tool.kind": "bash"},
        "inputs": {"tool_input": {"command": "pwd"}},
        "outputs": {"tool_response": {"stdout": "/tmp"}},
        "raw": {"hook_event_name": "PostToolUse"},
    }

    export_event(config, event)

    assert captured["api_key"] == "hh_test"
    assert captured["base_url"] == "https://api.honeyhive.ai"
    assert captured["event"]["event_id"] == "evt-1"
    assert captured["event"]["project"] == "ignored-project"
    assert captured["event"]["event_type"] == "tool"
    assert captured["event"]["event_name"] == "tool.bash"
    assert captured["event"]["parent_id"] == "sess-1"
    assert captured["event"]["metadata"]["tool.kind"] == "bash"
    assert captured["event"]["inputs"]["tool_input"]["command"] == "pwd"
    assert captured["event"]["outputs"]["tool_response"]["stdout"] == "/tmp"
    assert captured["event"]["metadata"]["raw"]["hook_event_name"] == "PostToolUse"


def test_record_session_activity_and_idle_selection(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HH_DAEMON_HOME", str(tmp_path / "daemon-home"))

    record_session_activity(
        "sess-1",
        transcript_path="/tmp/transcript.jsonl",
        last_activity_ms=1000,
    )
    ready = get_sessions_needing_artifact(
        now_ms=1000 + 24 * 60 * 60 * 1000 + 1,
        idle_threshold_ms=24 * 60 * 60 * 1000,
    )

    assert len(ready) == 1
    assert ready[0]["session_id"] == "sess-1"

    mark_session_artifact_pushed("sess-1", 2000)
    ready_after_push = get_sessions_needing_artifact(
        now_ms=1000 + 24 * 60 * 60 * 1000 + 1,
        idle_threshold_ms=24 * 60 * 60 * 1000,
    )
    assert ready_after_push == []


def test_push_pending_session_artifacts_updates_root_event(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HH_DAEMON_HOME", str(tmp_path / "daemon-home"))
    captured = []
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text('{"type":"user","message":"hi"}\n', encoding="utf-8")

    def fake_update_event_outputs(config, *, event_id, outputs):  # type: ignore[no-untyped-def]
        captured.append(
            {
                "project": config.project,
                "event_id": event_id,
                "outputs": outputs,
            }
        )

    monkeypatch.setattr(
        "honeyhive_daemon.main._now_ms",
        lambda: 2000,
    )
    monkeypatch.setattr(
        "honeyhive_daemon.exporter.update_event_outputs",
        fake_update_event_outputs,
    )

    metrics_captured = []

    def fake_update_event(config, *, event_id, inputs=None, outputs=None, metrics=None):  # type: ignore[no-untyped-def]
        metrics_captured.append({"event_id": event_id, "metrics": metrics})

    monkeypatch.setattr(
        "honeyhive_daemon.exporter.update_event",
        fake_update_event,
    )

    # Accumulate chat history so the root event update fires
    from honeyhive_daemon.state import append_chat_history

    append_chat_history("sess-root-1", "user", "hi")

    record_session_activity(
        "sess-root-1",
        transcript_path=str(transcript_path),
        last_activity_ms=1000,
        ended=True,
        session_end_event_id="sess-end-1",
    )

    config = DaemonConfig(
        api_key="hh_test",
        base_url="https://api.honeyhive.ai",
        project="demo",
    )
    _push_pending_session_artifacts(config)

    # Root event gets chat_history, end event gets full artifact
    assert [item["event_id"] for item in captured] == ["sess-root-1", "sess-end-1"]
    assert all(item["project"] == "demo" for item in captured)
    # Root event: chat_history only
    assert "chat_history" in captured[0]["outputs"]
    assert captured[0]["outputs"]["chat_history"] == [{"role": "user", "content": "hi"}]
    # End event: full artifact transcript
    assert captured[1]["outputs"]["artifact"]["path"] == str(transcript_path)
    assert captured[1]["outputs"]["artifact"]["content"] == [
        {"type": "user", "message": "hi"}
    ]
    assert captured[1]["outputs"]["artifact"]["format"] == "json"
    assert captured[1]["outputs"]["artifact"]["reason"] == "session_end"
    # Metrics were attached to the root event
    assert len(metrics_captured) == 1
    assert metrics_captured[0]["event_id"] == "sess-root-1"
    assert "coding_agent.total_events" in metrics_captured[0]["metrics"]


# ---------------------------------------------------------------------------
# _extract_error — WAG-310: guard against list values in ev["error"]
# ---------------------------------------------------------------------------

def test_extract_error_string_direct() -> None:
    """Plain string in top-level error field."""
    ev = {"error": "permission denied"}
    assert _extract_error(ev) == "permission denied"


def test_extract_error_list_text_blocks() -> None:
    """Multi-block list in top-level error field must not raise AttributeError."""
    ev = {"error": [{"type": "text", "text": "tool call failed"}]}
    assert _extract_error(ev) == "tool call failed"


def test_extract_error_list_plain_strings() -> None:
    """List of plain strings in top-level error field."""
    ev = {"error": ["first error", "second error"]}
    assert _extract_error(ev) == "first error"


def test_extract_error_list_empty() -> None:
    """Empty list in top-level error field returns empty string."""
    ev = {"error": []}
    assert _extract_error(ev) == ""


def test_extract_error_none() -> None:
    """None error field falls through to tool_response."""
    ev = {"error": None, "outputs": {"tool_response": {"stderr": "bad command"}}}
    assert _extract_error(ev) == "bad command"


def test_extract_error_tool_response_list() -> None:
    """List-valued tool_response still works."""
    ev = {"outputs": {"tool_response": [{"text": "stderr: not found"}]}}
    assert _extract_error(ev) == "stderr: not found"
