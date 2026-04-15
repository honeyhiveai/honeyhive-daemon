"""Local state helpers for the HoneyHive daemon."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List

from .config import (
    ensure_state_layout,
    get_chat_histories_path,
    get_log_path,
    get_pending_tools_path,
    get_sessions_path,
    get_spool_path,
)


def log_message(message: str) -> None:
    """Append a log message to the daemon log."""
    ensure_state_layout()
    timestamp = datetime.now(timezone.utc).isoformat()
    with get_log_path().open("a", encoding="utf-8") as handle:
        handle.write(f"{timestamp} {message}\n")


def append_spool_event(event: Dict[str, Any]) -> None:
    """Append a failed event to the local spool."""
    ensure_state_layout()
    with get_spool_path().open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(event, sort_keys=True) + "\n")


def read_spool_events() -> List[Dict[str, Any]]:
    """Load pending events from the local spool."""
    path = get_spool_path()
    if not path.exists():
        return []
    events: List[Dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                log_message("skipped malformed spool line")
    return events


def replace_spool_events(events: List[Dict[str, Any]]) -> None:
    """Replace the current spool with unsent events."""
    ensure_state_layout()
    path = get_spool_path()
    with path.open("w", encoding="utf-8") as handle:
        for event in events:
            handle.write(json.dumps(event, sort_keys=True) + "\n")


def load_session_index() -> Dict[str, Dict[str, Any]]:
    """Load tracked Claude session state."""
    path = get_sessions_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        log_message("skipped malformed session index")
        return {}


def save_session_index(index: Dict[str, Dict[str, Any]]) -> None:
    """Persist tracked Claude session state."""
    ensure_state_layout()
    get_sessions_path().write_text(
        json.dumps(index, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def record_session_activity(
    session_id: str,
    *,
    transcript_path: str | None,
    last_activity_ms: int,
    ended: bool = False,
    session_end_event_id: str | None = None,
    session_start_exported: bool | None = None,
) -> Dict[str, Any]:
    """Update local state for one Claude session."""
    index = load_session_index()
    is_new = session_id not in index
    session = index.get(session_id, {})
    session["session_id"] = session_id
    session["event_id"] = session_id
    session["last_activity_ms"] = last_activity_ms
    if transcript_path:
        session["transcript_path"] = transcript_path
    if ended:
        session["ended"] = True
        # Reset artifact_pushed so the background loop re-uploads the
        # transcript if the session was resumed after a previous push.
        session["artifact_pushed"] = False
    if session_end_event_id:
        session["session_end_event_id"] = session_end_event_id
    if session_start_exported is not None:
        session["session_start_exported"] = session_start_exported
    session.setdefault("session_start_exported", False)
    session.setdefault("artifact_pushed", False)
    session["_is_new"] = is_new
    index[session_id] = session
    save_session_index(index)
    return session


def mark_session_artifact_pushed(session_id: str, pushed_at_ms: int) -> None:
    """Mark a session's transcript artifact as already pushed upstream."""
    index = load_session_index()
    session = index.get(session_id)
    if session is None:
        return
    session["artifact_pushed"] = True
    session["artifact_pushed_at_ms"] = pushed_at_ms
    index[session_id] = session
    save_session_index(index)


def _load_chat_histories() -> Dict[str, List[Dict[str, str]]]:
    """Load the per-session chat history index."""
    path = get_chat_histories_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        log_message("skipped malformed chat histories index")
        return {}


def _save_chat_histories(index: Dict[str, List[Dict[str, str]]]) -> None:
    """Persist the per-session chat history index."""
    ensure_state_layout()
    get_chat_histories_path().write_text(
        json.dumps(index, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def get_chat_history(session_id: str) -> List[Dict[str, str]]:
    """Return the accumulated chat history for a session."""
    return list(_load_chat_histories().get(session_id, []))


def append_chat_history(
    session_id: str, role: str, content: str
) -> List[Dict[str, str]]:
    """Append a message to a session's chat history and return the history before appending."""
    index = _load_chat_histories()
    history = list(index.get(session_id, []))
    history_before = list(history)
    history.append({"role": role, "content": content})
    index[session_id] = history
    _save_chat_histories(index)
    return history_before


def _load_pending_tools() -> Dict[str, Dict[str, Any]]:
    """Load the pending tool events index."""
    path = get_pending_tools_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        log_message("skipped malformed pending tools index")
        return {}


def _save_pending_tools(index: Dict[str, Dict[str, Any]]) -> None:
    """Persist the pending tool events index."""
    ensure_state_layout()
    get_pending_tools_path().write_text(
        json.dumps(index, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def buffer_pending_tool_event(
    session_id: str, tool_use_id: str, event: Dict[str, Any]
) -> None:
    """Buffer a pre-phase tool event waiting for its post-phase counterpart."""
    index = _load_pending_tools()
    key = f"{session_id}:{tool_use_id}"
    index[key] = event
    _save_pending_tools(index)


def pop_pending_tool_event(
    session_id: str, tool_use_id: str
) -> Dict[str, Any] | None:
    """Pop a buffered pre-phase tool event for merging with its post-phase."""
    index = _load_pending_tools()
    key = f"{session_id}:{tool_use_id}"
    event = index.pop(key, None)
    if event is not None:
        _save_pending_tools(index)
    return event


def get_expired_tool_events(*, now_ms: int, timeout_ms: int = 60_000) -> List[Dict[str, Any]]:
    """Return and remove tool events buffered longer than timeout_ms."""
    index = _load_pending_tools()
    expired: List[Dict[str, Any]] = []
    remaining: Dict[str, Dict[str, Any]] = {}
    for key, event in index.items():
        if now_ms - int(event.get("start_time", 0)) >= timeout_ms:
            expired.append(event)
        else:
            remaining[key] = event
    if expired:
        _save_pending_tools(remaining)
    return expired


def get_sessions_needing_artifact(
    *,
    now_ms: int,
    idle_threshold_ms: int,
) -> List[Dict[str, Any]]:
    """Return sessions that should have their transcript artifact pushed."""
    sessions = load_session_index()
    ready: List[Dict[str, Any]] = []
    for session in sessions.values():
        if session.get("artifact_pushed"):
            continue
        transcript_path = session.get("transcript_path")
        if not transcript_path:
            continue
        last_activity_ms = int(session.get("last_activity_ms", 0))
        if session.get("ended") or now_ms - last_activity_ms >= idle_threshold_ms:
            ready.append(session)
    return ready
