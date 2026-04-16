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
from .error_categories import categorize, load_rules


# ---------------------------------------------------------------------------
# Cadence → cron schedule
# ---------------------------------------------------------------------------

CADENCES = {
    "hourly": "0 * * * *",
    "daily": "0 9 * * *",
    "weekly": "0 9 * * 1",
}


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
    # Use explicit isinstance check to handle non-string values safely.
    for key in ("stderr", "error", "message"):
        raw_val = outputs.get(key)
        if not raw_val:
            continue
        val = raw_val.strip() if isinstance(raw_val, str) else str(raw_val).strip()
        if val:
            return val

    return ""


def _detect_patterns(
    error_events: list,
    categories: list,
    skip_patterns: list,
) -> list:
    groups: dict = defaultdict(list)
    for ev in error_events:
        error = _extract_error(ev)
        if not error:
            continue
        cat_key, fix, _ = categorize(error, categories, skip_patterns)
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


def _detect_loop_patterns(error_events: list) -> list:
    """Find sessions where the same tool fails repeatedly — agent stuck in a retry loop.

    Works on the already-fetched error events so no extra API calls are needed.
    Surfaces session+tool combos where the same tool errors 5+ times in one session,
    which indicates the agent is retrying a broken call rather than escalating.
    """
    # Group errors by session × tool
    session_tool: dict = defaultdict(lambda: defaultdict(list))
    for ev in error_events:
        sid = ev.get("session_id")
        tool = (ev.get("event_name") or "unknown").replace("tool.", "")
        if sid:
            session_tool[str(sid)][tool].append(ev)

    # Collect (tool, session_id, count) triples where count >= 5
    loop_cases: dict = defaultdict(list)  # tool → [(sid, count, evts)]
    for sid, tools in session_tool.items():
        for tool, evts in tools.items():
            if len(evts) >= 5:
                loop_cases[tool].append((sid, len(evts), evts))

    patterns = []
    for tool, cases in sorted(loop_cases.items(), key=lambda x: -len(x[1])):
        n_sessions = len(cases)
        if n_sessions == 0:
            continue
        worst_sid, worst_count, worst_evts = max(cases, key=lambda x: x[1])
        all_evts = [ev for _, _, evts in cases for ev in evts]
        ts_list = [ev["start_time"] for ev in all_evts if ev.get("start_time")]
        sample = _extract_error(worst_evts[0])[:200] if worst_evts else ""
        session_ids = [sid for sid, _, _ in cases]
        patterns.append({
            "id": "retry_loop",
            "type": "retry_loop",
            "error_category": "retry_loop",
            "tool": tool,
            "error_snippet": (
                f"{tool} failing {worst_count}x in one session — agent retrying instead of escalating."
                + (f" Sample: {sample}" if sample else "")
            ),
            "occurrences": len(all_evts),
            "affected_sessions": n_sessions,
            "confidence": "high" if n_sessions >= 3 else "medium" if n_sessions >= 2 else "low",
            "suggested_fix": (
                "Agent is stuck retrying the same failing call. Consider: a helper script "
                "that encodes the correct invocation, a PostToolUse hook that detects the "
                "loop condition and surfaces a clear escalation message, or explicit "
                "CLAUDE.md guidance on when to stop retrying and ask for help."
            ),
            "example_session_id": worst_sid,
            "first_seen_ms": min(ts_list) if ts_list else None,
            "last_seen_ms": max(ts_list) if ts_list else None,
        })

    return sorted(patterns, key=lambda p: p["occurrences"], reverse=True)


def _detect_evaluator_patterns(url: str, key: str, project: str, since_ms: int) -> list:
    """Query evaluator results and surface data-leakage and adherence failures.

    Looks for two evaluator metric names created by ``push-evaluators``:
    - ``Sensitive Data Leakage - <slug>`` → returns LEAK:<types> or CLEAN
    - ``Instruction Adherence - <slug>``  → float 0-4; low scores = adherence failures

    Both metrics are optional — if neither evaluator has run yet, returns [].
    Gracefully handles API errors (HH may not support nested metric filters
    on all deployments).
    """
    from .evaluators import _safe_project_slug  # avoid circular at module level

    slug = _safe_project_slug(project)
    leakage_metric = f"Sensitive Data Leakage - {slug}"
    adherence_metric = f"Instruction Adherence - {slug}"
    patterns: list[dict] = []

    # ----- Data leakage -----
    try:
        leak_events = _query(url, key, project, filters=[
            {"field": f"metrics.{leakage_metric}", "operator": "contains",
             "value": "LEAK", "type": "string"},
            {"field": "start_time", "operator": ">=", "type": "number", "value": since_ms},
        ], limit=500)

        if leak_events:
            # Aggregate by leak type (parse "LEAK:type1,type2").
            type_groups: dict = defaultdict(list)
            for ev in leak_events:
                metric_val = (ev.get("metrics") or {}).get(leakage_metric, "")
                leak_types = metric_val.replace("LEAK:", "").split(",") if "LEAK:" in metric_val else ["unknown"]
                for t in leak_types:
                    t = t.strip()
                    if t:
                        type_groups[t].append(ev)

            for leak_type, evts in sorted(type_groups.items(), key=lambda x: -len(x[1])):
                n = len(evts)
                session_ids = list({str(e["session_id"]) for e in evts if e.get("session_id")})
                ts_list = [e["start_time"] for e in evts if e.get("start_time")]
                confidence = "high" if n >= 5 else "medium" if n >= 3 else "low"
                patterns.append({
                    "id": f"data_leakage_{leak_type}",
                    "type": "data_leakage",
                    "error_category": "data_leakage",
                    "tool": "evaluator",
                    "error_snippet": f"{leak_type.replace('_', ' ')} found in session inputs/outputs",
                    "occurrences": n,
                    "affected_sessions": len(session_ids),
                    "confidence": confidence,
                    "suggested_fix": (
                        "Remove sensitive data from prompts and tool outputs; "
                        "use environment variables instead of inline credentials"
                    ),
                    "example_session_id": session_ids[0] if session_ids else None,
                    "first_seen_ms": min(ts_list) if ts_list else None,
                    "last_seen_ms": max(ts_list) if ts_list else None,
                    "evaluator": leakage_metric,
                })
    except (httpx.HTTPStatusError, httpx.RequestError):
        pass  # evaluator not pushed yet or API doesn't support metric filtering

    # ----- Adherence failures -----
    try:
        low_adherence = _query(url, key, project, filters=[
            {"field": f"metrics.{adherence_metric}", "operator": "less than",
             "value": 2, "type": "number"},
            {"field": "start_time", "operator": ">=", "type": "number", "value": since_ms},
        ], limit=500)

        if low_adherence:
            session_ids = list({str(e["session_id"]) for e in low_adherence if e.get("session_id")})
            ts_list = [e["start_time"] for e in low_adherence if e.get("start_time")]
            n = len(low_adherence)
            confidence = "high" if n >= 5 else "medium" if n >= 3 else "low"
            # Find the lowest-scoring example
            worst = min(
                (e for e in low_adherence if (e.get("metrics") or {}).get(adherence_metric) is not None),
                key=lambda e: e["metrics"][adherence_metric],
                default=low_adherence[0],
            )
            score = (worst.get("metrics") or {}).get(adherence_metric, "?")
            patterns.append({
                "id": "adherence_failure",
                "type": "adherence_failure",
                "error_category": "adherence_failure",
                "tool": "evaluator",
                "error_snippet": f"Instruction adherence score {score}/4 — agent deviated from CLAUDE.md rules",
                "occurrences": n,
                "affected_sessions": len(session_ids),
                "confidence": confidence,
                "suggested_fix": "Review deviating sessions and strengthen rules in CLAUDE.md",
                "example_session_id": session_ids[0] if session_ids else None,
                "first_seen_ms": min(ts_list) if ts_list else None,
                "last_seen_ms": max(ts_list) if ts_list else None,
                "evaluator": adherence_metric,
            })
    except (httpx.HTTPStatusError, httpx.RequestError):
        pass

    return patterns


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
            --project '__PROJECT__'
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
          Read /tmp/hh-patterns.json.

          ## Your job
          Make this agent system more reliable by turning recurring failures into
          code. The goal is to move predictable, closed-ended problems out of the
          LLM's attention so it can focus on genuinely open-ended work.

          Every CLAUDE.md line costs tokens on every session and depends on the
          model remembering it. A hook or script runs deterministically, costs
          nothing, and never forgets. Prefer the most reliable artifact that fits.

          ## For each pattern with occurrences >= 3 and confidence != low

          ### Step 1 — understand
          Read the error_snippet and tool. Look at the error_category and
          suggested_fix fields as hints, not prescriptions. Ask yourself:
          - What is the agent trying to do when this fails?
          - Is this failure predictable from context (closed-ended)?
            Or does it require judgment each time (open-ended)?

          ### Step 2 — choose the right artifact
          Pick whichever fits the specific failure — there is no required order:

          **Hook** (.claude/hooks/): best for failures that happen at a known
          tool boundary and have a deterministic response regardless of task.
          Examples: PreToolUse on Read to validate the path exists; PostToolUse
          on Bash to intercept a sentinel exit code and exit 0 silently; a hook
          that auto-fetches before git diff when exit 128 occurs.

          **Script**: best when the correct behavior can be encoded once and
          called by name. First, look for an existing scripts directory in the
          repo (bin/, scripts/, tools/, Makefile, etc.) and place the file
          there. If none exists, create a bin/ directory or inline the logic
          as a shell function in an appropriate existing file. Examples: a
          retry wrapper for rate-limited calls; a setup script that installs
          a missing tool; a chunker for operations that consistently time out.

          **Config** (.claude/settings.json, package.json, etc.): best for
          declarative facts. Examples: adding a command to allowedTools;
          pinning a dependency version.

          **CLAUDE.md**: only when the fix requires judgment that changes with
          context — auth rotation steps, architectural guidance, escalation
          rules. Keep it short. If you add something here that could later
          become a hook or script, add a comment: `<!-- TODO: promote to hook -->`.

          ### Step 3 — implement and open a PR
          - Branch: hh-improve-YYYYMMDD-<short-slug>
          - One fix per PR, minimal change — no refactoring, no extra cleanup
          - PR title: "[HH] <artifact type>: <what it does>"
            e.g. "[HH] hook: intercept git exit 128, auto-fetch origin"
                 "[HH] helper: retry wrapper for rate-limited API calls"
                 "[HH] doc: auth rotation steps for HubSpot token expiry"
          - PR body: sessions affected, error snippet, what the artifact does

          ## Special cases
          - retry_loop: the agent is stuck retrying a broken call. A PostToolUse
            hook that detects N consecutive failures on the same tool and surfaces
            a clear "stop and escalate" message is usually the right fix.
          - exit_code_sentinel: the script uses non-zero exits as no-op signals.
            A wrapper script or PostToolUse hook that intercepts the specific code
            and exits 0 silently removes it from error traces permanently.
          - data_leakage: a PreToolUse hook that scrubs known-sensitive patterns
            from tool inputs before they leave the session.
          - adherence_failure: read the low-scoring sessions to find which rule
            was violated, then either strengthen the CLAUDE.md rule or — if the
            violation is mechanical (always the same mistake) — encode it as a hook.
          - unknown_error with >= 5 occurrences: also append to
            .honeyhive/error-categories.json "discovered" array (id, pattern,
            fix, discovered_at ISO8601, occurrences, sample) so future analyze
            runs classify it automatically.

          ## Limits
          - At most 3 PRs per run — pick the highest-impact patterns
          - Never modify test files or lock files
          - If nothing qualifies, print: "No actionable patterns found."
          PROMPT
          claude --dangerously-skip-permissions -p "$(cat /tmp/improve-prompt.txt)"

      - name: No actionable patterns
        if: steps.analyse.outputs.actionable_count == '0'
        run: echo "No patterns with 3+ occurrences found in this window. Nothing to fix."
"""


_PROJECT_SAFE_RE = re.compile(r"[^a-zA-Z0-9_\-.]")


def _validate_project_for_yaml(project: str) -> None:
    """Raise ClickException if the project name is unsafe to embed in shell commands.

    Project names with spaces or shell metacharacters would produce broken YAML
    without additional quoting. Reject early with a clear error rather than
    silently generating a broken workflow file.
    """
    if _PROJECT_SAFE_RE.search(project):
        raise click.ClickException(
            f"Project name {project!r} contains characters that are unsafe in shell "
            "commands. Use only letters, digits, hyphens, underscores, and dots.\n"
            "Rename the project with 'honeyhive-daemon init --project safe-name'."
        )


def generate_workflow(project: str, cadence: str) -> str:
    _validate_project_for_yaml(project)
    cron = CADENCES[cadence]
    # Substitute __PROJECT__ before __CRON__ to prevent any cross-contamination
    # if the project name somehow contained the __CRON__ token.
    return (
        _WORKFLOW_TEMPLATE
        .replace("__PROJECT__", project, )
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

    # Load per-repo error category config (falls back to built-in defaults).
    categories, skip_patterns = load_rules(str(Path.cwd()))

    click.echo(f"Querying project '{project}' for the last {since}…", err=True)

    # Query failed tool events.
    # Bug fix: use an explicit flag to distinguish "query succeeded with 0 results"
    # from "query failed" — previously both set failure_events=[] which caused the
    # second query's error handler to raise when the first merely returned 0 events.
    first_query_ok = False
    failure_events: list = []
    try:
        failure_events = _query(url, key, project, filters=[
            {"field": "metadata.tool.status", "operator": "is",  "type": "string", "value": "failure"},
            {"field": "start_time",           "operator": ">=",  "type": "number", "value": since_ms},
        ])
        first_query_ok = True
    except (httpx.HTTPStatusError, httpx.RequestError):
        first_query_ok = False

    errfield_events: list = []
    try:
        errfield_events = _query(url, key, project, filters=[
            {"field": "error",      "operator": "is not null", "type": "string"},
            {"field": "start_time", "operator": ">=",          "type": "number", "value": since_ms},
        ])
    except httpx.HTTPStatusError as exc:
        if not first_query_ok:
            raise click.ClickException(
                f"HoneyHive API error {exc.response.status_code}: {exc.response.text[:200]}"
            )
    except httpx.RequestError as exc:
        if not first_query_ok:
            raise click.ClickException(f"Network error querying HoneyHive: {exc}")

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

    # Detect error patterns from tool failure events.
    patterns = _detect_patterns(error_events, categories, skip_patterns)

    # Detect retry loops — same tool failing 5+ times in one session.
    loop_patterns = _detect_loop_patterns(error_events)
    patterns.extend(loop_patterns)

    # Detect evaluator-based patterns (data leakage + adherence failures).
    # These are additive — if evaluators haven't been pushed yet they return [].
    evaluator_patterns = _detect_evaluator_patterns(url, key, project, since_ms)
    patterns.extend(evaluator_patterns)
    patterns.sort(key=lambda p: p["occurrences"], reverse=True)

    actionable = sum(1 for p in patterns if p["occurrences"] >= 3)

    click.echo(
        f"Found {len(error_events)} error events across "
        f"{'?' if session_count is None else session_count} sessions → "
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

    # Scaffold per-repo error categories config if it doesn't exist yet.
    from .error_categories import init_config as _init_categories
    root_for_cats = find_project_root(str(cwd)) or str(cwd)
    # Check existence BEFORE calling init_config — init_config creates the file,
    # so checking afterwards always returns True (Bug fix: cats_existed was always True).
    cats_existed = (Path(root_for_cats) / ".honeyhive" / "error-categories.json").exists()
    cats_path = _init_categories(root_for_cats)

    cron = CADENCES[cadence]
    click.echo("")
    try:
        wf_display = workflow_path.relative_to(cwd)
    except ValueError:
        wf_display = workflow_path
    click.echo(f"{'Updated' if already_existed else 'Created'} {wf_display}")

    try:
        cats_rel = cats_path.relative_to(cwd)
        click.echo(
            f"{'Already exists' if cats_existed else 'Created'} "
            f"{cats_rel}  (per-repo error categories)"
        )
    except ValueError:
        pass
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
