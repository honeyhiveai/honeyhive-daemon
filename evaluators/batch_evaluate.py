#!/usr/bin/env python3
"""Batch-evaluate existing coding agent sessions in HoneyHive.

Exports session events, runs Python evaluators locally, and pushes
metric results back via PUT /events.

Usage:
    python -m evaluators.batch_evaluate [--limit N] [--dry-run] [--page N]

Environment variables:
    HH_API_KEY   - HoneyHive API key (required)
    HH_API_URL   - HoneyHive base URL (default: https://api.honeyhive.ai)
    HH_PROJECT   - HoneyHive project name (required)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from typing import Any, Dict, List


def _api_url() -> str:
    return os.environ.get("HH_API_URL", "https://api.honeyhive.ai").rstrip("/")


def _api_key() -> str:
    key = os.environ.get("HH_API_KEY", "")
    if not key:
        print("ERROR: HH_API_KEY environment variable is required", file=sys.stderr)
        sys.exit(1)
    return key


def _project() -> str:
    project = os.environ.get("HH_PROJECT", "")
    if not project:
        print("ERROR: HH_PROJECT environment variable is required", file=sys.stderr)
        sys.exit(1)
    return project


def _request(method: str, path: str, body: Dict[str, Any] | None = None) -> Dict[str, Any]:
    url = f"{_api_url()}{path}"
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(
        url,
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {_api_key()}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read())


def export_events(filters: List[Dict], limit: int = 100, page: int = 1) -> Dict[str, Any]:
    return _request("POST", "/v1/events/export", {
        "project": _project(),
        "filters": filters,
        "limit": limit,
        "page": page,
    })


def update_event_metrics(event_id: str, metrics: Dict[str, Any]) -> None:
    _request("PUT", "/events", {"event_id": event_id, "metrics": metrics})


# ---------------------------------------------------------------------------
# Local evaluator implementations (mirrors of server-side Python evaluators)
# ---------------------------------------------------------------------------

def _normalize_tool(name: str) -> str | None:
    if not name or not name.startswith("tool."):
        return None
    t = name[5:]
    low = t.lower()
    if low in ("bash",):
        return "bash"
    if low in ("read", "file_read"):
        return "file_read"
    if low in ("write", "file_write", "file_create"):
        return "file_write"
    if low in ("edit", "file_edit"):
        return "file_edit"
    if low in ("glob", "grep", "file_search"):
        return "file_search"
    if low in ("agent",):
        return "agent"
    if low in ("webfetch", "websearch", "web_search"):
        return "web"
    if low.startswith("mcp__"):
        return "mcp"
    return "other"


def eval_bash_ratio(session_events: List[Dict]) -> float:
    """Fraction of tool calls that are bash/shell (neutral metric)."""
    tool_count = 0
    bash_count = 0
    for e in session_events:
        if e.get("event_type") != "tool":
            continue
        tool_count += 1
        if _normalize_tool(e.get("event_name", "")) == "bash":
            bash_count += 1
    return round(bash_count / tool_count, 3) if tool_count else 0.0


def eval_bash_edit_misuse(session_events: List[Dict]) -> float:
    """Fraction of file edits done via bash (sed -i/awk -i) vs native Edit tool."""
    import re
    edit_tool_count = 0
    bash_edit_count = 0
    for e in session_events:
        if e.get("event_type") != "tool":
            continue
        cat = _normalize_tool(e.get("event_name", ""))
        if cat == "file_edit":
            edit_tool_count += 1
        elif cat == "bash":
            cmd = str(e.get("inputs", {}).get("command", ""))
            if re.search(r'\b(sed|awk)\b.*-i', cmd):
                bash_edit_count += 1
    total = edit_tool_count + bash_edit_count
    return round(bash_edit_count / total, 3) if total else 0.0


def eval_file_search_spam(session_events: List[Dict]) -> float:
    tool_count = 0
    search_count = 0
    for e in session_events:
        if e.get("event_type") != "tool":
            continue
        tool_count += 1
        cat = _normalize_tool(e.get("event_name", ""))
        if cat == "file_search":
            search_count += 1
    return round(search_count / tool_count, 3) if tool_count else 0.0


def eval_permission_bottleneck(session_events: List[Dict]) -> float:
    total = len(session_events)
    perm_count = sum(
        1 for e in session_events
        if "permission" in str(e.get("outputs", {}).get("message", "")).lower()
        or e.get("metadata", {}).get("notification_type") == "permission_prompt"
    )
    return round(perm_count / total, 3) if total else 0.0


def eval_subagent_lifecycle(session_events: List[Dict]) -> bool:
    starts = sum(1 for e in session_events if e.get("event_name") == "chain.subagent.start")
    stops = sum(1 for e in session_events if e.get("event_name") == "chain.subagent.stop")
    return starts == 0 or starts == stops


def eval_session_event_count(session_events: List[Dict]) -> float:
    return float(len(session_events))


def eval_tool_model_ratio(session_events: List[Dict]) -> float:
    tools = sum(1 for e in session_events if e.get("event_type") == "tool")
    models = sum(1 for e in session_events if e.get("event_type") == "model")
    if models == 0:
        return float(tools) if tools else 0.0
    return round(tools / models, 2)


def eval_tool_distribution(session_events: List[Dict]) -> Dict[str, int]:
    """Return a breakdown of tool categories used."""
    dist: Dict[str, int] = {}
    for e in session_events:
        if e.get("event_type") != "tool":
            continue
        cat = _normalize_tool(e.get("event_name", "")) or "unknown"
        dist[cat] = dist.get(cat, 0) + 1
    return dist


def eval_has_errors(session_events: List[Dict]) -> bool:
    return any(
        e.get("error") or e.get("metadata", {}).get("tool.status") == "failure"
        for e in session_events
    )


def evaluate_session(session_events: List[Dict]) -> Dict[str, Any]:
    """Run all local evaluators on a session's events and return metrics dict."""
    return {
        "coding_agent.bash_ratio": eval_bash_ratio(session_events),
        "coding_agent.bash_edit_misuse": eval_bash_edit_misuse(session_events),
        "coding_agent.file_search_spam": eval_file_search_spam(session_events),
        "coding_agent.permission_bottleneck": eval_permission_bottleneck(session_events),
        "coding_agent.subagent_lifecycle": eval_subagent_lifecycle(session_events),
        "coding_agent.session_event_count": eval_session_event_count(session_events),
        "coding_agent.tool_to_model_ratio": eval_tool_model_ratio(session_events),
        "coding_agent.tool_distribution": eval_tool_distribution(session_events),
        "coding_agent.has_errors": eval_has_errors(session_events),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Batch-evaluate coding agent sessions")
    parser.add_argument("--limit", type=int, default=50, help="Sessions per page to export")
    parser.add_argument("--pages", type=int, default=10, help="Max pages to process")
    parser.add_argument("--dry-run", action="store_true", help="Compute metrics but don't push back")
    parser.add_argument("--verbose", action="store_true", help="Print per-session details")
    args = parser.parse_args()

    print(f"Exporting session.end events from {_project()}...")

    total_evaluated = 0
    total_pushed = 0
    aggregate: Dict[str, list] = {
        "bash_ratio": [],
        "file_search_spam": [],
        "permission_bottleneck": [],
        "subagent_lifecycle": [],
        "session_event_count": [],
        "tool_to_model_ratio": [],
    }

    for page in range(1, args.pages + 1):
        # Get session.end events (they have the artifact with full transcript)
        data = export_events(
            [{"field": "event_type", "operator": "is", "value": "session", "type": "string"}],
            limit=args.limit,
            page=page,
        )
        sessions = data.get("events", [])
        if not sessions:
            break

        print(f"Page {page}: {len(sessions)} sessions")

        for session_evt in sessions:
            sid = session_evt.get("session_id", "?")

            # Fetch all events for this session
            try:
                session_data = export_events(
                    [{"field": "session_id", "operator": "is", "value": sid, "type": "string"}],
                    limit=7500,
                )
            except Exception as exc:
                print(f"  SKIP {sid[:12]}: failed to fetch events: {exc}")
                continue

            session_events = session_data.get("events", [])
            if len(session_events) < 2:
                continue

            metrics = evaluate_session(session_events)
            total_evaluated += 1

            # Collect aggregates
            for key in aggregate:
                full_key = f"coding_agent.{key}"
                val = metrics.get(full_key)
                if val is not None and isinstance(val, (int, float)):
                    aggregate[key].append(val)

            if args.verbose:
                print(f"  {sid[:12]}: events={len(session_events)} "
                      f"bash={metrics['coding_agent.bash_ratio']:.2f} "
                      f"search={metrics['coding_agent.file_search_spam']:.2f} "
                      f"perm={metrics['coding_agent.permission_bottleneck']:.2f} "
                      f"ratio={metrics['coding_agent.tool_to_model_ratio']:.1f}")

            if not args.dry_run:
                try:
                    update_event_metrics(session_evt["event_id"], metrics)
                    total_pushed += 1
                except Exception as exc:
                    print(f"  FAILED to push metrics for {sid[:12]}: {exc}")

    # Summary report
    print(f"\n{'='*60}")
    print(f"BATCH EVALUATION SUMMARY")
    print(f"{'='*60}")
    print(f"Sessions evaluated: {total_evaluated}")
    if not args.dry_run:
        print(f"Metrics pushed: {total_pushed}")

    for key, values in aggregate.items():
        if not values:
            continue
        avg = sum(values) / len(values)
        mn = min(values)
        mx = max(values)
        p50 = sorted(values)[len(values) // 2]
        print(f"\n  {key}:")
        print(f"    n={len(values)}, avg={avg:.3f}, p50={p50:.3f}, min={mn:.3f}, max={mx:.3f}")

        # Flag concerning sessions
        if key == "bash_ratio":
            flagged = sum(1 for v in values if v > 0.5)
            if flagged:
                print(f"    WARNING: {flagged} sessions exceed 50% bash usage")
        elif key == "file_search_spam":
            flagged = sum(1 for v in values if v > 0.4)
            if flagged:
                print(f"    WARNING: {flagged} sessions exceed 40% file search")
        elif key == "session_event_count":
            flagged = sum(1 for v in values if v > 500)
            if flagged:
                print(f"    WARNING: {flagged} sessions exceed 500 events")
        elif key == "tool_to_model_ratio":
            flagged = sum(1 for v in values if v > 30)
            if flagged:
                print(f"    WARNING: {flagged} sessions exceed 30:1 tool-to-model ratio")


if __name__ == "__main__":
    main()
