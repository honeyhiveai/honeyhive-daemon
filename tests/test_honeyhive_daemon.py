"""Unit tests for the minimal HoneyHive daemon."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from click.testing import CliRunner

from honeyhive_daemon.ci import _extract_error
from honeyhive_daemon.claude_hooks import (
    _is_daemon_hook_command,
    get_hook_command,
    install_claude_hooks,
    normalize_claude_payload,
)
from honeyhive_daemon.config import DaemonConfig
from honeyhive_daemon.exporter import export_event
from honeyhive_daemon.git_hooks import HOOK_MARKER_START, install_post_commit_hook
from honeyhive_daemon.main import (
    _flush_spool,
    _merge_tool_events,
    _push_pending_session_artifacts,
    _session_token_metadata,
    cli,
)
from honeyhive_daemon.state import (
    append_chat_history,
    append_spool_event,
    get_sessions_needing_artifact,
    load_session_index,
    mark_session_artifact_pushed,
    read_spool_events,
    record_session_activity,
)


def test_get_hook_command_uses_resolved_binary(monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["pytest"])
    monkeypatch.setattr(
        "honeyhive_daemon.claude_hooks.shutil.which",
        lambda _: "/opt/venv/bin/honeyhive-daemon",
    )
    assert (
        get_hook_command()
        == "/opt/venv/bin/honeyhive-daemon ingest claude-hook"
    )


def test_get_hook_command_prefers_argv_over_path(
    monkeypatch, tmp_path: Path
) -> None:
    """Running ``run`` via absolute path must win over stale PATH entries."""
    fresh_bin = tmp_path / "fresh" / "bin" / "honeyhive-daemon"
    fresh_bin.parent.mkdir(parents=True)
    fresh_bin.write_text("#!/bin/sh\n", encoding="utf-8")
    fresh_bin.chmod(0o755)

    monkeypatch.setattr(sys, "argv", [str(fresh_bin), "run"])
    monkeypatch.setattr(
        "honeyhive_daemon.claude_hooks.shutil.which",
        lambda _: str(tmp_path / "stale" / "bin" / "honeyhive-daemon"),
    )

    assert get_hook_command() == f"{fresh_bin} ingest claude-hook"


def test_get_hook_command_quotes_paths_with_spaces(monkeypatch) -> None:
    monkeypatch.setattr(sys, "argv", ["pytest"])
    monkeypatch.setattr(
        "honeyhive_daemon.claude_hooks.shutil.which",
        lambda _: "/opt/my venv/bin/honeyhive-daemon",
    )
    assert (
        get_hook_command()
        == "'/opt/my venv/bin/honeyhive-daemon' ingest claude-hook"
    )


def test_is_daemon_hook_command_recognizes_exe_and_rejects_wrappers() -> None:
    assert _is_daemon_hook_command(
        "C:/Tools/honeyhive-daemon.exe ingest claude-hook"
    )
    assert _is_daemon_hook_command(
        "/opt/venv/bin/honeyhive-daemon ingest claude-hook"
    )
    assert _is_daemon_hook_command(
        '"C:\\Program Files\\HoneyHive\\honeyhive-daemon.exe" ingest claude-hook'
    )
    assert not _is_daemon_hook_command(
        "echo warmup && /opt/venv/bin/honeyhive-daemon ingest claude-hook"
    )


def test_install_claude_hooks_idempotent_windows_quoted_path(
    tmp_path: Path, monkeypatch
) -> None:
    settings_path = tmp_path / "settings.json"
    quoted_cmd = (
        '"C:\\Program Files\\HoneyHive\\honeyhive-daemon.exe" ingest claude-hook'
    )
    settings_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": quoted_cmd,
                                }
                            ]
                        }
                    ]
                }
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    changed = install_claude_hooks(settings_path, quoted_cmd)
    changed_again = install_claude_hooks(settings_path, quoted_cmd)

    data = json.loads(settings_path.read_text(encoding="utf-8"))
    commands = [
        hook["command"]
        for entry in data["hooks"]["SessionStart"]
        for hook in entry["hooks"]
        if hook.get("type") == "command"
    ]
    assert changed is True
    assert changed_again is False
    assert commands.count(quoted_cmd) == 1


def test_install_claude_hooks_replaces_legacy_bare_command(tmp_path: Path) -> None:
    """Re-install removes bare-path hooks and leaves a single absolute hook."""
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "SessionStart": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "honeyhive-daemon ingest claude-hook",
                                }
                            ]
                        }
                    ]
                }
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    install_claude_hooks(
        settings_path, "/opt/venv/bin/honeyhive-daemon ingest claude-hook"
    )

    data = json.loads(settings_path.read_text(encoding="utf-8"))
    commands = [
        hook["command"]
        for entry in data["hooks"]["SessionStart"]
        for hook in entry["hooks"]
        if hook.get("type") == "command"
    ]
    assert commands == ["/opt/venv/bin/honeyhive-daemon ingest claude-hook"]


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
    from honeyhive_daemon.claude_hooks import _session_name_cache

    # Clear the manual dict cache so our tmp file is read fresh
    _session_name_cache.clear()

    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "custom-title",
                "customTitle": "release-focus-watcher",
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
    assert event["metadata"]["session_name"] == "release-focus-watcher"


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
    from honeyhive_daemon.claude_hooks import _session_name_cache

    _session_name_cache.clear()

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


def test_normalize_claude_stop_event_is_model() -> None:
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
    assert event["event_type"] == "chain"
    assert event["parent_id"] == "sess-prompt-1"
    # chat_history is injected at ingest time from session state, not at normalize time
    assert "chat_history" not in event.get("inputs", {})
    assert event["outputs"]["role"] == "user"
    assert event["outputs"]["content"] == "show me the failing tests"


def test_normalize_instructions_loaded_reads_file_content(tmp_path: Path) -> None:
    instr = tmp_path / "CLAUDE.md"
    instr.write_text("# Test\n\nhh-daemon-claude-md-loaded\n", encoding="utf-8")

    event = normalize_claude_payload(
        {
            "hook_event_name": "InstructionsLoaded",
            "session_id": "sess-instr-1",
            "cwd": str(tmp_path),
            "file_path": str(instr),
            "memory_type": "Project",
            "load_reason": "session_start",
        }
    )

    assert event is not None
    assert event["event_name"] == "chain.instructions.loaded"
    assert event["metadata"]["file.path"] == str(instr)
    assert event["metadata"]["instructions.memory_type"] == "Project"
    assert event["outputs"]["path"] == str(instr)
    assert event["outputs"]["basename"] == "CLAUDE.md"
    assert "hh-daemon-claude-md-loaded" in event["outputs"]["content"]


def test_record_session_activity_recovers_malformed_index(
    monkeypatch, tmp_path: Path
) -> None:
    from honeyhive_daemon.config import ensure_state_layout, get_sessions_path

    monkeypatch.setenv("HH_DAEMON_HOME", str(tmp_path / "daemon-home"))
    ensure_state_layout()
    get_sessions_path().write_text("{", encoding="utf-8")

    session = record_session_activity(
        "sess-lock-recover",
        transcript_path=None,
        last_activity_ms=123,
        session_start_exported=True,
    )

    assert session["session_id"] == "sess-lock-recover"
    assert load_session_index()["sess-lock-recover"]["session_start_exported"] is True


def test_ingest_instructions_loaded_does_not_synthesize_session_start(
    monkeypatch, tmp_path: Path
) -> None:
    from honeyhive_daemon.config import save_config

    monkeypatch.setenv("HH_DAEMON_HOME", str(tmp_path / "daemon-home"))
    captured: list[dict] = []

    class FakeEventsAPI:
        def create_event(self, request) -> None:  # type: ignore[no-untyped-def]
            captured.append(_nested_event_dict(request))

    class FakeHoneyHive:
        def __init__(self, api_key: str, base_url: str) -> None:
            self.events = FakeEventsAPI()

    monkeypatch.setattr("honeyhive_daemon.exporter.HoneyHive", FakeHoneyHive)
    monkeypatch.setattr(
        "honeyhive_daemon.main.resolve_config",
        lambda **kw: kw.get("cli_defaults"),
    )
    save_config(
        DaemonConfig(
            api_key="hh_test",
            base_url="https://api.honeyhive.ai",
        )
    )

    instr = tmp_path / "CLAUDE.md"
    instr.write_text("# Test\n", encoding="utf-8")
    payload = json.dumps(
        {
            "hook_event_name": "InstructionsLoaded",
            "session_id": "sess-instr-no-start",
            "cwd": str(tmp_path),
            "file_path": str(instr),
            "memory_type": "Project",
            "load_reason": "session_start",
        }
    )

    result = CliRunner().invoke(cli, ["ingest", "claude-hook"], input=payload)
    assert result.exit_code == 0, result.output
    assert [event["event_name"] for event in captured] == [
        "chain.instructions.loaded"
    ]


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
    assert "Pending spool events: 0" in result.output


def test_cli_status_shows_configured_base_url(
    monkeypatch, tmp_path: Path
) -> None:
    from honeyhive_daemon.config import save_config

    monkeypatch.setenv("HH_DAEMON_HOME", str(tmp_path / "daemon-home"))
    base_url = "https://api.honeyhive.ai"
    save_config(
        DaemonConfig(
            api_key="hh_test",
            base_url=base_url,
        )
    )

    result = CliRunner().invoke(cli, ["status"])
    assert result.exit_code == 0
    assert f"Base URL: {base_url}" in result.output.splitlines()


def test_cli_status_shows_spool_failure_reason(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HH_DAEMON_HOME", str(tmp_path / "daemon-home"))
    append_spool_event(
        {
            "event_name": "session.start",
            "spool_reason": "Connection refused: honeyhive events endpoint",
        }
    )

    result = CliRunner().invoke(cli, ["status"])
    assert result.exit_code == 0
    assert "Connection refused" in result.output


def test_append_chat_history_includes_current_message(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HH_DAEMON_HOME", str(tmp_path / "daemon-home"))

    first = append_chat_history("sess-chat", "user", "first")
    second = append_chat_history("sess-chat", "assistant", "reply")
    third = append_chat_history("sess-chat", "user", "second")

    assert first[-1] == {"role": "user", "content": "first"}
    assert len(second) == 2
    assert third[-1] == {"role": "user", "content": "second"}
    assert len(third) == 3


def test_ingest_turn_chat_history_includes_current_message(
    monkeypatch, tmp_path: Path
) -> None:
    from honeyhive_daemon.config import save_config

    monkeypatch.setenv("HH_DAEMON_HOME", str(tmp_path / "daemon-home"))
    captured: list[dict] = []

    class FakeEventsAPI:
        def create_event(self, request) -> None:  # type: ignore[no-untyped-def]
            captured.append(_nested_event_dict(request))

    class FakeHoneyHive:
        def __init__(self, api_key: str, base_url: str) -> None:
            self.events = FakeEventsAPI()

    monkeypatch.setattr("honeyhive_daemon.exporter.HoneyHive", FakeHoneyHive)
    monkeypatch.setattr(
        "honeyhive_daemon.main.resolve_config",
        lambda **kw: kw.get("cli_defaults"),
    )
    save_config(
        DaemonConfig(
            api_key="hh_test",
            base_url="https://api.honeyhive.ai",
        )
    )

    user_payload = json.dumps(
        {
            "hook_event_name": "UserPromptSubmit",
            "session_id": "sess-chat-dedupe",
            "prompt": "hello",
        }
    )
    agent_payload = json.dumps(
        {
            "hook_event_name": "Stop",
            "session_id": "sess-chat-dedupe",
            "last_assistant_message": "world",
        }
    )

    runner = CliRunner()
    assert runner.invoke(cli, ["ingest", "claude-hook"], input=user_payload).exit_code == 0
    assert runner.invoke(cli, ["ingest", "claude-hook"], input=agent_payload).exit_code == 0

    turn_events = [e for e in captured if e.get("event_name", "").startswith("turn.")]
    assert len(turn_events) == 2

    user_event = next(e for e in turn_events if e["event_name"] == "turn.user")
    assert user_event["inputs"]["chat_history"] == [
        {"role": "user", "content": "hello"}
    ]
    assert user_event["outputs"]["content"] == "hello"

    agent_event = next(e for e in turn_events if e["event_name"] == "turn.agent")
    assert agent_event["inputs"]["chat_history"] == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "world"},
    ]
    assert agent_event["outputs"]["content"] == "world"


def test_ingest_tool_usage_attached_once_per_api_request(
    monkeypatch, tmp_path: Path
) -> None:
    from honeyhive_daemon.config import save_config
    from honeyhive_daemon.transcript import TranscriptContext

    monkeypatch.setenv("HH_DAEMON_HOME", str(tmp_path / "daemon-home"))
    captured: list[dict] = []

    class FakeEventsAPI:
        def create_event(self, request) -> None:  # type: ignore[no-untyped-def]
            captured.append(_nested_event_dict(request))

    class FakeHoneyHive:
        def __init__(self, api_key: str, base_url: str) -> None:
            self.events = FakeEventsAPI()

    def fake_get_context(transcript_path: str, tool_use_id: str) -> TranscriptContext:
        ctx = TranscriptContext()
        ctx.request_id = "req-shared"
        ctx.usage = {"input_tokens": 2739, "output_tokens": 238}
        ctx.model = "claude-opus-4-8"
        return ctx

    monkeypatch.setattr("honeyhive_daemon.exporter.HoneyHive", FakeHoneyHive)
    monkeypatch.setattr(
        "honeyhive_daemon.main.get_context_for_tool_use", fake_get_context
    )
    monkeypatch.setattr(
        "honeyhive_daemon.main.resolve_config",
        lambda **kw: kw.get("cli_defaults"),
    )
    save_config(
        DaemonConfig(
            api_key="hh_test",
            base_url="https://api.honeyhive.ai",
        )
    )

    runner = CliRunner()
    for tool_name, tool_use_id in (
        ("Read", "toolu_read"),
        ("Bash", "toolu_bash"),
    ):
        payload = json.dumps(
            {
                "hook_event_name": "PostToolUse",
                "session_id": "sess-tool-tokens",
                "tool_name": tool_name,
                "tool_use_id": tool_use_id,
                "tool_input": {"command": "echo hi"},
                "tool_response": {"stdout": "hi"},
                "transcript_path": str(tmp_path / "transcript.jsonl"),
            }
        )
        result = runner.invoke(cli, ["ingest", "claude-hook"], input=payload)
        assert result.exit_code == 0, result.output

    tool_events = [e for e in captured if e.get("event_type") == "tool"]
    assert len(tool_events) == 2
    with_tokens = [
        e for e in tool_events if e.get("metadata", {}).get("prompt_tokens") is not None
    ]
    assert len(with_tokens) == 1
    assert with_tokens[0]["metadata"]["prompt_tokens"] == 2739


def test_merge_tool_events_uses_reported_duration_and_keeps_wall_time() -> None:
    pre_event = {
        "event_id": "tool-event-1",
        "session_id": "sess-tools",
        "event_type": "tool",
        "event_name": "tool.Read",
        "start_time": 1000,
        "end_time": 1000,
        "inputs": {"tool_input": {"file_path": "README.md"}},
        "metadata": {"tool.name": "Read"},
        "raw": {"hook_event_name": "PreToolUse"},
    }
    post_event = {
        "event_id": "post-event",
        "session_id": "sess-tools",
        "event_type": "tool",
        "event_name": "tool.Read",
        "start_time": 2500,
        "end_time": 2500,
        "inputs": {"tool_input": {"file_path": "README.md"}},
        "outputs": {"tool_response": {"type": "text"}},
        "metadata": {"tool.status": "success"},
        "raw": {"hook_event_name": "PostToolUse", "duration_ms": 25},
    }

    merged = _merge_tool_events(pre_event, post_event)

    assert merged["event_id"] == "tool-event-1"
    assert merged["duration"] == 25
    assert merged["end_time"] == 2500
    assert merged["start_time"] == 2475
    assert merged["metadata"]["tool.wall_duration_ms"] == 1500
    assert merged["metadata"]["tool.reported_duration_ms"] == 25


def test_session_token_metadata_aliases_coding_agent_metrics() -> None:
    metadata = _session_token_metadata(
        {
            "coding_agent.total_input_tokens": 123.0,
            "coding_agent.total_output_tokens": 45.0,
            "coding_agent.total_tokens": 168.0,
            "coding_agent.total_cache_read_tokens": 1000.0,
            "coding_agent.total_cache_creation_tokens": 50.0,
        }
    )

    assert metadata == {
        "prompt_tokens": 123.0,
        "completion_tokens": 45.0,
        "total_tokens": 168.0,
    }


def test_ingest_skills_listed_once_per_session(
    monkeypatch, tmp_path: Path
) -> None:
    from honeyhive_daemon.config import save_config

    monkeypatch.setenv("HH_DAEMON_HOME", str(tmp_path / "daemon-home"))
    captured: list[dict] = []

    class FakeEventsAPI:
        def create_event(self, request) -> None:  # type: ignore[no-untyped-def]
            captured.append(_nested_event_dict(request))

    class FakeHoneyHive:
        def __init__(self, api_key: str, base_url: str) -> None:
            self.events = FakeEventsAPI()

    monkeypatch.setattr("honeyhive_daemon.exporter.HoneyHive", FakeHoneyHive)
    monkeypatch.setattr(
        "honeyhive_daemon.main.resolve_config",
        lambda **kw: kw.get("cli_defaults"),
    )
    save_config(
        DaemonConfig(
            api_key="hh_test",
            base_url="https://api.honeyhive.ai",
        )
    )

    repo = tmp_path / "repo"
    repo.mkdir()

    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "attachment",
                "attachment": {
                    "type": "skill_listing",
                    "names": ["plugin-skill", "hh-daemon-smoke"],
                    "skillCount": 2,
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    runner = CliRunner()
    for tool_name, tool_use_id in (
        ("Read", "toolu_read"),
        ("Bash", "toolu_bash"),
    ):
        payload = json.dumps(
            {
                "hook_event_name": "PostToolUse",
                "session_id": "sess-skills",
                "cwd": str(repo),
                "tool_name": tool_name,
                "tool_use_id": tool_use_id,
                "tool_input": {"command": "echo hi"},
                "tool_response": {"stdout": "hi"},
                "transcript_path": str(transcript),
            }
        )
        result = runner.invoke(cli, ["ingest", "claude-hook"], input=payload)
        assert result.exit_code == 0, result.output

    skills_events = [
        e for e in captured if e.get("event_name") == "chain.skills.listed"
    ]
    assert len(skills_events) == 1
    outputs = skills_events[0]["outputs"]
    assert outputs["names"] == ["plugin-skill", "hh-daemon-smoke"]
    assert outputs["count"] == 2


def test_failed_skills_listed_export_is_spooled_once(
    monkeypatch, tmp_path: Path
) -> None:
    from honeyhive_daemon.config import save_config

    monkeypatch.setenv("HH_DAEMON_HOME", str(tmp_path / "daemon-home"))
    skills_export_attempts: list[dict] = []
    captured: list[dict] = []

    class FakeEventsAPI:
        def create_event(self, request) -> None:  # type: ignore[no-untyped-def]
            event = _nested_event_dict(request)
            if event["event_name"] == "chain.skills.listed":
                skills_export_attempts.append(event)
                raise RuntimeError("export unavailable")
            captured.append(event)

    class FakeHoneyHive:
        def __init__(self, api_key: str, base_url: str) -> None:
            self.events = FakeEventsAPI()

    monkeypatch.setattr("honeyhive_daemon.exporter.HoneyHive", FakeHoneyHive)
    monkeypatch.setattr(
        "honeyhive_daemon.main.resolve_config",
        lambda **kw: kw.get("cli_defaults"),
    )
    save_config(
        DaemonConfig(
            api_key="hh_test",
            base_url="https://api.honeyhive.ai",
        )
    )

    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text(
        json.dumps(
            {
                "type": "attachment",
                "attachment": {
                    "type": "skill_listing",
                    "names": ["hh-daemon-smoke"],
                    "skillCount": 1,
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    runner = CliRunner()
    for tool_use_id in ("toolu_read", "toolu_bash"):
        payload = json.dumps(
            {
                "hook_event_name": "PostToolUse",
                "session_id": "sess-skills-fail",
                "tool_name": "Read",
                "tool_use_id": tool_use_id,
                "tool_input": {"file_path": "README.md"},
                "tool_response": {"stdout": "ok"},
                "transcript_path": str(transcript),
            }
        )
        result = runner.invoke(cli, ["ingest", "claude-hook"], input=payload)
        assert result.exit_code == 0, result.output

    assert len(skills_export_attempts) == 1
    skills_spool = [
        event
        for event in read_spool_events()
        if event.get("event_name") == "chain.skills.listed"
    ]
    assert len(skills_spool) == 1
    tool_events = [event for event in captured if event["event_type"] == "tool"]
    assert [event["event_name"] for event in tool_events] == ["tool.Read", "tool.Read"]


def test_ingest_session_end_logs_session_id(
    monkeypatch, tmp_path: Path
) -> None:
    from honeyhive_daemon.config import save_config

    monkeypatch.setenv("HH_DAEMON_HOME", str(tmp_path / "daemon-home"))
    log_messages: list[str] = []

    exported_events: list[dict] = []

    class FakeEventsAPI:
        def create_event(self, request) -> None:  # type: ignore[no-untyped-def]
            exported_events.append(_nested_event_dict(request))

    class FakeHoneyHive:
        def __init__(self, api_key: str, base_url: str) -> None:
            self.events = FakeEventsAPI()

    monkeypatch.setattr("honeyhive_daemon.exporter.HoneyHive", FakeHoneyHive)
    monkeypatch.setattr(
        "honeyhive_daemon.exporter.log_message",
        lambda message: log_messages.append(message),
    )
    monkeypatch.setattr(
        "honeyhive_daemon.main.resolve_config",
        lambda **kw: kw.get("cli_defaults"),
    )
    config = DaemonConfig(
        api_key="hh_test",
        base_url="https://api.honeyhive.ai",
    )
    save_config(config)

    payload = json.dumps(
        {
            "hook_event_name": "SessionEnd",
            "session_id": "sess-log-test",
            "event": {"event_id": "evt-log-test"},
        }
    )
    result = CliRunner().invoke(cli, ["ingest", "claude-hook"], input=payload)
    assert result.exit_code == 0, result.output
    assert exported_events == []

    _flush_spool(config)

    assert any(e["event_name"] == "session.end" for e in exported_events)
    assert any(
        "session ended" in msg and "sess-log-test" in msg for msg in log_messages
    )


def _nested_event_dict(request) -> dict:  # type: ignore[no-untyped-def]
    event = request.event
    return event.model_dump() if hasattr(event, "model_dump") else event


def test_export_session_event_includes_session_name(monkeypatch, tmp_path: Path) -> None:
    """session_name is promoted to a top-level field on session events."""
    captured = {}
    monkeypatch.setenv("HH_DAEMON_HOME", str(tmp_path / "daemon-home"))

    class FakeEventsAPI:
        def create_event(self, request) -> None:  # type: ignore[no-untyped-def]
            captured["event"] = _nested_event_dict(request)

    class FakeHoneyHive:
        def __init__(self, api_key: str, base_url: str) -> None:
            self.events = FakeEventsAPI()

    monkeypatch.setattr("honeyhive_daemon.exporter.HoneyHive", FakeHoneyHive)

    config = DaemonConfig(
        api_key="hh_test",
        base_url="https://api.honeyhive.ai",
    )
    event = {
        "event_id": "sess-1",
        "session_id": "sess-1",
        "event_type": "session",
        "event_name": "session.start",
        "start_time": 1000,
        "end_time": 1000,
        "duration": 0,
        "metadata": {"session_name": "release-focus-watcher"},
        "inputs": {},
        "outputs": {},
    }

    export_event(config, event)

    assert captured["event"]["session_name"] == "release-focus-watcher"
    # Also preserved in metadata
    assert captured["event"]["metadata"]["session_name"] == "release-focus-watcher"


def test_export_tool_event_no_session_name_field(monkeypatch, tmp_path: Path) -> None:
    """session_name is NOT promoted to top-level on non-session events."""
    captured = {}
    monkeypatch.setenv("HH_DAEMON_HOME", str(tmp_path / "daemon-home"))

    class FakeEventsAPI:
        def create_event(self, request) -> None:  # type: ignore[no-untyped-def]
            captured["event"] = _nested_event_dict(request)

    class FakeHoneyHive:
        def __init__(self, api_key: str, base_url: str) -> None:
            self.events = FakeEventsAPI()

    monkeypatch.setattr("honeyhive_daemon.exporter.HoneyHive", FakeHoneyHive)

    config = DaemonConfig(
        api_key="hh_test",
        base_url="https://api.honeyhive.ai",
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
        "metadata": {"session_name": "release-focus-watcher"},
        "inputs": {},
        "outputs": {},
    }

    export_event(config, event)

    assert "session_name" not in captured["event"]
    # But still in metadata for queryability
    assert captured["event"]["metadata"]["session_name"] == "release-focus-watcher"


def test_export_event_posts_honeyhive_event(monkeypatch, tmp_path: Path) -> None:
    captured = {}
    monkeypatch.setenv("HH_DAEMON_HOME", str(tmp_path / "daemon-home"))

    class FakeEventsAPI:
        def create_event(self, request) -> None:  # type: ignore[no-untyped-def]
            captured["event"] = _nested_event_dict(request)

    class FakeHoneyHive:
        def __init__(self, api_key: str, base_url: str) -> None:
            captured["api_key"] = api_key
            captured["base_url"] = base_url
            self.events = FakeEventsAPI()

    monkeypatch.setattr("honeyhive_daemon.exporter.HoneyHive", FakeHoneyHive)

    config = DaemonConfig(
        api_key="hh_test",
        base_url="https://api.honeyhive.ai",
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
    assert not captured["event"].get("project")
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

    def fake_update_event(  # type: ignore[no-untyped-def]
        config, *, event_id, inputs=None, outputs=None, metadata=None, metrics=None
    ):
        metrics_captured.append(
            {"event_id": event_id, "metadata": metadata, "metrics": metrics}
        )

    monkeypatch.setattr(
        "honeyhive_daemon.exporter.update_event",
        fake_update_event,
    )

    # Accumulate chat history so the session.end update includes the final chat.
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
    )
    _push_pending_session_artifacts(config)

    assert [item["event_id"] for item in captured] == ["sess-root-1", "sess-end-1"]
    assert len(captured) == 2
    assert captured[0]["outputs"]["chat_history"] == [{"role": "user", "content": "hi"}]
    # End event gets full artifact transcript and final chat history.
    assert captured[1]["outputs"]["artifact"]["path"] == str(transcript_path)
    assert captured[1]["outputs"]["artifact"]["content"] == [
        {"type": "user", "message": "hi"}
    ]
    assert captured[1]["outputs"]["artifact"]["format"] == "json"
    assert captured[1]["outputs"]["artifact"]["reason"] == "session_end"
    assert captured[1]["outputs"]["chat_history"] == [{"role": "user", "content": "hi"}]
    # Metrics were attached to the root event
    assert len(metrics_captured) == 1
    assert metrics_captured[0]["event_id"] == "sess-root-1"
    assert "coding_agent.event_count" in metrics_captured[0]["metrics"]
    assert metrics_captured[0]["metadata"] is None


def test_push_pending_session_artifacts_retries_when_synthetic_session_end_fails(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HH_DAEMON_HOME", str(tmp_path / "daemon-home"))
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text(
        '{"type":"user","message":{"role":"user","content":"hi"}}\n',
        encoding="utf-8",
    )
    record_session_activity(
        "sess-synth-fail",
        transcript_path=str(transcript_path),
        last_activity_ms=1000,
        session_start_exported=True,
    )

    def fail_export(_config, _event):  # type: ignore[no-untyped-def]
        raise RuntimeError("export unavailable")

    monkeypatch.setattr("honeyhive_daemon.main.export_event", fail_export)
    idle_ms = 24 * 60 * 60 * 1000
    monkeypatch.setattr(
        "honeyhive_daemon.main._now_ms",
        lambda: 1000 + idle_ms + 1,
    )

    config = DaemonConfig(
        api_key="hh_test",
        base_url="https://api.honeyhive.ai",
    )
    _push_pending_session_artifacts(config)

    index = load_session_index()
    assert index["sess-synth-fail"].get("artifact_pushed") is not True
    assert index["sess-synth-fail"].get("artifact_retry_count", 0) >= 1


def test_push_pending_session_artifacts_synthesizes_orphan_session_end(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HH_DAEMON_HOME", str(tmp_path / "daemon-home"))
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text(
        '{"type":"user","message":{"role":"user","content":"hi"}}\n',
        encoding="utf-8",
    )
    record_session_activity(
        "sess-orphan",
        transcript_path=str(transcript_path),
        last_activity_ms=1000,
        session_start_exported=True,
    )

    exported_events: list[dict] = []
    monkeypatch.setattr(
        "honeyhive_daemon.main.export_event",
        lambda _config, event: exported_events.append(event),
    )
    monkeypatch.setattr(
        "honeyhive_daemon.exporter.update_event_outputs",
        lambda *a, **kw: None,
    )
    monkeypatch.setattr(
        "honeyhive_daemon.exporter.update_event",
        lambda *a, **kw: None,
    )
    idle_ms = 24 * 60 * 60 * 1000
    monkeypatch.setattr(
        "honeyhive_daemon.main._now_ms",
        lambda: 1000 + idle_ms + 1,
    )

    config = DaemonConfig(
        api_key="hh_test",
        base_url="https://api.honeyhive.ai",
    )
    _push_pending_session_artifacts(config)

    session_end_events = [
        e for e in exported_events if e.get("event_name") == "session.end"
    ]
    assert len(session_end_events) == 1
    assert session_end_events[0]["session_id"] == "sess-orphan"
    assert session_end_events[0]["metadata"].get("synthetic") is True


def test_push_pending_session_artifacts_stops_after_max_retries(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("HH_DAEMON_HOME", str(tmp_path / "daemon-home"))
    monkeypatch.setattr("honeyhive_daemon.main._now_ms", lambda: 2000)

    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text(
        '{"type":"user","message":{"role":"user","content":"hi"}}\n',
        encoding="utf-8",
    )
    record_session_activity(
        "sess-retry-cap",
        transcript_path=str(transcript_path),
        last_activity_ms=1000,
        ended=True,
        session_end_event_id="sess-end-cap",
    )

    def fail_update_outputs(_config, *, event_id, outputs):  # type: ignore[no-untyped-def]
        raise RuntimeError("400 Bad Request")

    monkeypatch.setattr(
        "honeyhive_daemon.exporter.update_event_outputs",
        fail_update_outputs,
    )
    monkeypatch.setattr(
        "honeyhive_daemon.exporter.update_event",
        lambda *a, **kw: None,
    )

    config = DaemonConfig(
        api_key="hh_test",
        base_url="https://api.honeyhive.ai",
    )
    for _ in range(4):
        _push_pending_session_artifacts(config)

    index = load_session_index()
    assert index["sess-retry-cap"]["artifact_pushed"] is True
    assert index["sess-retry-cap"].get("artifact_retry_count", 0) >= 3


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
