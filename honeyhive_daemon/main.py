"""CLI entrypoint for the minimal HoneyHive Claude Code daemon."""

from __future__ import annotations

import json
import os
import signal
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import click

from .claude_hooks import (
    _is_daemon_hook_command,
    get_hook_command,
    install_claude_hooks,
    normalize_claude_payload,
)
from .transcript import (
    TranscriptContext,
    get_context_for_latest_turn,
    get_context_for_tool_use,
)
from .config import (
    DEFAULT_BASE_URL,
    DaemonConfig,
    _get_user_config_path,
    get_claude_settings_path,
    get_daemon_home,
    get_pid_path,
    load_config,
    load_user_config,
    resolve_config,
    resolve_config_for_cwd,
    save_config,
    save_user_config,
)
from .ci import analyze_cmd, add_to_ci_cmd
from .evaluators import push_evaluators_cmd
from .exporter import export_event, export_events
from .filters import (
    FilterVerdict,
    apply_filters,
    filter_transcript_content,
    load_filters,
    redact_event,
    save_default_filters,
)
from .git_hooks import (
    find_git_root,
    get_commit_link_payload,
    install_post_commit_hook,
)
from .metrics import (
    compute_session_metrics as _compute_session_metrics,
    read_transcript_jsonl as _read_transcript_jsonl,
)
from .state import (
    append_chat_history,
    append_spool_event,
    claim_tool_usage_request_id,
    drain_spool_events,
    get_chat_history,
    buffer_pending_tool_event,
    get_expired_tool_events,
    get_sessions_needing_artifact,
    increment_session_artifact_retry,
    log_message,
    mark_session_artifact_pushed,
    pop_pending_tool_event,
    read_spool_events,
    record_session_activity,
)


SESSION_IDLE_THRESHOLD_MS = 24 * 60 * 60 * 1000
ARTIFACT_MAX_RETRIES = 3


@click.group()
def cli() -> None:
    """HoneyHive daemon for Claude Code telemetry."""


@cli.command()
@click.option(
    "--key",
    "api_key",
    envvar="HH_API_KEY",
    required=False,
    default=None,
    help="HoneyHive API key (deprecated — use 'honeyhive-daemon init').",
)
@click.option(
    "--url",
    "base_url",
    envvar="HH_API_URL",
    default=DEFAULT_BASE_URL,
    show_default=True,
    help="HoneyHive base URL or OTLP traces endpoint.",
)
@click.option(
    "--repo",
    type=click.Path(file_okay=False, path_type=Path),
    help="Repo to attach git commit events to.",
)
@click.option("--ci", is_flag=True, help="Enable CI mode.")
@click.pass_context
def run(
    ctx: click.Context,
    api_key: Optional[str],
    base_url: str,
    repo: Optional[Path],
    ci: bool,
) -> None:
    """Install Claude hooks, persist config, and keep retrying queued events."""
    repo_root = _resolve_repo(repo)

    # --- Detect explicit CLI flags vs env-var / default sourcing -----------
    # Click resolves envvar-backed options transparently, so we check
    # whether the value actually came from the command line by inspecting
    # the original source.  ``ctx.get_parameter_source`` returns
    # ``ParameterSource.COMMANDLINE`` only when the user typed the flag.
    key_from_cli = (
        ctx.get_parameter_source("api_key") == click.core.ParameterSource.COMMANDLINE
    )
    if key_from_cli:
        click.echo(
            "Warning: --key is deprecated. "
            "Use 'honeyhive-daemon init' to set up per-project config."
        )
    # --- Migrate CLI-provided key to user-level config --------------------
    if api_key and key_from_cli:
        user_data: dict = {"api_key_env": "HH_API_KEY"}
        # If HH_API_KEY is not set, store the raw key as a last-resort fallback
        if not os.getenv("HH_API_KEY"):
            user_data["api_key"] = api_key
        save_user_config(user_data)
        click.echo(
            f"Saved API key config to {_get_user_config_path()} "
            "(future runs won't need --key)."
        )

    # --- Fallback chain for api_key ---------------------------------------
    # Priority:
    #   1. --key / $HH_API_KEY (if provided)
    #   2. ~/.honeyhive/config.json (user-level)
    #   3. ~/.honeyhive/daemon/state/config.json (legacy)
    #   4. Error with helpful message
    if not api_key:
        # Try user-level config
        user_cfg = load_user_config()
        api_key = user_cfg.get("api_key")  # raw key fallback
        if not api_key:
            api_key_env = user_cfg.get("api_key_env")
            if api_key_env:
                api_key = os.getenv(api_key_env)

    if not api_key:
        # Try legacy daemon config
        legacy = load_config()
        if legacy and legacy.api_key:
            api_key = legacy.api_key

    if not api_key:
        click.echo(
            "Error: No API key found.\n\n"
            "Provide one of:\n"
            "  1. Run 'honeyhive-daemon init' in your project directory\n"
            "  2. Set the HH_API_KEY environment variable\n"
            "  3. Pass --key (deprecated)"
        )
        raise SystemExit(1)

    config = DaemonConfig(
        api_key=api_key,
        base_url=base_url,
        repo_path=str(repo_root) if repo_root else None,
        ci=ci,
    )
    save_config(config)
    filters_path = save_default_filters()

    hook_command = get_hook_command()
    settings_path = get_claude_settings_path()
    hooks_changed = install_claude_hooks(settings_path, hook_command)
    git_changed = False
    if repo_root is not None:
        git_changed = install_post_commit_hook(
            repo_root, "honeyhive-daemon ingest git-post-commit"
        )

    log_message(
        "daemon started "
        f"repo={repo_root or '-'} "
        f"ci={ci}"
    )
    _flush_spool(config)

    click.echo(f"Daemon home: {get_daemon_home()}")
    click.echo(f"Filters: {filters_path}")
    click.echo(f"Claude settings: {settings_path}")
    if repo_root is not None:
        click.echo(f"Repo: {repo_root}")
    click.echo(f"Claude hooks {'updated' if hooks_changed else 'already installed'}")
    if repo_root is not None:
        click.echo(
            f"Git post-commit hook {'updated' if git_changed else 'already installed'}"
        )
    pid_path = get_pid_path()
    if pid_path.exists():
        try:
            existing_pid = int(pid_path.read_text(encoding="utf-8").strip())
            os.kill(existing_pid, 0)  # check if process is alive
            click.echo(
                f"Daemon is already running (PID {existing_pid}). "
                "Run 'honeyhive-daemon stop' first."
            )
            raise SystemExit(1)
        except (ProcessLookupError, PermissionError):
            pid_path.unlink(missing_ok=True)  # stale PID file, clean it up
        except ValueError:
            pid_path.unlink(missing_ok=True)  # corrupt PID file, clean it up

    pid_path.write_text(str(os.getpid()), encoding="utf-8")
    click.echo(f"PID: {os.getpid()} (written to {pid_path})")
    click.echo("HoneyHive daemon is running. Press Ctrl-C to stop.")

    def _handle_sigterm(signum: int, frame: object) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _handle_sigterm)

    try:
        while True:
            time.sleep(5)
            _flush_spool(config)
            _flush_expired_tool_events(config)
            _push_pending_session_artifacts(config)
    except KeyboardInterrupt:
        click.echo("\nStopping HoneyHive daemon.")
    finally:
        if pid_path.exists():
            pid_path.unlink()


@cli.command()
@click.option("--api-key-env", default="HH_API_KEY", help="Env var holding the API key")
@click.option("--url", default=None, help="HoneyHive API base URL (for self-hosted / non-default endpoints)")
def init(api_key_env: str, url: str | None) -> None:
    """Initialize .honeyhive/ config in the current directory."""
    cwd = Path.cwd()
    hh_dir = cwd / ".honeyhive"

    if hh_dir.exists():
        click.echo(f"Warning: {hh_dir} already exists — updating files.")

    hh_dir.mkdir(parents=True, exist_ok=True)

    # Marker file (committed); secrets live in config.local.json
    (hh_dir / "config.json").write_text("{}\n", encoding="utf-8")

    # Write local config (not committed)
    local_config: dict[str, str] = {"api_key_env": api_key_env}
    if url:
        local_config["base_url"] = url
    local_config_path = hh_dir / "config.local.json"
    local_config_path.write_text(
        json.dumps(local_config, indent=2) + "\n",
        encoding="utf-8",
    )

    # Auto-append to .gitignore
    gitignore_path = cwd / ".gitignore"
    local_pattern = ".honeyhive/config.local.json"
    if gitignore_path.exists():
        existing = gitignore_path.read_text(encoding="utf-8")
        lines = existing.splitlines()
        if local_pattern not in lines:
            # Ensure trailing newline before appending
            if existing and not existing.endswith("\n"):
                existing += "\n"
            gitignore_path.write_text(
                existing + local_pattern + "\n",
                encoding="utf-8",
            )
    else:
        gitignore_path.write_text(local_pattern + "\n", encoding="utf-8")

    click.echo(f"Created {hh_dir}/")
    click.echo("  config.json        → (empty; project is resolved from your API key)")
    click.echo(f"  config.local.json  → api_key_env: {api_key_env}")
    click.echo(f"Updated {gitignore_path}")


@cli.command()
def status() -> None:
    """Show daemon status."""
    config = load_config()
    spool_events = read_spool_events()
    pending = len(spool_events)
    click.echo(f"Daemon home: {get_daemon_home()}")
    click.echo(f"Configured: {'yes' if config else 'no'}")
    click.echo(f"Pending spool events: {pending}")
    if pending > 0:
        reasons: dict[str, int] = {}
        for evt in spool_events:
            reason = evt.get("spool_reason", "unknown")
            reasons[reason] = reasons.get(reason, 0) + 1
        for reason, count in reasons.items():
            click.echo(f"  Spool reason ({count}x): {reason}")
    if config:
        click.echo(f"Base URL: {config.base_url}")
        click.echo(f"Repo: {config.repo_path or '-'}")


@cli.command()
def stop() -> None:
    """Stop a running background daemon."""
    pid_path = get_pid_path()
    if not pid_path.exists():
        click.echo("No daemon PID file found — daemon may not be running.")
        raise SystemExit(1)
    try:
        pid = int(pid_path.read_text(encoding="utf-8").strip())
    except ValueError:
        click.echo("PID file is corrupt.")
        raise SystemExit(1)
    try:
        os.kill(pid, signal.SIGTERM)
        click.echo(f"Sent SIGTERM to daemon (PID {pid}).")
    except ProcessLookupError:
        click.echo(f"No process with PID {pid} — removing stale PID file.")
        pid_path.unlink(missing_ok=True)
        raise SystemExit(1)
    except PermissionError:
        click.echo(f"Permission denied sending signal to PID {pid}.")
        raise SystemExit(1)


@cli.command()
def doctor() -> None:
    """Run a lightweight daemon self-check."""
    config = load_config()
    settings_path = get_claude_settings_path()
    click.echo(f"Config present: {'yes' if config else 'no'}")
    click.echo(f"Claude settings exists: {'yes' if settings_path.exists() else 'no'}")
    installed = "yes" if _settings_have_command(settings_path) else "no"
    click.echo(f"Claude hook command installed: {installed}")
    repo_root = _resolve_repo(None)
    click.echo(f"Git repo detected: {'yes' if repo_root else 'no'}")


cli.add_command(analyze_cmd)
cli.add_command(add_to_ci_cmd)
cli.add_command(push_evaluators_cmd)


@cli.group()
def ingest() -> None:
    """Internal commands used by Claude and git hooks."""


@ingest.command("claude-hook")
def ingest_claude_hook() -> None:
    """Receive one Claude hook event from stdin and export it."""
    config = load_config()
    if config is None:
        log_message("skipped claude hook because daemon config is missing")
        return

    raw_text = sys.stdin.read()
    if not raw_text.strip():
        log_message("skipped empty claude hook payload")
        return

    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError:
        log_message("skipped malformed claude hook payload")
        return

    hook_event_name = payload.get("hook_event_name", "unknown")
    tool_name = payload.get("tool_name", "-")
    session_id = payload.get("session_id", "-")
    log_message(
        "received claude hook "
        f"hook_event_name={hook_event_name} "
        f"tool_name={tool_name} "
        f"session_id={session_id}"
    )

    event = normalize_claude_payload(payload)
    if event is None:
        log_message(
            "ignored claude hook "
            f"hook_event_name={hook_event_name} "
            f"tool_name={tool_name}"
        )
        return

    # ── Apply output filters ─────────────────────────────────
    filters = load_filters()
    session_name = event.get("metadata", {}).get("session_name")
    verdict = apply_filters(event, filters, session_name=session_name)
    if not verdict.should_export:
        log_message(
            "filtered out claude event "
            f"event_name={event.get('event_name')} "
            f"reason={verdict.reason}"
        )
        return
    if verdict.should_redact:
        event = redact_event(event)
        log_message(
            "redacted claude event "
            f"event_name={event.get('event_name')} "
            f"reason={verdict.reason}"
        )

    # ── Route to correct project/key based on cwd ─────────────
    event_cwd = event.get("metadata", {}).get("cwd")
    cli_config = config  # preserve CLI-set defaults for hierarchical resolution
    config = resolve_config(
        cwd=event_cwd,
        session_name=session_name,
        cli_defaults=cli_config,
    )

    transcript_path = event.get("metadata", {}).get("transcript.path")
    is_session_start = event["event_name"] == "session.start"
    is_session_end = event["event_name"] == "session.end"

    if is_session_end:
        queued_event = dict(event)
        queued_event["spool_reason"] = "queued session.end for daemon flush"
        queued_event["_resolved_config"] = config.to_dict()
        append_spool_event(queued_event)
        record_session_activity(
            str(event["session_id"]),
            transcript_path=str(transcript_path) if transcript_path else None,
            last_activity_ms=int(event["end_time"]),
            ended=True,
            session_end_event_id=str(event["event_id"]),
            cwd=event_cwd,
            session_name=session_name,
        )
        log_message(
            "queued session.end for daemon flush "
            f"session_id={event['session_id']} "
            f"event_id={event['event_id']}"
        )
        return

    session_state = record_session_activity(
        str(event["session_id"]),
        transcript_path=str(transcript_path) if transcript_path else None,
        last_activity_ms=int(event["end_time"]),
        ended=False,
        session_end_event_id=None,
        session_start_exported=True if is_session_start else None,
        cwd=event_cwd,
        session_name=session_name,
    )

    # ── Synthesize session.start if daemon started after session ──
    # When the daemon starts mid-session, it misses the SessionStart hook.
    # Without a session.start event in HoneyHive, artifact updates fail
    # with 400 and all session data is lost. Create it now.
    should_synthesize_session_start = (
        not is_session_start
        and hook_event_name != "InstructionsLoaded"
        and not session_state.get("session_start_exported")
    )
    if should_synthesize_session_start:
        synthetic_session = {
            "event_id": str(event["session_id"]),  # session.start uses session_id as event_id
            "session_id": str(event["session_id"]),
            "event_type": "session",
            "event_name": "session.start",
            "start_time": int(event["start_time"]),
            "end_time": int(event["start_time"]),
            "duration": 1,
            "inputs": {},
            "outputs": {},
            "metadata": {
                k: v
                for k, v in event.get("metadata", {}).items()
                if k
                in (
                    "agent.provider",
                    "agent.product",
                    "capture.source",
                    "raw.format",
                    "agent.session_id",
                    "session_name",
                    "transcript.path",
                    "cwd",
                    "repo.path",
                    "git.revision",
                    "model.name",
                )
            },
        }
        synthetic_session["metadata"]["synthetic"] = True
        session_name = synthetic_session["metadata"].get("session_name")
        if session_name:
            synthetic_session["session_name"] = session_name
        try:
            export_event(config, synthetic_session)
            record_session_activity(
                str(event["session_id"]),
                transcript_path=str(transcript_path) if transcript_path else None,
                last_activity_ms=int(event["end_time"]),
                session_start_exported=True,
            )
            log_message(
                "synthesized session.start for mid-session daemon start "
                f"session_id={event['session_id']} "
                f"session_name={session_name or '(unknown)'}"
            )
        except Exception as exc:
            log_message(
                f"failed to synthesize session.start "
                f"session_id={event['session_id']}: {exc}"
            )

    # Pre+post tool event linking
    hook_phase = event.pop("_hook_phase", None)
    hook_failure = event.pop("_hook_failure", False)
    tool_use_id = event.get("tool_use_id")

    if hook_phase == "pre" and tool_use_id:
        buffer_pending_tool_event(str(event["session_id"]), tool_use_id, event)
        log_message(
            "buffered pre-phase tool event "
            f"tool_use_id={tool_use_id} "
            f"event_name={event['event_name']}"
        )
        return

    if hook_phase == "post" and tool_use_id:
        pre_event = pop_pending_tool_event(str(event["session_id"]), tool_use_id)
        if pre_event is not None:
            event = _merge_tool_events(pre_event, event, failure=hook_failure)
            log_message(
                "merged pre+post tool event "
                f"tool_use_id={tool_use_id} "
                f"event_name={event['event_name']} "
                f"duration={event.get('duration', 0)}ms"
            )

    # Enrich events with transcript context (thinking + usage/model metadata)
    if transcript_path:
        try:
            ctx: TranscriptContext | None = None
            if event.get("event_type") == "tool" and tool_use_id:
                ctx = get_context_for_tool_use(str(transcript_path), tool_use_id)
            elif event.get("event_type") == "model":
                ctx = get_context_for_latest_turn(str(transcript_path))
            if ctx is not None and ctx.has_data():
                include_usage = True
                if event.get("event_type") == "tool" and ctx.request_id:
                    include_usage = claim_tool_usage_request_id(
                        str(event["session_id"]), ctx.request_id
                    )
                _apply_transcript_context(event, ctx, include_usage=include_usage)
        except Exception:
            pass  # transcript enrichment is best-effort

    # Accumulate chat history for turn events.
    # inputs.chat_history includes the current turn so every turn is inspectable
    # without reconstructing its own outputs client-side.
    turn_role = event.get("metadata", {}).get("turn.role")
    if turn_role:
        content = event.get("outputs", {}).get("content")
        if content is not None:
            session_id = str(event["session_id"])
            event.setdefault("inputs", {})["chat_history"] = append_chat_history(
                session_id, turn_role, str(content)
            )

    try:
        export_event(config, event)
        # Success logs live in exporter.export_event (right after create_event)
        # so SessionEnd hooks killed on exit still leave an audit trail.
        # Artifact push is handled by the daemon's background loop
        # (every 5s) rather than inline here, to avoid hook timeouts.
    except Exception as exc:  # pragma: no cover
        event["spool_reason"] = str(exc)
        event["_resolved_config"] = config.to_dict()
        append_spool_event(event)
        log_message(f"spooled claude event {event['event_name']}: {exc}")


@ingest.command("git-post-commit")
@click.option(
    "--repo",
    type=click.Path(file_okay=False, path_type=Path),
    help="Repo to inspect.",
)
def ingest_git_post_commit(repo: Optional[Path]) -> None:
    """Emit a lightweight git commit-link event."""
    config = load_config()
    if config is None:
        log_message("skipped git post-commit because daemon config is missing")
        return

    repo_root = _resolve_repo(repo)
    if repo_root is None:
        log_message("skipped git post-commit because no repo was found")
        return

    payload = get_commit_link_payload(repo_root)
    if payload is None:
        return

    event = {
        "event_id": str(uuid.uuid4()),
        "session_id": str(uuid.uuid4()),
        "event_type": "chain",
        "event_name": "chain.commit_link",
        "start_time": _now_ms(),
        "end_time": _now_ms(),
        "duration": 0,
        "metadata": {
            "capture.source": "git_hook",
            "raw.format": "git_post_commit",
            "repo.path": payload["repo_path"],
            "git.commit_sha": payload["git.commit_sha"],
            "git.parent_sha": payload["git.parent_sha"],
        },
        "raw": payload,
    }

    try:
        export_event(config, event)
        log_message(
            "exported git commit-link "
            f"commit_sha={payload['git.commit_sha']} "
            f"repo={payload['repo_path']}"
        )
    except Exception as exc:  # pragma: no cover
        event["spool_reason"] = str(exc)
        append_spool_event(event)
        log_message(f"spooled git commit-link event: {exc}")


def _merge_tool_events(
    pre_event: dict, post_event: dict, *, failure: bool = False
) -> dict:
    """Merge a pre-phase and post-phase tool event into a single event."""
    merged = dict(post_event)
    merged["event_id"] = pre_event["event_id"]
    wall_duration_ms = max(0, int(post_event["end_time"]) - int(pre_event["start_time"]))
    reported_duration_ms = _tool_reported_duration_ms(post_event)
    duration_ms = reported_duration_ms if reported_duration_ms is not None else wall_duration_ms
    duration_ms = max(1, int(duration_ms))
    merged["duration"] = duration_ms
    merged["end_time"] = int(post_event["end_time"])
    merged["start_time"] = max(
        int(pre_event["start_time"]), int(merged["end_time"]) - duration_ms
    )

    # Merge inputs from pre, outputs from post
    merged["inputs"] = dict(pre_event.get("inputs", {}))
    merged["inputs"].update(post_event.get("inputs", {}))
    merged["outputs"] = dict(post_event.get("outputs", {}))

    # Merge metadata
    metadata = dict(pre_event.get("metadata", {}))
    metadata.update(post_event.get("metadata", {}))
    metadata["tool.phase"] = "complete"
    metadata["tool.status"] = "failure" if failure else "success"
    metadata["tool.wall_duration_ms"] = wall_duration_ms
    if reported_duration_ms is not None:
        metadata["tool.reported_duration_ms"] = reported_duration_ms
    merged["metadata"] = metadata

    # Propagate error from failed tool executions
    if failure:
        raw_post = post_event.get("raw") or {}
        error_msg = (
            raw_post.get("error")
            or post_event.get("outputs", {}).get("error")
            or post_event.get("inputs", {}).get("error")
        )
        if error_msg:
            merged["error"] = str(error_msg)

    # Store raw payloads from both phases
    merged["raw_pre"] = pre_event.get("raw")
    merged["raw_post"] = post_event.get("raw")
    merged.pop("raw", None)

    return merged


def _tool_reported_duration_ms(event: dict) -> Optional[int]:
    """Return Claude's reported tool runtime from the post-hook payload."""
    raw = event.get("raw") or {}
    value = raw.get("duration_ms")
    if value is None:
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def _flush_expired_tool_events(config: DaemonConfig) -> None:
    """Export any tool events that have been buffered too long (orphaned pre-events)."""
    expired = get_expired_tool_events(now_ms=_now_ms())
    for event in expired:
        event.get("metadata", {}).setdefault("tool.phase", "pre")
        event.pop("_hook_phase", None)
        event.pop("_hook_failure", None)
        # Resolve per-event config from cwd/session_name, fall back to global
        event_cwd = event.get("metadata", {}).get("cwd")
        event_session_name = event.get("metadata", {}).get("session_name")
        event_config = resolve_config(
            cwd=event_cwd,
            session_name=event_session_name,
            cli_defaults=config,
        ) if event_cwd or event_session_name else config
        try:
            export_event(event_config, event)
            log_message(
                "exported orphaned pre-phase tool event "
                f"event_name={event['event_name']}"
            )
        except Exception as exc:
            event["spool_reason"] = str(exc)
            event["_resolved_config"] = event_config.to_dict()
            append_spool_event(event)
            log_message(f"spooled orphaned tool event: {exc}")


def _apply_transcript_context(
    event: dict, ctx: TranscriptContext, *, include_usage: bool = True
) -> None:
    """Apply thinking, usage, and model metadata from transcript to an event."""
    if ctx.thinking:
        event.setdefault("inputs", {})["thinking"] = ctx.thinking
    metadata = event.setdefault("metadata", {})
    if ctx.model:
        metadata["model"] = ctx.model
    if ctx.request_id:
        metadata["request_id"] = ctx.request_id
    if include_usage and ctx.usage:
        for key, value in ctx.usage.items():
            metadata[f"usage.{key}"] = value
        # Alias to HoneyHive standard field names
        if "input_tokens" in ctx.usage:
            metadata["prompt_tokens"] = ctx.usage["input_tokens"]
        if "output_tokens" in ctx.usage:
            metadata["completion_tokens"] = ctx.usage["output_tokens"]


def _flush_spool(config: DaemonConfig) -> None:
    pending = drain_spool_events()
    if not pending:
        return
    log_message(f"flushing spool event_count={len(pending)}")
    failed: list = []
    for event in pending:
        # Use per-event resolved config if stamped, otherwise fall back to global
        stamped = event.pop("_resolved_config", None)
        event_config = DaemonConfig.from_dict(stamped) if stamped else config
        try:
            export_event(event_config, event)
        except Exception as exc:
            log_message(f"flush event failed: {exc}")
            event["spool_reason"] = str(exc)
            if stamped:
                event["_resolved_config"] = stamped
            failed.append(event)
    for event in failed:
        append_spool_event(event)
    flushed = len(pending) - len(failed)
    log_message(f"flush complete flushed={flushed} remaining={len(failed)}")


def _resolve_session_config(
    session: dict, cli_defaults: DaemonConfig
) -> DaemonConfig:
    """Resolve the hierarchical config for a tracked session."""
    cwd = session.get("cwd")
    name = session.get("session_name")
    if cwd or name:
        return resolve_config(cwd=cwd, session_name=name, cli_defaults=cli_defaults)
    return cli_defaults


def _push_pending_session_artifacts(
    config: DaemonConfig, session_ids: Optional[list[str]] = None
) -> None:
    from .exporter import update_event, update_event_outputs

    pending = get_sessions_needing_artifact(
        now_ms=_now_ms(),
        idle_threshold_ms=SESSION_IDLE_THRESHOLD_MS,
    )
    if session_ids is not None:
        allowed = set(session_ids)
        pending = [session for session in pending if session["session_id"] in allowed]
    for session in pending:
        transcript_path = session.get("transcript_path")
        if not transcript_path:
            continue

        session_config = _resolve_session_config(session, config)

        if not session.get("ended"):
            log_message(
                "skipped session artifact update "
                f"session_id={session['session_id']} "
                "because session has not ended"
            )
            continue

        transcript_content = _read_transcript_jsonl(transcript_path)
        if transcript_content is None:
            log_message(
                "skipped session artifact update "
                f"session_id={session['session_id']} "
                f"because transcript could not be read"
            )
            continue

        # Apply content filters to transcript before push
        artifact_filters = load_filters()
        original_count = len(transcript_content)
        transcript_content = filter_transcript_content(
            transcript_content, artifact_filters
        )
        if len(transcript_content) != original_count:
            log_message(
                "filtered transcript content "
                f"session_id={session['session_id']} "
                f"before={original_count} after={len(transcript_content)}"
            )

        chat_history = get_chat_history(session["session_id"])
        artifact_outputs = {
            "artifact": {
                "type": "transcript",
                "format": "json",
                "path": transcript_path,
                "content": transcript_content,
                "reason": "session_end",
            }
        }
        session_start_id = str(session["event_id"])
        session_end_id = session.get("session_end_event_id")
        try:
            if chat_history:
                update_event_outputs(
                    session_config,
                    event_id=session_start_id,
                    outputs={"chat_history": chat_history},
                )
            if session_end_id:
                update_event_outputs(
                    session_config,
                    event_id=str(session_end_id),
                    outputs=artifact_outputs,
                )
            session_metrics = _compute_session_metrics(transcript_content)
            if session_metrics:
                try:
                    token_metadata = _session_token_metadata(session_metrics)
                    update_event(
                        session_config,
                        event_id=session_start_id,
                        metadata=token_metadata or None,
                        metrics=session_metrics,
                    )
                    log_message(
                        "attached session metrics "
                        f"session_id={session['session_id']} "
                        f"metrics_count={len(session_metrics)}"
                    )
                except Exception as metrics_exc:
                    log_message(
                        "failed to attach session metrics "
                        f"session_id={session['session_id']}: {metrics_exc}"
                    )

            mark_session_artifact_pushed(session["session_id"], _now_ms())
            log_message(
                "updated session artifact "
                f"session_id={session['session_id']} "
                "reason=session_end"
            )
        except Exception as exc:  # pragma: no cover
            retry_count = increment_session_artifact_retry(session["session_id"])
            log_message(
                "failed session artifact update "
                f"session_id={session['session_id']} "
                f"retry={retry_count}/{ARTIFACT_MAX_RETRIES}: {exc}"
            )
            if retry_count >= ARTIFACT_MAX_RETRIES:
                mark_session_artifact_pushed(session["session_id"], _now_ms())
                log_message(
                    "giving up on session artifact after max retries "
                    f"session_id={session['session_id']}"
                )


def _session_token_metadata(metrics: dict) -> dict:
    """Return standard token metadata aliases for session-level UI rollups."""
    metadata: dict = {}
    input_tokens = metrics.get("coding_agent.total_input_tokens")
    output_tokens = metrics.get("coding_agent.total_output_tokens")
    total_tokens = metrics.get("coding_agent.total_tokens")

    if input_tokens is not None:
        metadata["prompt_tokens"] = input_tokens
    if output_tokens is not None:
        metadata["completion_tokens"] = output_tokens
    if total_tokens is not None:
        metadata["total_tokens"] = total_tokens
    return metadata


def _settings_have_command(settings_path: Path) -> bool:
    if not settings_path.exists():
        return False
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    hooks = settings.get("hooks", {})
    for entries in hooks.values():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            for hook in entry.get("hooks", []):
                if hook.get("type") == "command" and _is_daemon_hook_command(
                    str(hook.get("command", ""))
                ):
                    return True
    return False


def _resolve_repo(repo: Optional[Path]) -> Optional[Path]:
    if repo is not None:
        return find_git_root(repo) or repo.resolve()
    return find_git_root(Path.cwd())


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)
