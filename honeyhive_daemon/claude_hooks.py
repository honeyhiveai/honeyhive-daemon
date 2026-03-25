"""Claude Code hook installation and normalization."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from .git_hooks import find_git_root, get_git_revision
from .mappings import (
    load_claude_code_mapping,
    resolve_event_mapping,
    resolve_payload_path,
)


def get_hook_command() -> str:
    """Return the command registered in Claude settings."""
    return "honeyhive-daemon ingest claude-hook"


def install_claude_hooks(settings_path: Path, command: str) -> bool:
    """Install user-level Claude hooks idempotently."""
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    settings: Dict[str, Any] = {}
    if settings_path.exists():
        raw = settings_path.read_text(encoding="utf-8").strip()
        if raw:
            settings = json.loads(raw)

    hooks = settings.setdefault("hooks", {})
    changed = False
    mapping = load_claude_code_mapping()
    desired_by_event: Dict[str, list[Optional[str]]] = {}

    for registration in mapping["hook_registrations"]:
        desired_by_event.setdefault(registration["hook_event_name"], []).append(
            registration.get("matcher")
        )

    for event_name, matchers in desired_by_event.items():
        entries = hooks.setdefault(event_name, [])
        changed |= _sync_hook_entries(entries, command, matchers)

    if changed:
        settings_path.write_text(
            json.dumps(settings, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    return changed


def _sync_hook_entries(
    entries: list[Dict[str, Any]],
    command: str,
    desired_matchers: list[Optional[str]],
) -> bool:
    normalized_original = json.loads(json.dumps(entries))
    preserved_entries: list[Dict[str, Any]] = []

    for entry in entries:
        remaining_hooks = [
            hook
            for hook in entry.get("hooks", [])
            if not (
                hook.get("type") == "command" and hook.get("command") == command
            )
        ]
        if not remaining_hooks:
            continue
        updated_entry = dict(entry)
        updated_entry["hooks"] = remaining_hooks
        preserved_entries.append(updated_entry)

    for matcher in desired_matchers:
        entry: Dict[str, Any] = {
            "hooks": [{"type": "command", "command": command}],
        }
        if matcher is not None:
            entry["matcher"] = matcher
        preserved_entries.append(entry)

    if preserved_entries != normalized_original:
        entries[:] = preserved_entries
        return True
    return False


def normalize_claude_payload(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Normalize a Claude hook payload into a daemon event."""
    mapping = load_claude_code_mapping()
    hook_event_name = payload.get("hook_event_name")
    session_id = str(payload.get("session_id") or uuid.uuid4())
    cwd_value = payload.get("cwd")
    cwd = Path(cwd_value) if cwd_value else None
    repo_root = find_git_root(cwd) if cwd else None
    git_revision = get_git_revision(repo_root) if repo_root else None

    event_mapping = mapping["event_mappings"].get(hook_event_name)
    if event_mapping is None:
        return None
    event_mapping = resolve_event_mapping(event_mapping, payload)
    if event_mapping is None:
        return None

    metadata = _build_metadata(
        payload=payload,
        mapping=mapping,
        event_mapping=event_mapping,
        session_id=session_id,
        cwd=cwd,
        repo_root=repo_root,
        git_revision=git_revision,
    )
    # Resolve dynamic event name template if present
    event_name_template = event_mapping.get("event_name_template")
    if event_name_template:
        try:
            event_mapping = dict(event_mapping)
            event_mapping["event_name"] = event_name_template.format_map(
                _SafeFormatDict(payload)
            )
        except (KeyError, ValueError):
            pass  # keep static event_name as fallback

    event = _build_event(
        event_mapping=event_mapping,
        session_id=session_id,
        metadata=metadata,
        inputs=_build_data_section(payload, event_mapping.get("inputs", {})),
        outputs=_build_data_section(payload, event_mapping.get("outputs", {})),
        raw_payload=payload,
    )

    # Attach tool_use_id and hook phase for pre+post linking
    tool_use_id = payload.get("tool_use_id")
    if tool_use_id:
        event["tool_use_id"] = tool_use_id
    if hook_event_name in ("PreToolUse", "PermissionRequest"):
        event["_hook_phase"] = "pre"
    elif hook_event_name in ("PostToolUse", "PostToolUseFailure"):
        event["_hook_phase"] = "post"
        if hook_event_name == "PostToolUseFailure":
            event["_hook_failure"] = True

    return event


def _build_metadata(
    *,
    payload: Dict[str, Any],
    mapping: Dict[str, Any],
    event_mapping: Dict[str, Any],
    session_id: str,
    cwd: Optional[Path],
    repo_root: Optional[Path],
    git_revision: Optional[str],
) -> Dict[str, Any]:
    metadata: Dict[str, Any] = {}
    metadata.update(mapping["common_metadata"]["static"])

    for key, payload_path in mapping["common_metadata"]["from_payload"].items():
        value = resolve_payload_path(payload, payload_path)
        if value is not None:
            metadata[key] = value

    metadata["agent.session_id"] = session_id

    derived_values = {
        "repo_root": str(repo_root) if repo_root is not None else None,
        "git_revision": git_revision,
    }
    for key, derived_name in mapping["derived_metadata"].items():
        value = derived_values.get(derived_name)
        if value is not None:
            metadata[key] = value

    event_metadata = event_mapping.get("metadata", {})
    metadata.update(event_metadata.get("static", {}))
    for key, payload_path in event_metadata.get("from_payload", {}).items():
        value = resolve_payload_path(payload, payload_path)
        if value is not None:
            metadata[key] = value

    if cwd is not None and "cwd" not in metadata:
        metadata["cwd"] = str(cwd)

    return metadata


def _build_event(
    *,
    event_mapping: Dict[str, Any],
    session_id: str,
    metadata: Dict[str, Any],
    inputs: Dict[str, Any],
    outputs: Dict[str, Any],
    raw_payload: Dict[str, Any],
) -> Dict[str, Any]:
    timestamp = datetime.now(timezone.utc)
    timestamp_ms = int(timestamp.timestamp() * 1000)
    event_id = (
        session_id
        if event_mapping.get("event_id_strategy") == "session_id"
        else str(uuid.uuid4())
    )
    parent_id: Optional[str]
    if event_mapping.get("parent_id_strategy") == "session_root":
        parent_id = session_id
    else:
        parent_id = None

    return {
        "event_id": event_id,
        "session_id": session_id,
        "event_type": event_mapping["event_type"],
        "event_name": event_mapping["event_name"],
        "start_time": timestamp_ms,
        "end_time": timestamp_ms,
        "duration": 0,
        "metadata": metadata,
        "inputs": inputs,
        "outputs": outputs,
        "raw": raw_payload,
        "parent_id": parent_id,
    }


class _SafeFormatDict(dict):
    """Dict wrapper that returns the key itself for missing format fields."""

    def __init__(self, data: Dict[str, Any]) -> None:
        super().__init__(data)

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


def _build_data_section(
    payload: Dict[str, Any], section_mapping: Dict[str, Any]
) -> Dict[str, Any]:
    section: Dict[str, Any] = {}
    section.update(section_mapping.get("static", {}))
    for key, payload_path in section_mapping.get("from_payload", {}).items():
        value = resolve_payload_path(payload, payload_path)
        if value is not None:
            section[key] = value
    return section
