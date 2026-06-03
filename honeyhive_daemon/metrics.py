"""Client-side session metrics and transcript helpers."""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any, Optional


def _extract_usage(record: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Return usage dict from a transcript record (top-level or nested in message)."""
    usage = record.get("usage")
    if isinstance(usage, dict):
        return usage
    message = record.get("message")
    if isinstance(message, dict):
        usage = message.get("usage")
        if isinstance(usage, dict):
            return usage
    return None


def _extract_request_id(record: dict[str, Any]) -> Optional[str]:
    """Return API request id from a transcript record, if present."""
    request_id = record.get("requestId")
    if request_id:
        return str(request_id)
    message = record.get("message")
    if isinstance(message, dict) and message.get("requestId"):
        return str(message["requestId"])
    return None


def _message_content_blocks(record: dict[str, Any]) -> list[Any]:
    message = record.get("message")
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, list):
            return content
    return []


def _tool_names_from_record(record: dict[str, Any]) -> list[str]:
    """Return tool names invoked by a transcript record (one entry per tool_use)."""
    rtype = record.get("type", "")
    if rtype == "tool_use":
        return [str(record.get("tool_name") or record.get("name") or "other")]
    if rtype == "assistant":
        names: list[str] = []
        for block in _message_content_blocks(record):
            if isinstance(block, dict) and block.get("type") == "tool_use":
                names.append(str(block.get("name") or "other"))
        return names
    return []


def _tool_result_errors_from_record(record: dict[str, Any]) -> list[bool]:
    """Return is_error flags from tool_result blocks in a transcript record."""
    if record.get("type") == "tool_result":
        return [bool(record.get("is_error"))]
    if record.get("type") == "user":
        return [
            bool(block.get("is_error"))
            for block in _message_content_blocks(record)
            if isinstance(block, dict) and block.get("type") == "tool_result"
        ]
    return []


def _categorize_tool_name(
    tool_name: str,
    tool_categories: dict[str, int],
    *,
    bash_count_ref: list[int],
    search_count_ref: list[int],
) -> None:
    name = tool_name.lower()
    if name in ("bash",):
        bash_count_ref[0] += 1
        tool_categories["bash"] += 1
    elif name in ("read", "file_read"):
        tool_categories["file_read"] += 1
    elif name in ("write", "file_write", "file_create"):
        tool_categories["file_write"] += 1
    elif name in ("edit", "file_edit"):
        tool_categories["file_edit"] += 1
    elif name in ("glob", "grep", "file_search"):
        search_count_ref[0] += 1
        tool_categories["file_search"] += 1
    elif name in ("agent",):
        tool_categories["agent"] += 1
    elif name.startswith("mcp__"):
        tool_categories["mcp"] += 1
    else:
        tool_categories["other"] += 1


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
    counted_request_ids: set[str] = set()

    for record in transcript_content:
        if not isinstance(record, dict):
            continue

        rtype = record.get("type", "")
        hook_event = record.get("hook_event_name", "")

        bash_ref = [bash_count]
        search_ref = [search_count]
        for tool_name in _tool_names_from_record(record):
            tool_count += 1
            _categorize_tool_name(
                tool_name, tool_categories, bash_count_ref=bash_ref, search_count_ref=search_ref
            )
        bash_count = bash_ref[0]
        search_count = search_ref[0]

        for is_error in _tool_result_errors_from_record(record):
            if is_error:
                has_errors = True

        if rtype in ("text", "thinking", "assistant"):
            model_count += 1

        usage = _extract_usage(record)
        if isinstance(usage, dict):
            request_id = _extract_request_id(record)
            if request_id:
                if request_id in counted_request_ids:
                    continue
                counted_request_ids.add(request_id)
            total_input_tokens += int(usage.get("input_tokens", 0))
            total_output_tokens += int(usage.get("output_tokens", 0))
            total_cache_read_tokens += int(usage.get("cache_read_input_tokens", 0))
            total_cache_creation_tokens += int(
                usage.get("cache_creation_input_tokens", 0)
            )

        if record.get("notification_type") == "permission_prompt":
            permission_count += 1

        if hook_event == "SubagentStart":
            subagent_starts += 1
        elif hook_event == "SubagentStop":
            subagent_stops += 1

    total = tool_count + model_count
    metrics: dict[str, object] = {
        "coding_agent.event_count": float(len(transcript_content)),
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
