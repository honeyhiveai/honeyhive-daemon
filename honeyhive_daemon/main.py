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
from .state import (
    append_chat_history,
    append_spool_event,
    get_chat_history,
    buffer_pending_tool_event,
    get_expired_tool_events,
    get_sessions_needing_artifact,
    log_message,
    mark_session_artifact_pushed,
    pop_pending_tool_event,
    read_spool_events,
    record_session_activity,
    replace_spool_events,
)


SESSION_IDLE_THRESHOLD_MS = 24 * 60 * 60 * 1000


def _compute_session_metrics(transcript_content: list) -> dict:
    """Compute client-side metrics from a session transcript.

    These are attached to the session event via PUT /events so they're
    available for dashboards and evaluator filters without needing
    server-side evaluators to re-parse the transcript.
    """
    tool_count = 0
    model_count = 0
    chain_count = 0
    bash_count = 0
    search_count = 0
    permission_count = 0
    subagent_starts = 0
    subagent_stops = 0
    has_errors = False
    tool_categories: dict[str, int] = {}

    for record in transcript_content:
        if not isinstance(record, dict):
            continue

        # Detect event type from transcript record
        rtype = record.get("type", "")
        hook_event = record.get("hook_event_name", "")

        # Tool use records
        if rtype in ("tool_use", "tool_result"):
            tool_count += 1
            tool_name = (record.get("tool_name") or record.get("name") or "").lower()
            if tool_name in ("bash",):
                bash_count += 1
                tool_categories["bash"] = tool_categories.get("bash", 0) + 1
            elif tool_name in ("read", "file_read"):
                tool_categories["file_read"] = tool_categories.get("file_read", 0) + 1
            elif tool_name in ("write", "file_write", "file_create"):
                tool_categories["file_write"] = tool_categories.get("file_write", 0) + 1
            elif tool_name in ("edit", "file_edit"):
                tool_categories["file_edit"] = tool_categories.get("file_edit", 0) + 1
            elif tool_name in ("glob", "grep", "file_search"):
                search_count += 1
                tool_categories["file_search"] = tool_categories.get("file_search", 0) + 1
            elif tool_name in ("agent",):
                tool_categories["agent"] = tool_categories.get("agent", 0) + 1
            elif tool_name.startswith("mcp__"):
                tool_categories["mcp"] = tool_categories.get("mcp", 0) + 1
            else:
                tool_categories["other"] = tool_categories.get("other", 0) + 1

            if rtype == "tool_result" and record.get("is_error"):
                has_errors = True

        elif rtype in ("text", "thinking"):
            model_count += 1

        # Notification records
        if record.get("notification_type") == "permission_prompt":
            permission_count += 1

        # Subagent tracking
        if hook_event == "SubagentStart":
            subagent_starts += 1
        elif hook_event == "SubagentStop":
            subagent_stops += 1

    total = tool_count + model_count + chain_count
    metrics: dict[str, object] = {
        "coding_agent.total_events": float(len(transcript_content)),
        "coding_agent.tool_count": float(tool_count),
        "coding_agent.model_count": float(model_count),
        "coding_agent.unique_tools": float(len(tool_categories)),
    }
    if tool_count > 0:
        metrics["coding_agent.bash_ratio"] = round(bash_count / tool_count, 3)
        metrics["coding_agent.search_ratio"] = round(search_count / tool_count, 3)
    if model_count > 0:
        metrics["coding_agent.tool_model_ratio"] = round(tool_count / model_count, 2)
    if total > 0:
        metrics["coding_agent.permission_ratio"] = round(permission_count / total, 3)
    metrics["coding_agent.has_errors"] = has_errors
    metrics["coding_agent.subagent_balanced"] = (
        subagent_starts == 0 or subagent_starts == subagent_stops
    )

    return metrics


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
    "--project",
    envvar="HH_PROJECT",
    default=None,
    help="HoneyHive project override (deprecated — use 'honeyhive-daemon init').",
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
    project: Optional[str],
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
    project_from_cli = (
        ctx.get_parameter_source("project") == click.core.ParameterSource.COMMANDLINE
    )

    if key_from_cli:
        click.echo(
            "Warning: --key is deprecated. "
            "Use 'honeyhive-daemon init' to set up per-project config."
        )
    if project_from_cli:
        click.echo(
            "Warning: --project is deprecated. "
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
            if not project:
                project = legacy.project

    if not api_key:
        click.echo(
            "Error: No API key found.\n\n"
            "Provide one of:\n"
            "  1. Run 'honeyhive-daemon init' in your project directory\n"
            "  2. Set the HH_API_KEY environment variable\n"
            "  3. Pass --key (deprecated)"
        )
        raise SystemExit(1)

    # --- Fallback chain for project ---------------------------------------
    if not project:
        user_cfg = load_user_config()
        project = user_cfg.get("project")

    if not project:
        legacy = load_config()
        if legacy and legacy.project:
            project = legacy.project

    resolved_project = project or _derive_project_name(repo_root)

    config = DaemonConfig(
        api_key=api_key,
        base_url=base_url,
        project=resolved_project,
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
        f"project={resolved_project} "
        f"repo={repo_root or '-'} "
        f"ci={ci}"
    )
    _flush_spool(config)

    click.echo(f"Daemon home: {get_daemon_home()}")
    click.echo(f"Filters: {filters_path}")
    click.echo(f"Claude settings: {settings_path}")
    click.echo(f"Project: {resolved_project}")
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
@click.option("--project", "-p", required=True, help="HoneyHive project name")
@click.option("--api-key-env", default="HH_API_KEY", help="Env var holding the API key")
def init(project: str, api_key_env: str) -> None:
    """Initialize .honeyhive/ config in the current directory."""
    cwd = Path.cwd()
    hh_dir = cwd / ".honeyhive"

    if hh_dir.exists():
        click.echo(f"Warning: {hh_dir} already exists — updating files.")

    hh_dir.mkdir(parents=True, exist_ok=True)

    # Write project config
    project_config_path = hh_dir / "config.json"
    project_config_path.write_text(
        json.dumps({"project": project}, indent=2) + "\n",
        encoding="utf-8",
    )

    # Write local config (not committed)
    local_config_path = hh_dir / "config.local.json"
    local_config_path.write_text(
        json.dumps({"api_key_env": api_key_env}, indent=2) + "\n",
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
    click.echo(f"  config.json        → project: {project}")
    click.echo(f"  config.local.json  → api_key_env: {api_key_env}")
    click.echo(f"Updated {gitignore_path}")


@cli.command()
def status() -> None:
    """Show daemon status."""
    config = load_config()
    pending = len(read_spool_events())
    click.echo(f"Daemon home: {get_daemon_home()}")
    click.echo(f"Configured: {'yes' if config else 'no'}")
    click.echo(f"Pending spool events: {pending}")
    if config:
        click.echo(f"Project: {config.project}")
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
    session_state = record_session_activity(
        str(event["session_id"]),
        transcript_path=str(transcript_path) if transcript_path else None,
        last_activity_ms=int(event["end_time"]),
        ended=event["event_name"] == "session.end",
        session_end_event_id=(
            str(event["event_id"]) if event["event_name"] == "session.end" else None
        ),
        session_start_exported=True if is_session_start else None,
        cwd=event_cwd,
        session_name=session_name,
    )

    # ── Synthesize session.start if daemon started after session ──
    # When the daemon starts mid-session, it misses the SessionStart hook.
    # Without a session.start event in HoneyHive, artifact updates fail
    # with 400 and all session data is lost. Create it now.
    if not is_session_start and not session_state.get("session_start_exported"):
        synthetic_session = {
            "event_id": str(event["session_id"]),  # session.start uses session_id as event_id
            "session_id": str(event["session_id"]),
            "event_type": "session",
            "event_name": "session.start",
            "start_time": int(event["start_time"]),
            "end_time": int(event["start_time"]),
            "duration": 0,
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
                _apply_transcript_context(event, ctx)
        except Exception:
            pass  # transcript enrichment is best-effort

    # Accumulate chat history for turn events
    turn_role = event.get("metadata", {}).get("turn.role")
    if turn_role and event.get("event_type") == "model":
        content = event.get("outputs", {}).get("content")
        if content is not None:
            history_before = append_chat_history(
                str(event["session_id"]), turn_role, str(content)
            )
            event.setdefault("inputs", {})["chat_history"] = history_before

    try:
        export_event(config, event)
        log_message(
            "exported claude event "
            f"event_name={event['event_name']} "
            f"session_id={event['session_id']}"
        )
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
    merged["start_time"] = pre_event["start_time"]
    merged["duration"] = int(post_event["end_time"]) - int(pre_event["start_time"])

    # Merge inputs from pre, outputs from post
    merged["inputs"] = dict(pre_event.get("inputs", {}))
    merged["inputs"].update(post_event.get("inputs", {}))
    merged["outputs"] = dict(post_event.get("outputs", {}))

    # Merge metadata
    metadata = dict(pre_event.get("metadata", {}))
    metadata.update(post_event.get("metadata", {}))
    metadata["tool.phase"] = "complete"
    metadata["tool.status"] = "failure" if failure else "success"
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


def _apply_transcript_context(event: dict, ctx: TranscriptContext) -> None:
    """Apply thinking, usage, and model metadata from transcript to an event."""
    if ctx.thinking:
        event.setdefault("inputs", {})["thinking"] = ctx.thinking
    metadata = event.setdefault("metadata", {})
    if ctx.model:
        metadata["model"] = ctx.model
    if ctx.request_id:
        metadata["request_id"] = ctx.request_id
    if ctx.usage:
        for key, value in ctx.usage.items():
            metadata[f"usage.{key}"] = value
        # Alias to HoneyHive standard field names
        if "input_tokens" in ctx.usage:
            metadata["prompt_tokens"] = ctx.usage["input_tokens"]
        if "output_tokens" in ctx.usage:
            metadata["completion_tokens"] = ctx.usage["output_tokens"]


def _flush_spool(config: DaemonConfig) -> None:
    pending = read_spool_events()
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
    replace_spool_events(failed)
    flushed = len(pending) - len(failed)
    log_message(f"flush complete flushed={flushed} remaining={len(failed)}")


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
        # Resolve per-session config from stored cwd/session_name
        session_cwd = session.get("cwd")
        sess_name = session.get("session_name")
        session_config = resolve_config(
            cwd=session_cwd,
            session_name=sess_name,
            cli_defaults=config,
        ) if session_cwd or sess_name else config
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

        reason = "session_end" if session.get("ended") else "idle_timeout"
        artifact_outputs = {
            "artifact": {
                "type": "transcript",
                "format": "json",
                "path": transcript_path,
                "content": transcript_content,
                "reason": reason,
            }
        }
        # Session root (session.start) gets only chat_history
        chat_history = get_chat_history(session["session_id"])
        session_root_outputs = {}
        if chat_history:
            session_root_outputs["chat_history"] = chat_history

        # Session end gets the full artifact transcript
        target_event_ids = [str(session["event_id"])]
        session_end_event_id = session.get("session_end_event_id")
        if session_end_event_id and session_end_event_id not in target_event_ids:
            target_event_ids.append(str(session_end_event_id))
        try:
            for event_id in target_event_ids:
                if event_id == str(session["event_id"]):
                    # session.start root event — only chat history
                    if session_root_outputs:
                        update_event_outputs(
                            session_config,
                            event_id=event_id,
                            outputs=session_root_outputs,
                        )
                else:
                    # session.end event — full artifact transcript
                    update_event_outputs(
                        session_config,
                        event_id=event_id,
                        outputs=artifact_outputs,
                    )
            # Compute and attach client-side metrics to the session root event
            session_metrics = _compute_session_metrics(transcript_content)
            if session_metrics:
                try:
                    update_event(
                        session_config,
                        event_id=str(session["event_id"]),
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
                f"reason={reason}"
            )
        except Exception as exc:  # pragma: no cover
            log_message(
                "failed session artifact update "
                f"session_id={session['session_id']}: {exc}"
            )


def _read_transcript_jsonl(transcript_path: str) -> Optional[list]:
    """Read a JSONL transcript and return parsed JSON objects."""
    path = Path(transcript_path)
    if not path.exists() or not path.is_file():
        return None
    records: list = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records if records else None


def _settings_have_command(settings_path: Path) -> bool:
    if not settings_path.exists():
        return False
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    hooks = settings.get("hooks", {})
    target = get_hook_command()
    for entries in hooks.values():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            for hook in entry.get("hooks", []):
                if hook.get("type") == "command" and hook.get("command") == target:
                    return True
    return False


def _derive_project_name(repo_root: Optional[Path]) -> str:
    env_project = os.getenv("HH_PROJECT")
    if env_project:
        return env_project
    if repo_root is not None:
        return repo_root.name
    return Path.cwd().name


def _resolve_repo(repo: Optional[Path]) -> Optional[Path]:
    if repo is not None:
        return find_git_root(repo) or repo.resolve()
    return find_git_root(Path.cwd())


def _now_ms() -> int:
    return int(datetime.now(timezone.utc).timestamp() * 1000)
