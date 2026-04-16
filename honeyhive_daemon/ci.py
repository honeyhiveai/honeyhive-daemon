"""CI integration: analyze command + add-to-ci command."""

from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import click
import httpx

from .config import DEFAULT_BASE_URL, find_project_root, load_project_config


# ---------------------------------------------------------------------------
# Cadence → cron schedule
# ---------------------------------------------------------------------------

CADENCES = {
    "hourly": "0 * * * *",
    "daily": "0 9 * * *",
    "weekly": "0 9 * * 1",
}

# ---------------------------------------------------------------------------
# Error categorisation
# ---------------------------------------------------------------------------

_ERROR_RULES = [
    ("permission denied",    "permission_denied",  "Add the command to allowedTools in .claude/settings.json"),
    ("operation not permitted", "permission_denied", "Add the command to allowedTools in .claude/settings.json"),
    ("command not found",    "missing_command",    "Install the missing tool or document it in CLAUDE.md"),
    ("no such file",         "missing_file",       "Add path guidance or setup steps to CLAUDE.md"),
    ("cannot find module",   "missing_module",     "Add the install step to your setup docs"),
    ("module not found",     "missing_module",     "Add the install step to your setup docs"),
    ("authentication",       "auth_failure",       "Verify API credentials and document rotation in CLAUDE.md"),
    ("unauthorized",         "auth_failure",       "Verify API credentials and document rotation in CLAUDE.md"),
    ("http 401",             "auth_failure",       "Verify API credentials and document rotation in CLAUDE.md"),
    ("401 unauthorized",     "auth_failure",       "Verify API credentials and document rotation in CLAUDE.md"),
    ("rate limit",           "rate_limit",         "Add retry logic or caching for this operation"),
    ("too many requests",    "rate_limit",         "Add retry logic or caching for this operation"),
    ("timed out",            "timeout",            "Break into smaller operations or add a timeout handler"),
    ("search timed out",     "timeout",            "Use narrower glob patterns or break search into smaller scopes"),
    ("connection refused",   "connection_refused", "Check service availability and add health-check guidance to CLAUDE.md"),
    ("syntax error",         "syntax_error",       "Review code-generation patterns; add examples to CLAUDE.md"),
    ("traceback",            "python_exception",   "Add error handling guidance or a helper script"),
    ("exit code 127",        "missing_command",    "Install the missing tool or add it to PATH in CLAUDE.md"),
    ("exit code 126",        "permission_denied",  "Check file permissions; add execution steps to CLAUDE.md"),
    ("exit code 128",        "git_error",          "Add git setup steps to CLAUDE.md (fetch, base branch, worktree)"),
    ("fatal: ",              "git_error",          "Add git setup steps to CLAUDE.md (fetch, base branch, worktree)"),
    ("no merge base",        "git_error",          "Ensure worktrees fetch origin before diffing; add to CLAUDE.md"),
]

# Errors that are too generic to be actionable on their own.
_SKIP_IF_ONLY = {"exit code 1", "exit code 2", "exit code 1\n"}


def _categorize(error: str) -> tuple[str, str]:
    lower = error.lower().strip()
    # Skip events whose entire error content is just a generic exit code.
    if lower in _SKIP_IF_ONLY or lower.startswith("exit code 1\n") is False and lower == "exit code 1":
        if lower in _SKIP_IF_ONLY:
            return "", ""  # sentinel: caller skips this event
    for pattern, key, fix in _ERROR_RULES:
        if pattern in lower:
            return key, fix
    # If the text is non-trivial (>20 chars beyond the exit-code prefix), keep it.
    content = lower
    for prefix in ("exit code 1\n", "exit code 2\n", "exit code 1 "):
        if content.startswith(prefix):
            content = content[len(prefix):]
            break
    if len(content.strip()) < 10:
        return "", ""  # too short to be useful
    return "unknown_error", "Review the recurring error and add guidance to CLAUDE.md"


# ---------------------------------------------------------------------------
# HoneyHive query helpers
# ---------------------------------------------------------------------------

def _parse_since_ms(since: str) -> int:
    m = re.match(r"^(\d+)([hdw])$", since.strip().lower())
    if not m:
        raise click.BadParameter(
            f"Invalid format {since!r} — use e.g. 24h, 7d, 2w",
            param_hint="--since",
        )
    n, unit = int(m.group(1)), m.group(2)
    seconds = {"h": 3600, "d": 86400, "w": 604800}[unit]
    return int((datetime.now(timezone.utc).timestamp() - n * seconds) * 1000)


def _query(url: str, key: str, project: str, filters: list, limit: int = 500) -> list:
    resp = httpx.post(
        f"{url.rstrip('/')}/v1/events/export",
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        json={"project": project, "filters": filters, "limit": limit},
        timeout=30.0,
    )
    resp.raise_for_status()
    return resp.json().get("events", [])


def _extract_error(ev: dict) -> str:
    """Extract meaningful error text from an event, checking multiple fields.

    Claude Code daemon stores error content in several places depending on
    which hook fired and whether the error was in the raw payload's ``error``
    field or in the tool response (e.g. bash stderr / file-op error message).
    """
    # 1. Top-level ``error`` field — set by _merge_tool_events when raw_post
    #    has an ``error`` key (e.g. permission-denied payloads).
    direct = (ev.get("error") or "").strip()
    if direct:
        return direct

    # 2. ``outputs.tool_response`` — the raw tool response from the hook.
    #    For bash failures this is {"stderr": "...", "returnCode": N}.
    #    For file-edit failures it's {"error": "...", "type": "error"}.
    outputs = ev.get("outputs") or {}
    tool_resp = outputs.get("tool_response")
    if isinstance(tool_resp, dict):
        for key in ("stderr", "error", "message", "content"):
            raw_val = tool_resp.get(key)
            if not raw_val:
                continue
            val = str(raw_val).strip() if not isinstance(raw_val, str) else raw_val.strip()
            if val:
                return val
    elif isinstance(tool_resp, str):
        val = tool_resp.strip()
        if val:
            return val
    elif isinstance(tool_resp, list):
        # Array of content blocks — look for text or error entries.
        for block in tool_resp:
            if isinstance(block, dict):
                for key in ("text", "error", "stderr"):
                    raw_val = block.get(key)
                    if raw_val and str(raw_val).strip():
                        return str(raw_val).strip()
            elif isinstance(block, str) and block.strip():
                return block.strip()

    # 3. Fallback: any bare error/stderr key directly in outputs.
    for key in ("stderr", "error", "message"):
        val = (outputs.get(key) or "").strip()
        if val:
            return val

    return ""


def _detect_patterns(error_events: list) -> list:
    groups: dict = defaultdict(list)
    for ev in error_events:
        error = _extract_error(ev)
        if not error:
            continue
        cat_key, fix = _categorize(error)
        if not cat_key:  # filtered as too-generic
            continue
        groups[(cat_key, fix)].append((ev, error))

    patterns = []
    for (cat_key, fix), event_pairs in groups.items():
        n = len(event_pairs)
        session_ids = list({
            str(ev["session_id"])
            for ev, _ in event_pairs
            if ev.get("session_id")
        })
        ts_list = [ev["start_time"] for ev, _ in event_pairs if ev.get("start_time")]
        confidence = "high" if n >= 5 else "medium" if n >= 3 else "low"
        first_ev, first_err = event_pairs[0]
        patterns.append({
            "id": cat_key,
            "type": "tool_error",
            "error_category": cat_key,
            "tool": (first_ev.get("event_name") or "unknown").replace("tool.", ""),
            "error_snippet": first_err[:300],
            "occurrences": n,
            "affected_sessions": len(session_ids),
            "confidence": confidence,
            "suggested_fix": fix,
            "example_session_id": session_ids[0] if session_ids else None,
            "first_seen_ms": min(ts_list) if ts_list else None,
            "last_seen_ms": max(ts_list) if ts_list else None,
        })

    return sorted(patterns, key=lambda p: p["occurrences"], reverse=True)


# ---------------------------------------------------------------------------
# Workflow YAML template
# ---------------------------------------------------------------------------

# Use __PROJECT__ / __CRON__ as substitution tokens to avoid f-string
# collision with GitHub Actions' ${{ }} expression syntax.

_WORKFLOW_TEMPLATE = """\
# Auto-generated by: honeyhive-daemon add-to-ci
# Queries Claude Code session traces and opens PRs for recurring issues.
name: HoneyHive Proactive Improvements

on:
  schedule:
    - cron: '__CRON__'
  workflow_dispatch:
    inputs:
      since:
        description: 'Time window to analyse (e.g. 24h, 7d)'
        default: '24h'
        required: false

permissions:
  contents: write
  pull-requests: write

jobs:
  improve:
    runs-on: ubuntu-latest
    timeout-minutes: 30
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - uses: actions/setup-python@v5
        with:
          python-version: '3.12'

      - name: Install dependencies
        run: |
          pip install honeyhive-daemon
          npm install -g @anthropic-ai/claude-code@latest

      - name: Configure git
        run: |
          git config user.name "HoneyHive Improvement Bot"
          git config user.email "improvements@honeyhive.ai"

      - name: Push evaluators
        env:
          HH_API_KEY: ${{ secrets.HH_API_KEY }}
          HH_API_URL: ${{ vars.HH_API_URL }}
        run: |
          honeyhive-daemon push-evaluators \\
            --project __PROJECT__
          # Idempotent — safe to run on every CI pass.
          # Updates the CLAUDE.md adherence evaluator if the file changed.

      - name: Analyse Claude Code sessions
        id: analyse
        env:
          HH_API_KEY: ${{ secrets.HH_API_KEY }}
          HH_API_URL: ${{ vars.HH_API_URL }}
        run: |
          honeyhive-daemon analyze \\
            --project __PROJECT__ \\
            --since "${{ github.event.inputs.since || '24h' }}" \\
            --out /tmp/hh-patterns.json
          echo "=== patterns detected ==="
          python3 -m json.tool /tmp/hh-patterns.json
          COUNT=$(python3 -c "
          import json, sys
          p = json.load(open('/tmp/hh-patterns.json'))
          print(sum(1 for x in p['patterns'] if x['occurrences'] >= 3))
          ")
          echo "actionable_count=$COUNT" >> "$GITHUB_OUTPUT"

      - name: Open improvement PRs
        if: steps.analyse.outputs.actionable_count != '0'
        env:
          ANTHROPIC_API_KEY: ${{ secrets.ANTHROPIC_API_KEY }}
          HH_API_KEY: ${{ secrets.HH_API_KEY }}
          HH_API_URL: ${{ vars.HH_API_URL }}
          GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
        run: |
          cat > /tmp/improve-prompt.txt << 'PROMPT'
          Read /tmp/hh-patterns.json. For each pattern with occurrences >= 3:

          1. Understand what error is happening and in which tool.
          2. Determine the right fix:
             - permission_denied     → add command to allowedTools in .claude/settings.json
             - missing_command       → add install note to CLAUDE.md
             - missing_file          → add path guidance to CLAUDE.md
             - missing_module        → add install step to setup docs
             - auth_failure          → add credential rotation note to CLAUDE.md
             - rate_limit            → add caching or retry helper script
             - timeout               → add chunking note to CLAUDE.md
             - unknown_error         → add guidance for the specific error to CLAUDE.md
          3. Create a branch named: hh-improve-YYYYMMDD-<short-slug>
             (use today's actual date, replace spaces/slashes with dashes in slug)
          4. Apply the minimal fix — no refactoring, no extra changes.
          5. Open a PR with title "[HH] Fix: <what was fixed>" and body that includes:
             - How many sessions were affected
             - The error snippet from the patterns file
             - What the fix does and why

          Rules:
          - Open at most 3 PRs per run.
          - Skip patterns with confidence=low.
          - If nothing meets the threshold, print: "No actionable patterns found."
          - Never modify test files or lock files.
          - For git_error: add fetch + base-branch setup steps to CLAUDE.md.
          PROMPT
          claude --dangerously-skip-permissions -p "$(cat /tmp/improve-prompt.txt)"

      - name: No actionable patterns
        if: steps.analyse.outputs.actionable_count == '0'
        run: echo "No patterns with 3+ occurrences found in this window. Nothing to fix."
"""


def generate_workflow(project: str, cadence: str) -> str:
    cron = CADENCES[cadence]
    return (
        _WORKFLOW_TEMPLATE
        .replace("__PROJECT__", project)
        .replace("__CRON__", cron)
    )


# ---------------------------------------------------------------------------
# Click commands (registered in main.py)
# ---------------------------------------------------------------------------

@click.command("analyze")
@click.option(
    "--project", "-p",
    envvar="HH_PROJECT",
    default=None,
    help="HoneyHive project name (falls back to .honeyhive/config.json).",
)
@click.option(
    "--since",
    default="24h",
    show_default=True,
    help="Time window to analyse: 24h, 7d, 2w.",
)
@click.option(
    "--out", "-o",
    default="-",
    show_default=True,
    help="Output file path (- for stdout).",
)
@click.option(
    "--url",
    envvar="HH_API_URL",
    default=DEFAULT_BASE_URL,
    show_default=True,
    help="HoneyHive base URL.",
)
@click.option(
    "--key",
    envvar="HH_API_KEY",
    default=None,
    help="HoneyHive API key.",
)
def analyze_cmd(
    project: Optional[str],
    since: str,
    out: str,
    url: str,
    key: Optional[str],
) -> None:
    """Query HoneyHive traces and detect recurring improvement patterns.

    Outputs a JSON report of error patterns grouped by type, with occurrence
    counts, affected session counts, and suggested fixes. Patterns with
    occurrences >= 3 are considered actionable.

    Examples:

      honeyhive-daemon analyze --since 24h

      honeyhive-daemon analyze --project my-project --since 7d --out patterns.json
    """
    # Resolve project from .honeyhive/config.json if not provided
    if not project:
        root = find_project_root(str(Path.cwd()))
        if root:
            cfg = load_project_config(root)
            project = cfg.get("project")
    if not project:
        raise click.UsageError(
            "No project found. Pass --project or run 'honeyhive-daemon init' first."
        )

    if not key:
        raise click.UsageError(
            "No API key found. Set HH_API_KEY or pass --key."
        )

    since_ms = _parse_since_ms(since)

    click.echo(f"Querying project '{project}' for the last {since}…", err=True)

    # Query failed tool events.
    # Primary filter: metadata.tool.status = failure (set by daemon on every
    # PostToolUseFailure event). This is more reliable than `error is not null`
    # because the daemon always stamps this field even when the error text lives
    # inside outputs.tool_response rather than the top-level error field.
    # Fallback to `error is not null` so we also catch legacy / third-party events
    # that directly set an error field.
    try:
        failure_events = _query(url, key, project, filters=[
            {"field": "metadata.tool.status", "operator": "is",  "type": "string", "value": "failure"},
            {"field": "start_time",           "operator": ">=",  "type": "number", "value": since_ms},
        ])
    except (httpx.HTTPStatusError, httpx.RequestError):
        failure_events = []

    try:
        errfield_events = _query(url, key, project, filters=[
            {"field": "error",      "operator": "is not null", "type": "string"},
            {"field": "start_time", "operator": ">=",          "type": "number", "value": since_ms},
        ])
    except httpx.HTTPStatusError as exc:
        if not failure_events:
            raise click.ClickException(
                f"HoneyHive API error {exc.response.status_code}: {exc.response.text[:200]}"
            )
        errfield_events = []
    except httpx.RequestError as exc:
        if not failure_events:
            raise click.ClickException(f"Network error querying HoneyHive: {exc}")
        errfield_events = []

    # Merge and deduplicate by event_id.
    seen: set = set()
    error_events: list = []
    for ev in failure_events + errfield_events:
        eid = ev.get("event_id") or id(ev)
        if eid not in seen:
            seen.add(eid)
            error_events.append(ev)

    # Query session count
    try:
        session_events = _query(url, key, project, filters=[
            {"field": "event_type", "operator": "is",          "type": "string", "value": "session"},
            {"field": "event_name", "operator": "is",          "type": "string", "value": "session.start"},
            {"field": "start_time", "operator": ">=",          "type": "number", "value": since_ms},
        ], limit=1000)
        session_count = len(session_events)
    except Exception:
        session_count = None

    patterns = _detect_patterns(error_events)
    actionable = sum(1 for p in patterns if p["occurrences"] >= 3)

    click.echo(
        f"Found {len(error_events)} error events across "
        f"{session_count or '?'} sessions → "
        f"{len(patterns)} pattern(s), {actionable} actionable (>=3 occurrences).",
        err=True,
    )

    report = {
        "analyzed_at": datetime.now(timezone.utc).isoformat(),
        "project": project,
        "window": since,
        "session_count": session_count,
        "error_event_count": len(error_events),
        "patterns": patterns,
    }

    payload = json.dumps(report, indent=2)

    if out == "-":
        click.echo(payload)
    else:
        Path(out).write_text(payload + "\n", encoding="utf-8")
        click.echo(f"Wrote {len(patterns)} pattern(s) to {out}", err=True)


@click.command("add-to-ci")
@click.option(
    "--cadence",
    default="daily",
    type=click.Choice(list(CADENCES)),
    show_default=True,
    help="How often the workflow should run.",
)
@click.option(
    "--project", "-p",
    envvar="HH_PROJECT",
    default=None,
    help="HoneyHive project name (falls back to .honeyhive/config.json).",
)
@click.option(
    "--output-dir",
    default=None,
    type=click.Path(file_okay=False, path_type=Path),
    help="Directory to write the workflow file (default: .github/workflows/ in cwd).",
)
def add_to_ci_cmd(
    cadence: str,
    project: Optional[str],
    output_dir: Optional[Path],
) -> None:
    """Add a GitHub Actions workflow for proactive Claude Code improvements.

    Generates .github/workflows/hh-proactive-improvements.yml. The workflow
    runs honeyhive-daemon analyze on a schedule, then invokes Claude Code to
    open PRs for any recurring error patterns (>= 3 occurrences).

    After running this command, add these secrets/variables to your GitHub repo:

    \b
      Secrets:   HH_API_KEY       — HoneyHive API key
                 ANTHROPIC_API_KEY — Anthropic API key
      Variables: HH_API_URL       — HoneyHive base URL (optional, defaults to api.honeyhive.ai)

    Examples:

      honeyhive-daemon add-to-ci

      honeyhive-daemon add-to-ci --cadence weekly --project my-project
    """
    cwd = Path.cwd()

    # Resolve project
    if not project:
        root = find_project_root(str(cwd))
        if root:
            cfg = load_project_config(root)
            project = cfg.get("project")
    if not project:
        raise click.UsageError(
            "No project found. Pass --project or run 'honeyhive-daemon init' first."
        )

    # Resolve output path
    workflows_dir = output_dir or (cwd / ".github" / "workflows")
    workflows_dir.mkdir(parents=True, exist_ok=True)
    workflow_path = workflows_dir / "hh-proactive-improvements.yml"

    already_existed = workflow_path.exists()
    yaml_content = generate_workflow(project, cadence)
    workflow_path.write_text(yaml_content, encoding="utf-8")

    cron = CADENCES[cadence]
    click.echo("")
    click.echo(
        f"{'Updated' if already_existed else 'Created'} "
        f"{workflow_path.relative_to(cwd)}"
    )
    click.echo("")
    click.echo("  Workflow:  HoneyHive Proactive Improvements")
    click.echo(f"  Project:   {project}")
    click.echo(f"  Cadence:   {cadence}  (cron: {cron})")
    click.echo(f"  Trigger:   schedule + workflow_dispatch (manual run any time)")
    click.echo("")
    click.echo("Next steps — add to your GitHub repo:")
    click.echo("")
    click.echo("  Secrets  (Settings → Secrets and variables → Actions):")
    click.echo("    HH_API_KEY         HoneyHive API key")
    click.echo("    ANTHROPIC_API_KEY  Anthropic API key")
    click.echo("")
    click.echo("  Variables  (same page, Variables tab):")
    click.echo("    HH_API_URL         https://api.honeyhive.ai  (or your self-hosted URL)")
    click.echo("")
    click.echo("Commit the workflow file and push — GitHub will pick it up automatically.")
    click.echo(f"To trigger immediately: gh workflow run hh-proactive-improvements.yml")
    click.echo("")
