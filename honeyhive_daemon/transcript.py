"""Transcript JSONL parsing helpers for extracting thinking and usage context."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional


# In-memory cache: transcript_path -> list of parsed records
_transcript_cache: Dict[str, List[Dict[str, Any]]] = {}


def _load_transcript(transcript_path: str) -> List[Dict[str, Any]]:
    """Load and cache a transcript JSONL file.

    The cache is keyed by path and re-reads if the file has grown since last
    load (new records appended during a session).
    """
    path = Path(transcript_path)
    if not path.exists() or not path.is_file():
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []

    cached = _transcript_cache.get(transcript_path)
    if cached is not None and len(cached) >= len(lines):
        return cached

    records: List[Dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    _transcript_cache[transcript_path] = records
    return records


# ---------------------------------------------------------------------------
# Unified context extraction
# ---------------------------------------------------------------------------

class TranscriptContext:
    """Context extracted from the transcript for a specific event."""

    __slots__ = ("thinking", "usage", "model", "request_id")

    def __init__(self) -> None:
        self.thinking: Optional[str] = None
        self.usage: Optional[Dict[str, Any]] = None
        self.model: Optional[str] = None
        self.request_id: Optional[str] = None

    def has_data(self) -> bool:
        return any(v is not None for v in (self.thinking, self.usage, self.model))


def get_context_for_tool_use(
    transcript_path: str, tool_use_id: str
) -> TranscriptContext:
    """Extract thinking, usage, and model for a specific tool use.

    Walks the transcript to find the assistant message containing the tool_use
    block, extracts usage/model from it, and searches backwards from there for
    the nearest thinking block (which may be in a prior streaming chunk of the
    same message, identified by sharing the same message id).
    """
    ctx = TranscriptContext()
    records = _load_transcript(transcript_path)
    if not records:
        return ctx

    tool_use_idx: Optional[int] = None
    for i, record in enumerate(records):
        if _record_contains_tool_use_id(record, tool_use_id):
            tool_use_idx = i
            break

    if tool_use_idx is None:
        return ctx

    # Extract usage/model from the tool_use record (or its message wrapper)
    _extract_usage_and_model(records[tool_use_idx], ctx)

    # Search backwards for thinking block
    for i in range(tool_use_idx, -1, -1):
        thinking = _extract_thinking(records[i])
        if thinking is not None:
            ctx.thinking = thinking
            break
        # Stop searching if we hit a non-assistant record (previous turn boundary)
        if i < tool_use_idx and _is_turn_boundary(records[i]):
            break

    return ctx


def get_context_for_latest_turn(transcript_path: str) -> TranscriptContext:
    """Extract thinking, usage, and model for the most recent assistant turn.

    Walks backwards from the end of the transcript to find the last assistant
    message with a terminal stop_reason (end_turn, stop_sequence, or max_tokens).
    Collects thinking and usage from it and any preceding streaming chunks.
    """
    ctx = TranscriptContext()
    records = _load_transcript(transcript_path)
    if not records:
        return ctx

    # Find the last terminal assistant record (the one with stop_reason set)
    terminal_idx: Optional[int] = None
    for i in range(len(records) - 1, -1, -1):
        record = records[i]
        if _is_assistant_record(record):
            stop_reason = _get_stop_reason(record)
            # Terminal: end_turn, stop_sequence, max_tokens — NOT tool_use or null
            if stop_reason and stop_reason != "tool_use":
                terminal_idx = i
                break
            # Also accept the last assistant record if no stop_reason found
            # (could be a streaming chunk that's the final record)
            if terminal_idx is None and stop_reason is None:
                terminal_idx = i

    if terminal_idx is None:
        # Fallback: just find the last assistant record with usage
        for i in range(len(records) - 1, -1, -1):
            if _is_assistant_record(records[i]):
                terminal_idx = i
                break

    if terminal_idx is None:
        return ctx

    # Extract usage/model from the terminal record
    _extract_usage_and_model(records[terminal_idx], ctx)

    # Search backwards for thinking block
    for i in range(terminal_idx, -1, -1):
        thinking = _extract_thinking(records[i])
        if thinking is not None:
            ctx.thinking = thinking
            break
        if i < terminal_idx and _is_turn_boundary(records[i]):
            break

    return ctx


# ---------------------------------------------------------------------------
# Legacy wrapper (kept for compatibility)
# ---------------------------------------------------------------------------

def get_thinking_for_tool_use(
    transcript_path: str, tool_use_id: str
) -> Optional[str]:
    """Extract the thinking/reasoning block that precedes a tool use."""
    return get_context_for_tool_use(transcript_path, tool_use_id).thinking


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _is_assistant_record(record: Dict[str, Any]) -> bool:
    """Check if a transcript record is an assistant message."""
    if record.get("type") == "assistant":
        return True
    if record.get("role") == "assistant":
        return True
    message = record.get("message")
    if isinstance(message, dict) and message.get("role") == "assistant":
        return True
    return False


def _get_stop_reason(record: Dict[str, Any]) -> Optional[str]:
    """Extract stop_reason from a transcript record."""
    message = record.get("message", record)
    return message.get("stop_reason")


def _is_turn_boundary(record: Dict[str, Any]) -> bool:
    """Check if a record represents a turn boundary (user message or tool result)."""
    if record.get("type") == "user":
        return True
    if record.get("role") == "user":
        return True
    message = record.get("message")
    if isinstance(message, dict) and message.get("role") == "user":
        return True
    return False


def _record_contains_tool_use_id(record: Dict[str, Any], tool_use_id: str) -> bool:
    """Check if a transcript record references the given tool_use_id."""
    message = record.get("message", record)
    content = message.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict):
                if block.get("id") == tool_use_id:
                    return True
                if block.get("tool_use_id") == tool_use_id:
                    return True
    if record.get("tool_use_id") == tool_use_id:
        return True
    if record.get("id") == tool_use_id:
        return True
    return False


def _extract_thinking(record: Dict[str, Any]) -> Optional[str]:
    """Extract thinking text from a transcript record."""
    message = record.get("message", record)
    content = message.get("content")
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "thinking":
                thinking_text = block.get("thinking") or block.get("text")
                if thinking_text:
                    return str(thinking_text)
    return None


def _extract_usage_and_model(record: Dict[str, Any], ctx: TranscriptContext) -> None:
    """Extract usage dict, model, and requestId from a transcript record into ctx."""
    message = record.get("message", record)
    usage = message.get("usage")
    if isinstance(usage, dict):
        ctx.usage = {
            k: v for k, v in usage.items()
            if k != "cache_creation" and v is not None
        }
    model = message.get("model")
    if model:
        ctx.model = str(model)
    request_id = record.get("requestId") or message.get("requestId")
    if request_id:
        ctx.request_id = str(request_id)


def clear_transcript_cache() -> None:
    """Clear the in-memory transcript cache."""
    _transcript_cache.clear()
