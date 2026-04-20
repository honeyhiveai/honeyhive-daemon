"""Output filters for the HoneyHive daemon.

Reads a filters.json config and decides whether to export, redact, or drop
each event before it leaves the machine. Filters are applied in
`ingest_claude_hook` and `_push_pending_session_artifacts`.

Filter config lives at ``$HH_DAEMON_HOME/state/filters.json`` (or override
with ``$HH_DAEMON_FILTERS``). If no config exists, everything passes.

Schema::

    {
      "enabled": true,

      // Session-level gates (glob on session name from transcript)
      "session_name_include": ["waggle-*"],
      "session_name_exclude": ["waggle-taste-*"],

      // Event-type gates
      "exclude_event_types": ["tool", "model"],
      "only_session_bookends": false,

      // Hard content filters — drop events touching these paths/commands
      "path_exclude": [
          "**/.env", "**/credentials*", "**/secrets/**",
          "**/node_modules/**", "**/__pycache__/**"
      ],
      "path_include": [],

      // Command filters — drop bash events whose command matches
      "command_exclude": [
          ".*api[_-]?key.*", ".*secret.*", ".*password.*", ".*token.*"
      ],

      // Redaction — keep the event but strip inputs/outputs content
      "redact_paths": ["**/state/**", "**/*.json"],

      // Transcript limits
      "max_transcript_events": 0
    }
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import get_state_dir


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

_DEFAULT_CONFIG: Dict[str, Any] = {
    "enabled": True,
    "session_name_include": [],
    "session_name_exclude": [],
    "exclude_event_types": [],
    "only_session_bookends": False,
    "path_exclude": [
        "**/.env",
        "**/.env.*",
        "**/credentials*",
        "**/secrets/**",
        "**/*.key",
        "**/*.pem",
    ],
    "path_include": [],
    "command_exclude": [],
    "redact_paths": [],
    "max_transcript_events": 0,
}


def _get_filters_path() -> Path:
    override = os.getenv("HH_DAEMON_FILTERS")
    if override:
        return Path(override).expanduser()
    return get_state_dir() / "filters.json"


def load_filters() -> Dict[str, Any]:
    """Load filter config, falling back to defaults."""
    path = _get_filters_path()
    if not path.exists():
        return dict(_DEFAULT_CONFIG)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        merged = dict(_DEFAULT_CONFIG)
        merged.update(raw)
        return merged
    except (json.JSONDecodeError, OSError):
        return dict(_DEFAULT_CONFIG)


def save_default_filters() -> Path:
    """Write default filter config if none exists. Returns path."""
    path = _get_filters_path()
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(_DEFAULT_CONFIG, indent=2) + "\n", encoding="utf-8"
        )
    return path


# ---------------------------------------------------------------------------
# Matching helpers
# ---------------------------------------------------------------------------

def _glob_match(value: str, patterns: List[str]) -> bool:
    """Check if value matches any of the glob patterns."""
    for pattern in patterns:
        if fnmatch.fnmatch(value, pattern):
            return True
    return False


def _regex_match(value: str, patterns: List[str]) -> bool:
    """Check if value matches any regex pattern (case-insensitive)."""
    for pattern in patterns:
        try:
            if re.search(pattern, value, re.IGNORECASE):
                return True
        except re.error:
            continue
    return False


def _extract_file_paths(event: Dict[str, Any]) -> List[str]:
    """Pull all file paths from an event's inputs, metadata, and raw payload."""
    paths: List[str] = []

    # metadata.file.path
    meta = event.get("metadata") or {}
    fp = meta.get("file.path")
    if fp:
        paths.append(str(fp))

    # inputs.tool_input.file_path
    inputs = event.get("inputs") or {}
    tool_input = inputs.get("tool_input") or {}
    if isinstance(tool_input, dict):
        for key in ("file_path", "path", "file"):
            v = tool_input.get(key)
            if v:
                paths.append(str(v))
        # Glob/Grep pattern field may contain a path
        pattern = tool_input.get("pattern", "")
        if isinstance(pattern, str) and "/" in pattern:
            paths.append(pattern)

    # raw payload fallback
    raw = event.get("raw") or {}
    if isinstance(raw, dict):
        raw_input = raw.get("tool_input") or {}
        if isinstance(raw_input, dict):
            for key in ("file_path", "path"):
                v = raw_input.get(key)
                if v and str(v) not in paths:
                    paths.append(str(v))

    return paths


def _extract_command(event: Dict[str, Any]) -> Optional[str]:
    """Pull the bash command from a tool event."""
    meta = event.get("metadata") or {}
    cmd = meta.get("tool.command")
    if cmd:
        return str(cmd)

    inputs = event.get("inputs") or {}
    tool_input = inputs.get("tool_input") or {}
    if isinstance(tool_input, dict):
        cmd = tool_input.get("command")
        if cmd:
            return str(cmd)

    return None


# ---------------------------------------------------------------------------
# Filter verdicts
# ---------------------------------------------------------------------------

class FilterVerdict:
    """Result of applying filters to an event."""
    __slots__ = ("action", "reason")

    EXPORT = "export"
    DROP = "drop"
    REDACT = "redact"

    def __init__(self, action: str, reason: str = "") -> None:
        self.action = action
        self.reason = reason

    @property
    def should_export(self) -> bool:
        return self.action in (self.EXPORT, self.REDACT)

    @property
    def should_redact(self) -> bool:
        return self.action == self.REDACT


def apply_filters(
    event: Dict[str, Any],
    filters: Dict[str, Any],
    session_name: Optional[str] = None,
) -> FilterVerdict:
    """Decide whether to export, redact, or drop an event.

    Parameters
    ----------
    event : dict
        The normalized daemon event (after ``normalize_claude_payload``).
    filters : dict
        Loaded filter config.
    session_name : str, optional
        Session name resolved from transcript. If ``None``, the session
        name filter is skipped.
    """
    if not filters.get("enabled", True):
        return FilterVerdict(FilterVerdict.EXPORT)

    event_name = event.get("event_name", "")
    event_type = event.get("event_type", "")

    # ── Session name gates ────────────────────────────────────
    if session_name is not None:
        include = filters.get("session_name_include") or []
        if include and not _glob_match(session_name, include):
            return FilterVerdict(FilterVerdict.DROP, f"session_name '{session_name}' not in include list")

        exclude = filters.get("session_name_exclude") or []
        if exclude and _glob_match(session_name, exclude):
            return FilterVerdict(FilterVerdict.DROP, f"session_name '{session_name}' matches exclude")

    # ── Session bookends mode ─────────────────────────────────
    if filters.get("only_session_bookends"):
        if event_name not in ("session.start", "session.end"):
            return FilterVerdict(FilterVerdict.DROP, "only_session_bookends=true")

    # ── Event type exclusion ──────────────────────────────────
    exclude_types = filters.get("exclude_event_types") or []
    if event_type in exclude_types:
        return FilterVerdict(FilterVerdict.DROP, f"event_type '{event_type}' excluded")

    # ── Path filters (hard drop) ──────────────────────────────
    file_paths = _extract_file_paths(event)
    path_exclude = filters.get("path_exclude") or []
    if path_exclude and file_paths:
        for fp in file_paths:
            if _glob_match(fp, path_exclude):
                return FilterVerdict(FilterVerdict.DROP, f"path '{fp}' matches path_exclude")

    # Path include: if set, at least one path must match
    path_include = filters.get("path_include") or []
    if path_include and file_paths:
        if not any(_glob_match(fp, path_include) for fp in file_paths):
            return FilterVerdict(FilterVerdict.DROP, "no file path matches path_include")

    # ── Command filters (hard drop) ───────────────────────────
    command = _extract_command(event)
    command_exclude = filters.get("command_exclude") or []
    if command and command_exclude:
        if _regex_match(command, command_exclude):
            return FilterVerdict(FilterVerdict.DROP, "command matches command_exclude")

    # ── Redaction check ───────────────────────────────────────
    redact_paths = filters.get("redact_paths") or []
    if redact_paths and file_paths:
        for fp in file_paths:
            if _glob_match(fp, redact_paths):
                return FilterVerdict(FilterVerdict.REDACT, f"path '{fp}' matches redact_paths")

    return FilterVerdict(FilterVerdict.EXPORT)


def redact_event(event: Dict[str, Any]) -> Dict[str, Any]:
    """Strip sensitive content from an event while keeping structure.

    Keeps: event_id, session_id, event_type, event_name, start_time,
    end_time, duration, metadata, parent_id.
    Strips: inputs content, outputs content, raw payload.
    """
    redacted = {
        "event_id": event.get("event_id"),
        "session_id": event.get("session_id"),
        "event_type": event.get("event_type"),
        "event_name": event.get("event_name"),
        "start_time": event.get("start_time"),
        "end_time": event.get("end_time"),
        "duration": event.get("duration", 0),
        "metadata": event.get("metadata", {}),
        "parent_id": event.get("parent_id"),
    }
    # Keep input keys but replace values
    inputs = event.get("inputs") or {}
    redacted["inputs"] = {k: "[REDACTED]" for k in inputs}
    outputs = event.get("outputs") or {}
    redacted["outputs"] = {k: "[REDACTED]" for k in outputs}
    redacted["raw"] = "[REDACTED]"

    # Preserve tool_use_id and hook phase for pre+post linking
    if "tool_use_id" in event:
        redacted["tool_use_id"] = event["tool_use_id"]
    if "_hook_phase" in event:
        redacted["_hook_phase"] = event["_hook_phase"]
    if "_hook_failure" in event:
        redacted["_hook_failure"] = event["_hook_failure"]

    return redacted


def filter_transcript_content(
    content: list,
    filters: Dict[str, Any],
) -> list:
    """Apply filters to a transcript content array before artifact push.

    Drops records that match path/command exclusions and enforces
    ``max_transcript_events``.
    """
    if not filters.get("enabled", True):
        return content

    path_exclude = filters.get("path_exclude") or []
    command_exclude = filters.get("command_exclude") or []
    redact_paths_list = filters.get("redact_paths") or []
    max_events = filters.get("max_transcript_events", 0)

    filtered: list = []
    for record in content:
        if not isinstance(record, dict):
            filtered.append(record)
            continue

        # Extract paths from transcript record
        tool_input = record.get("tool_input") or record.get("input") or {}
        file_path = None
        if isinstance(tool_input, dict):
            file_path = tool_input.get("file_path") or tool_input.get("path")

        # Path exclude check
        if file_path and path_exclude and _glob_match(str(file_path), path_exclude):
            continue

        # Command exclude check
        cmd = None
        if isinstance(tool_input, dict):
            cmd = tool_input.get("command")
        if cmd and command_exclude and _regex_match(str(cmd), command_exclude):
            continue

        # Redact content for matching paths
        if file_path and redact_paths_list and _glob_match(str(file_path), redact_paths_list):
            record = dict(record)
            if "tool_response" in record:
                record["tool_response"] = "[REDACTED]"
            if "output" in record:
                record["output"] = "[REDACTED]"

        filtered.append(record)

    # Apply max events cap
    if max_events > 0 and len(filtered) > max_events:
        filtered = filtered[:max_events]

    return filtered
