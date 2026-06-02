"""Local state helpers for the HoneyHive daemon."""

from __future__ import annotations

import fcntl
import json
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Dict, Iterator, List

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


@contextmanager
def _locked_text_file(path):
    """Open a state text file under an exclusive lock."""
    ensure_state_layout()
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.seek(0)
            yield handle
            handle.flush()
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def _locked_json_mapping(path, malformed_message: str) -> Iterator[Dict[str, Any]]:
    """Load and save a JSON object state file under an exclusive lock."""
    ensure_state_layout()
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.seek(0)
            raw = handle.read()
            try:
                data = json.loads(raw) if raw.strip() else {}
            except json.JSONDecodeError:
                log_message(malformed_message)
                data = {}
            yield data
            handle.seek(0)
            handle.truncate()
            handle.write(json.dumps(data, indent=2, sort_keys=True) + "\n")
            handle.flush()
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _parse_spool_lines(lines: list[str]) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            log_message("skipped malformed spool line")
    return events


def append_spool_event(event: Dict[str, Any]) -> None:
    """Append a failed event to the local spool."""
    with _locked_text_file(get_spool_path()) as handle:
        handle.seek(0, 2)
        handle.write(json.dumps(event, sort_keys=True) + "\n")


def read_spool_events() -> List[Dict[str, Any]]:
    """Load pending events from the local spool."""
    path = get_spool_path()
    if not path.exists():
        return []
    with _locked_text_file(path) as handle:
        return _parse_spool_lines(handle.readlines())


def drain_spool_events() -> List[Dict[str, Any]]:
    """Atomically return and clear pending spool events."""
    with _locked_text_file(get_spool_path()) as handle:
        events = _parse_spool_lines(handle.readlines())
        handle.seek(0)
        handle.truncate()
        return events


def replace_spool_events(events: List[Dict[str, Any]]) -> None:
    """Replace the current spool with unsent events."""
    with _locked_text_file(get_spool_path()) as handle:
        handle.seek(0)
        handle.truncate()
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


@contextmanager
def _locked_session_index() -> Iterator[Dict[str, Dict[str, Any]]]:
    """Load the session index under an exclusive lock for read-modify-write."""
    ensure_state_layout()
    path = get_sessions_path()
    if not path.exists():
        path.write_text("{}\n", encoding="utf-8")
    with path.open("r+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            raw = handle.read()
            try:
                index = json.loads(raw) if raw.strip() else {}
            except json.JSONDecodeError:
                log_message("skipped malformed session index")
                index = {}
            yield index
            handle.seek(0)
            handle.truncate()
            handle.write(json.dumps(index, indent=2, sort_keys=True) + "\n")
            handle.flush()
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def record_session_activity(
    session_id: str,
    *,
    transcript_path: str | None,
    last_activity_ms: int,
    ended: bool = False,
    session_end_event_id: str | None = None,
    session_start_exported: bool | None = None,
    cwd: str | None = None,
    session_name: str | None = None,
) -> Dict[str, Any]:
    """Update local state for one Claude session."""
    with _locked_session_index() as index:
        is_new = session_id not in index
        session = index.get(session_id, {})
        session["session_id"] = session_id
        session["event_id"] = session_id
        session["last_activity_ms"] = last_activity_ms
        if transcript_path:
            session["transcript_path"] = transcript_path
        if cwd:
            session["cwd"] = cwd
        if session_name:
            session["session_name"] = session_name
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
        return dict(session)


def mark_session_artifact_pushed(session_id: str, pushed_at_ms: int) -> None:
    """Mark a session's transcript artifact as already pushed upstream."""
    with _locked_session_index() as index:
        session = index.get(session_id)
        if session is None:
            return
        session["artifact_pushed"] = True
        session["artifact_pushed_at_ms"] = pushed_at_ms
        index[session_id] = session


def _load_chat_histories() -> Dict[str, List[Dict[str, str]]]:
    """Load the per-session chat history index."""
    path = get_chat_histories_path()
    if not path.exists():
        return {}
    with _locked_json_mapping(path, "skipped malformed chat histories index") as index:
        return dict(index)


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


def claim_skills_listed_export(session_id: str) -> bool:
    """Atomically claim the one chain.skills.listed export slot for a session."""
    with _locked_session_index() as index:
        session = index.setdefault(
            session_id,
            {
                "session_id": session_id,
                "event_id": session_id,
                "session_start_exported": False,
                "artifact_pushed": False,
            },
        )
        if session.get("skills_listed_exported"):
            return False
        session["skills_listed_exported"] = True
        return True


def claim_tool_usage_request_id(session_id: str, request_id: str) -> bool:
    """Return True if usage for this API request may be attached to a tool event.

    Claude Code may emit multiple tool events from one API call; usage should
    appear on only the first tool event for that request_id.
    """
    if not request_id:
        return True
    with _locked_session_index() as index:
        session = index.setdefault(
            session_id,
            {
                "session_id": session_id,
                "event_id": session_id,
                "session_start_exported": False,
                "artifact_pushed": False,
            },
        )
        used = set(session.get("tool_usage_request_ids", []))
        if request_id in used:
            return False
        used.add(request_id)
        session["tool_usage_request_ids"] = sorted(used)
        index[session_id] = session
        return True


def append_chat_history(
    session_id: str, role: str, content: str
) -> List[Dict[str, str]]:
    """Append a message to a session's chat history and return the history including the new message."""
    with _locked_json_mapping(
        get_chat_histories_path(), "skipped malformed chat histories index"
    ) as index:
        history = list(index.get(session_id, []))
        history.append({"role": role, "content": content})
        index[session_id] = history
        return list(history)


def increment_session_artifact_retry(session_id: str) -> int:
    """Increment and return the artifact retry count for a session."""
    with _locked_session_index() as index:
        session = index.get(session_id)
        if session is None:
            return 0
        count = session.get("artifact_retry_count", 0) + 1
        session["artifact_retry_count"] = count
        index[session_id] = session
        return count


def _load_pending_tools() -> Dict[str, Dict[str, Any]]:
    """Load the pending tool events index."""
    path = get_pending_tools_path()
    if not path.exists():
        return {}
    with _locked_json_mapping(path, "skipped malformed pending tools index") as index:
        return dict(index)


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
    with _locked_json_mapping(
        get_pending_tools_path(), "skipped malformed pending tools index"
    ) as index:
        key = f"{session_id}:{tool_use_id}"
        index[key] = event


def pop_pending_tool_event(
    session_id: str, tool_use_id: str
) -> Dict[str, Any] | None:
    """Pop a buffered pre-phase tool event for merging with its post-phase."""
    with _locked_json_mapping(
        get_pending_tools_path(), "skipped malformed pending tools index"
    ) as index:
        key = f"{session_id}:{tool_use_id}"
        return index.pop(key, None)


def get_expired_tool_events(*, now_ms: int, timeout_ms: int = 60_000) -> List[Dict[str, Any]]:
    """Return and remove tool events buffered longer than timeout_ms."""
    with _locked_json_mapping(
        get_pending_tools_path(), "skipped malformed pending tools index"
    ) as index:
        expired: List[Dict[str, Any]] = []
        for key, event in list(index.items()):
            if now_ms - int(event.get("start_time", 0)) >= timeout_ms:
                expired.append(event)
                index.pop(key, None)
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
