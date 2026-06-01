"""Client-side session metrics and transcript helpers."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Optional


def read_transcript_jsonl(transcript_path: str) -> Optional[list]:
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


def compute_session_metrics(transcript_content: list) -> dict:
    """Compute client-side metrics from a session transcript.

    These are attached to the session event via PUT /events so they're
    available for dashboards and evaluator filters without needing
    server-side evaluators to re-parse the transcript.
    """
    tool_count = 0
    model_count = 0
    bash_count = 0
    search_count = 0
    permission_count = 0
    subagent_starts = 0
    subagent_stops = 0
    has_errors = False
    tool_categories: dict[str, int] = defaultdict(int)
    total_input_tokens = 0
    total_output_tokens = 0
    total_cache_read_tokens = 0
    total_cache_creation_tokens = 0

    for record in transcript_content:
        if not isinstance(record, dict):
            continue

        rtype = record.get("type", "")
        hook_event = record.get("hook_event_name", "")

        if rtype in ("tool_use", "tool_result"):
            tool_count += 1
            tool_name = (record.get("tool_name") or record.get("name") or "").lower()
            if tool_name in ("bash",):
                bash_count += 1
                tool_categories["bash"] += 1
            elif tool_name in ("read", "file_read"):
                tool_categories["file_read"] += 1
            elif tool_name in ("write", "file_write", "file_create"):
                tool_categories["file_write"] += 1
            elif tool_name in ("edit", "file_edit"):
                tool_categories["file_edit"] += 1
            elif tool_name in ("glob", "grep", "file_search"):
                search_count += 1
                tool_categories["file_search"] += 1
            elif tool_name in ("agent",):
                tool_categories["agent"] += 1
            elif tool_name.startswith("mcp__"):
                tool_categories["mcp"] += 1
            else:
                tool_categories["other"] += 1

            if rtype == "tool_result" and record.get("is_error"):
                has_errors = True

        elif rtype in ("text", "thinking", "assistant"):
            model_count += 1

        usage = record.get("usage")
        if isinstance(usage, dict):
            total_input_tokens += int(usage.get("input_tokens", 0))
            total_output_tokens += int(usage.get("output_tokens", 0))
            total_cache_read_tokens += int(usage.get("cache_read_input_tokens", 0))
            total_cache_creation_tokens += int(usage.get("cache_creation_input_tokens", 0))

        if record.get("notification_type") == "permission_prompt":
            permission_count += 1

        if hook_event == "SubagentStart":
            subagent_starts += 1
        elif hook_event == "SubagentStop":
            subagent_stops += 1

    total = tool_count + model_count
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
    total_tokens = total_input_tokens + total_output_tokens
    if total_tokens > 0:
        metrics["coding_agent.total_input_tokens"] = float(total_input_tokens)
        metrics["coding_agent.total_output_tokens"] = float(total_output_tokens)
        metrics["coding_agent.total_tokens"] = float(total_tokens)
    if total_cache_read_tokens > 0:
        metrics["coding_agent.total_cache_read_tokens"] = float(total_cache_read_tokens)
    if total_cache_creation_tokens > 0:
        metrics["coding_agent.total_cache_creation_tokens"] = float(total_cache_creation_tokens)
    metrics["coding_agent.subagent_balanced"] = (
        subagent_starts == 0 or subagent_starts == subagent_stops
    )

    return metrics
