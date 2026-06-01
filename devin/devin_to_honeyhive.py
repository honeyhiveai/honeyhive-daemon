#!/usr/bin/env python3
"""Batch export workflow: Devin sessions → HoneyHive events.

Polls the Devin v3 Organization API for sessions and pushes them to
HoneyHive as session events, including messages and internal processing
events.

Usage:
    # One-shot sync
    python devin_to_honeyhive.py

    # Daemon mode (continuous polling)
    python devin_to_honeyhive.py --daemon

    # Custom interval
    python devin_to_honeyhive.py --daemon --interval 30

Environment variables:
    DEVIN_API_KEY       Devin service-user API key (cog_* prefix)
    DEVIN_ORG_ID        Devin organization ID (required)
    HH_API_KEY          HoneyHive API key
    HH_API_URL          HoneyHive data plane URL
    HH_PROJECT          HoneyHive project name (optional; API key is project-scoped)
    STATE_FILE_PATH     Path to sync state file (default: ./sync_state.json)
"""

import argparse
import json
import logging
import os
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import requests

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
log = logging.getLogger("devin-export")

DEVIN_BASE_URL = "https://api.devin.ai"
DEFAULT_STATE_FILE = "./sync_state.json"
DEFAULT_SYNC_INTERVAL = 60
BATCH_SIZE = 50


def devin_session_id_to_uuid(devin_session_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"devin-session:{devin_session_id}"))


def devin_message_id_to_uuid(devin_session_id: str, message_event_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"devin-message:{devin_session_id}:{message_event_id}"))


def devin_internal_event_id_to_uuid(devin_session_id: str, event_id: str) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"devin-event:{devin_session_id}:{event_id}"))


class DevinClient:
    def __init__(self, api_key: str, org_id: Optional[str] = None):
        self.api_key = api_key
        self.org_id = org_id
        self.headers = {"Authorization": f"Bearer {api_key}"}

        if not self.org_id:
            self.org_id = self._discover_org_id()
            if not self.org_id:
                raise ValueError(
                    "DEVIN_ORG_ID is required. Set the DEVIN_ORG_ID env var "
                    "to your organization ID (find it at Settings → Service Users)."
                )
            log.info("Auto-discovered org_id: %s", self.org_id)

    def _discover_org_id(self) -> Optional[str]:
        # Try v3 self endpoint first, then fall back to v3beta1
        for path in ("/v3/enterprise/self", "/v3beta1/enterprise/self"):
            try:
                resp = requests.get(
                    f"{DEVIN_BASE_URL}{path}",
                    headers=self.headers,
                    timeout=10,
                )
                if resp.ok:
                    data = resp.json()
                    # v3 returns org_id directly on the principal
                    if data.get("org_id"):
                        return data["org_id"]
                    orgs = data.get("organizations", [])
                    if orgs:
                        return orgs[0].get("org_id", orgs[0].get("id", ""))
            except requests.RequestException:
                continue
        return None

    def list_sessions(
        self,
        updated_after: Optional[int],
        limit: int,
        cursor: Optional[str],
    ) -> dict:
        url = f"{DEVIN_BASE_URL}/v3/organizations/{self.org_id}/sessions"
        params: dict = {"first": min(limit, 200)}
        if updated_after is not None:
            params["updated_after"] = updated_after
        if cursor:
            params["after"] = cursor

        resp = requests.get(url, headers=self.headers, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        sessions = []
        for item in data.get("items", []):
            sessions.append(self._normalize_v3_session(item))

        return {
            "sessions": sessions,
            "has_more": data.get("has_next_page", False),
            "cursor": data.get("end_cursor"),
            "total": data.get("total"),
        }

    def get_session(self, session_id: str) -> dict:
        url = f"{DEVIN_BASE_URL}/v3/organizations/{self.org_id}/sessions/devin-{session_id}"
        resp = requests.get(url, headers=self.headers, timeout=30)
        resp.raise_for_status()
        return self._normalize_v3_session(resp.json())

    def get_session_messages(self, session_id: str) -> list[dict]:
        """Paginate through the messages endpoint and return normalized dicts."""
        messages: list[dict] = []
        cursor: Optional[str] = None
        msg_index = 0
        base_url = (
            f"{DEVIN_BASE_URL}/v3/organizations/{self.org_id}"
            f"/sessions/devin-{session_id}/messages"
        )
        while True:
            params: dict = {"first": 200}
            if cursor:
                params["after"] = cursor

            resp = requests.get(
                base_url, headers=self.headers, params=params, timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()

            for item in data.get("items", []):
                messages.append(self._normalize_v3_message(item, msg_index))
                msg_index += 1

            if not data.get("has_next_page"):
                break
            cursor = data.get("end_cursor")
            if not cursor:
                break

        return messages

    @staticmethod
    def _normalize_v3_message(item: dict, index: int) -> dict:
        """Normalize an API message to the common format used by the mapper.

        The returned ``type`` is the raw v3 ``source`` value — always
        ``"user"`` or ``"devin"`` for well-formed responses.
        """
        created_epoch = item.get("created_at", 0)
        if isinstance(created_epoch, (int, float)) and created_epoch > 0:
            timestamp_ms = int(created_epoch * 1000) if created_epoch < 1e12 else int(created_epoch)
        else:
            timestamp_ms = 0

        return {
            "event_id": item.get("event_id", f"msg-{index}"),
            "type": item.get("source", "unknown"),
            "message": item.get("message", ""),
            "timestamp_ms": timestamp_ms,
            "index": index,
        }

    def get_session_events(self, session_id: str) -> list:
        """Fetch internal processing events for a session.

        Returns a list of normalized event dicts from the
        ``/v3/organizations/{org}/sessions/devin-{id}/events`` endpoint.
        """
        events: list[dict] = []
        cursor: Optional[str] = None
        index = 0
        base_url = (
            f"{DEVIN_BASE_URL}/v3/organizations/{self.org_id}"
            f"/sessions/devin-{session_id}/events"
        )

        while True:
            params: dict = {"first": 50}
            if cursor:
                params["after"] = cursor

            resp = requests.get(
                base_url, headers=self.headers, params=params, timeout=60,
            )
            resp.raise_for_status()
            data = resp.json()

            for item in data.get("items", []):
                events.append(self._normalize_v3_event(item, index))
                index += 1

            if not data.get("has_next_page"):
                break
            cursor = data.get("end_cursor")
            if not cursor:
                break

        return events

    @staticmethod
    def _normalize_v3_event(item: dict, index: int) -> dict:
        """Normalize a v3 internal event to a common dict format."""
        created_epoch = item.get("created_at", 0)
        if isinstance(created_epoch, (int, float)) and created_epoch > 0:
            timestamp_ms = int(created_epoch * 1000) if created_epoch < 1e12 else int(created_epoch)
        else:
            timestamp_ms = 0

        return {
            "event_id": item.get("event_id", f"evt-{index}"),
            "event_type": item.get("event_type", "unknown"),
            "category": item.get("category", "other"),
            "direction": item.get("direction", "outgoing"),
            "summary": item.get("summary", ""),
            "timestamp_ms": timestamp_ms,
            "index": index,
        }

    def _normalize_v3_session(self, item: dict) -> dict:
        created_epoch = item.get("created_at", 0)
        updated_epoch = item.get("updated_at", 0)

        if isinstance(created_epoch, int) and created_epoch > 1e12:
            created_ms = created_epoch
        elif isinstance(created_epoch, int):
            created_ms = created_epoch * 1000
        else:
            created_ms = 0

        if isinstance(updated_epoch, int) and updated_epoch > 1e12:
            updated_ms = updated_epoch
        elif isinstance(updated_epoch, int):
            updated_ms = updated_epoch * 1000
        else:
            updated_ms = 0

        return {
            "devin_session_id": item.get("session_id", ""),
            "title": item.get("title", ""),
            "status": item.get("status", ""),
            "status_detail": item.get("status_detail", ""),
            "tags": item.get("tags", []),
            "pull_requests": item.get("pull_requests", []),
            "url": item.get("url", ""),
            "user_id": item.get("user_id", ""),
            "origin": item.get("origin", ""),
            "category": item.get("category", ""),
            "playbook_id": item.get("playbook_id"),
            "acus_consumed": item.get("acus_consumed", 0),
            "is_archived": item.get("is_archived", False),
            "parent_session_id": item.get("parent_session_id"),
            "child_session_ids": item.get("child_session_ids", []),
            "structured_output": item.get("structured_output"),
            "created_at_ms": created_ms,
            "updated_at_ms": updated_ms,
            "created_at_epoch": created_epoch,
            "updated_at_epoch": updated_epoch,
        }


class HoneyHiveClient:
    def __init__(self, api_key: str, api_url: str, project: str = ""):
        self.api_key = api_key
        self.api_url = api_url.rstrip("/")
        self.project = project
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

    def create_session(self, session_data: dict) -> dict:
        url = f"{self.api_url}/session/start"
        payload = {"session": session_data}
        resp = requests.post(url, headers=self.headers, json=payload, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def update_event(self, event_id: str, updates: dict) -> dict:
        url = f"{self.api_url}/events"
        payload = {"event_id": event_id, **updates}
        resp = requests.put(url, headers=self.headers, json=payload, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def create_event_batch(self, events: list) -> dict:
        url = f"{self.api_url}/events/batch"
        payload = {"events": events}
        resp = requests.post(url, headers=self.headers, json=payload, timeout=30)
        resp.raise_for_status()
        return resp.json()


class SyncState:
    def __init__(self, state_file: str):
        self.state_file = Path(state_file)
        self._state = self._load()

    def _load(self) -> dict:
        if self.state_file.exists():
            try:
                return json.loads(self.state_file.read_text())
            except (json.JSONDecodeError, OSError):
                log.warning("Corrupt state file, starting fresh")
        return {"last_sync_epoch": 0, "synced_sessions": {}}

    def save(self) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        self.state_file.write_text(json.dumps(self._state, indent=2))

    @property
    def last_sync_epoch(self) -> int:
        return self._state.get("last_sync_epoch", 0)

    @last_sync_epoch.setter
    def last_sync_epoch(self, value: int) -> None:
        self._state["last_sync_epoch"] = value

    def get_hh_event_id(self, devin_session_id: str) -> Optional[str]:
        return self._state.get("synced_sessions", {}).get(devin_session_id, {}).get("hh_event_id")

    def set_synced(
        self,
        devin_session_id: str,
        hh_event_id: str,
        updated_epoch: int,
        message_count: int = 0,
        internal_event_count: int = 0,
    ) -> None:
        if "synced_sessions" not in self._state:
            self._state["synced_sessions"] = {}
        existing = self._state["synced_sessions"].get(devin_session_id, {})
        self._state["synced_sessions"][devin_session_id] = {
            "hh_event_id": hh_event_id,
            "last_updated_epoch": updated_epoch,
            "synced_message_count": message_count,
            "synced_event_count": internal_event_count,
            "session_end_emitted": existing.get("session_end_emitted", False),
        }

    def is_session_end_emitted(self, devin_session_id: str) -> bool:
        return self._state.get("synced_sessions", {}).get(devin_session_id, {}).get("session_end_emitted", False)

    def set_session_end_emitted(self, devin_session_id: str) -> None:
        if "synced_sessions" not in self._state:
            self._state["synced_sessions"] = {}
        if devin_session_id not in self._state["synced_sessions"]:
            self._state["synced_sessions"][devin_session_id] = {}
        self._state["synced_sessions"][devin_session_id]["session_end_emitted"] = True

    def get_last_updated(self, devin_session_id: str) -> int:
        return self._state.get("synced_sessions", {}).get(devin_session_id, {}).get("last_updated_epoch", 0)

    def get_synced_message_count(self, devin_session_id: str) -> int:
        return self._state.get("synced_sessions", {}).get(devin_session_id, {}).get("synced_message_count", 0)

    def get_synced_event_count(self, devin_session_id: str) -> int:
        return self._state.get("synced_sessions", {}).get(devin_session_id, {}).get("synced_event_count", 0)


def _build_session_metadata(session: dict) -> dict:
    """Build the ``metadata`` dict shared by create and update payloads."""
    pr_urls = [pr.get("pr_url", "") for pr in session.get("pull_requests", []) if pr.get("pr_url")]
    return {
        "devin_status": session.get("status", ""),
        "devin_status_detail": session.get("status_detail", ""),
        "devin_tags": session.get("tags", []),
        "devin_url": session.get("url", ""),
        "devin_pull_requests": pr_urls,
        "devin_origin": session.get("origin", ""),
        "devin_category": session.get("category", ""),
        "devin_playbook_id": session.get("playbook_id"),
        "devin_is_archived": session.get("is_archived", False),
        "devin_parent_session_id": session.get("parent_session_id"),
        "devin_child_session_ids": session.get("child_session_ids", []),
    }


def map_devin_to_hh_session(session: dict, project: str) -> dict:
    hh_session_id = devin_session_id_to_uuid(session["devin_session_id"])

    pr_urls = [pr.get("pr_url", "") for pr in session.get("pull_requests", []) if pr.get("pr_url")]

    # Extract initial user query for session inputs
    initial_query = session.get("initial_query", "")

    payload = {
        **({"project": project} if project else {}),
        "session_id": hh_session_id,
        "session_name": session.get("title") or f"Devin Session {session['devin_session_id'][:8]}",
        "source": "devin-export",
        "user_properties": {
            "devin_user_id": session.get("user_id", ""),
            "devin_session_id": session["devin_session_id"],
        },
        "metadata": _build_session_metadata(session),
        "metrics": {
            "acus_consumed": session.get("acus_consumed", 0),
        },
        "inputs": {
            "prompt": session.get("title", ""),
            **({"query": initial_query} if initial_query else {}),
        },
        "start_time": session.get("created_at_ms", 0),
        "end_time": session.get("updated_at_ms", 0),
    }
    return payload


def build_chat_history(messages: list) -> list:
    """Build a chat-style history from normalized Devin messages.

    Returns a list of ``{"role": ..., "content": ...}`` dicts suitable for
    storing in ``outputs.chat_history`` on the session event.
    """
    history: list[dict] = []
    for msg in messages:
        msg_type = msg.get("type", "unknown")
        content = msg.get("message", "")
        if msg_type == "user":
            role = "user"
        elif msg_type == "devin":
            role = "assistant"
        else:
            role = msg_type
        history.append({"role": role, "content": content})
    return history


def map_devin_messages_to_hh_events(
    messages: list,
    devin_session_id: str,
    hh_session_id: str,
    hh_parent_event_id: str,
    project: str = "",
    skip_count: int = 0,
) -> list:
    """Map normalized Devin messages to HoneyHive child events.

    Messages are expected to already be in the normalized format produced by
    ``DevinClient._normalize_v3_message`` — ``type`` is ``"user"`` or
    ``"devin"``.
    """
    events = []
    for i, msg in enumerate(messages):
        if i < skip_count:
            continue

        msg_event_id = msg.get("event_id", f"msg-{i}")
        hh_event_id = devin_message_id_to_uuid(devin_session_id, msg_event_id)
        msg_type = msg.get("type", "unknown")
        msg_content = msg.get("message", "")
        msg_timestamp_ms = msg.get("timestamp_ms", 0)

        if msg_type == "user":
            event_type = "chain"
            event_name = msg_type
            inputs = {"message": msg_content}
            outputs = {}
        elif msg_type == "devin":
            event_type = "model"
            event_name = msg_type
            inputs = {}
            outputs = {"message": msg_content}
        else:
            event_type = "tool"
            event_name = msg_type
            inputs = {}
            outputs = {"message": msg_content}

        events.append({
            **({"project": project} if project else {}),
            "event_id": hh_event_id,
            "session_id": hh_session_id,
            "parent_id": hh_parent_event_id,
            "event_type": event_type,
            "event_name": event_name,
            "source": "devin-export",
            "inputs": inputs,
            "outputs": outputs,
            "start_time": msg_timestamp_ms,
            "end_time": msg_timestamp_ms,
            "duration": 0,
            "metadata": {
                "devin_event_id": msg_event_id,
                "devin_session_id": devin_session_id,
                "message_index": msg.get("index", i),
            },
        })

    return events


# Categories of internal events that map to HoneyHive event_type="model"
_AGENT_CATEGORIES = {"message"}
# Categories that map to event_type="tool"
_TOOL_CATEGORIES = {"shell", "browser", "git", "file", "search", "todo", "webhook", "lifecycle"}

# Event types to skip — too noisy / internal bookkeeping
_SKIP_EVENT_TYPES = frozenset({
    "simple_activity_update",
    "checkpoint_created",
    "acu_consumption_at_last_user_interaction",
    "live_chain_update",
    "one_line_thoughts",
    "note_used",
    "repo_note_auto_import",
    "skills_available",
    "loaded_repo_setup_info",
    "vscode_ready",
    "terminal_update",
})


def map_devin_internal_events_to_hh_events(
    events: list,
    devin_session_id: str,
    hh_session_id: str,
    hh_parent_event_id: str,
    project: str = "",
    skip_count: int = 0,
) -> list:
    """Map normalized Devin internal events to HoneyHive child events.

    Filters out noisy internal events and maps the rest as tool/model events
    under the session.
    """
    hh_events = []
    for i, evt in enumerate(events):
        if i < skip_count:
            continue

        evt_type = evt.get("event_type", "unknown")
        if evt_type in _SKIP_EVENT_TYPES:
            continue

        category = evt.get("category", "other")
        summary = evt.get("summary", "")
        direction = evt.get("direction", "outgoing")
        timestamp_ms = evt.get("timestamp_ms", 0)
        evt_id = evt.get("event_id", f"evt-{i}")

        hh_event_id = devin_internal_event_id_to_uuid(devin_session_id, evt_id)

        # Determine HoneyHive event_type and name
        if category in _AGENT_CATEGORIES:
            hh_type = "model"
        elif category in _TOOL_CATEGORIES:
            hh_type = "tool"
        else:
            hh_type = "tool"

        event_name = f"{category}/{evt_type}"

        # Route summary into inputs or outputs based on direction
        if direction == "incoming":
            inputs = {"content": summary}
            outputs = {}
        else:
            inputs = {}
            outputs = {"content": summary}

        event = {
            **({"project": project} if project else {}),
            "event_id": hh_event_id,
            "session_id": hh_session_id,
            "parent_id": hh_parent_event_id,
            "event_type": hh_type,
            "event_name": event_name,
            "source": "devin-export",
            "inputs": inputs,
            "outputs": outputs,
            "start_time": timestamp_ms,
            "end_time": timestamp_ms,
            "duration": 0,
            "metadata": {
                "devin_event_id": evt_id,
                "devin_session_id": devin_session_id,
                "devin_event_type": evt_type,
                "devin_category": category,
                "devin_direction": direction,
                "event_index": evt.get("index", i),
            },
        }
        hh_events.append(event)

    return hh_events


def map_devin_session_end(
    session: dict,
    hh_session_id: str,
    hh_parent_event_id: str,
    project: str = "",
    messages: list = None,
) -> dict:
    """Create a session.end chain event with an artifact containing the conversation.

    This allows server-side evaluators (which trigger on session.end with
    outputs.artifact) to work on Devin sessions too.
    """
    if messages is None:
        messages = []
    end_event_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"devin-session-end:{session['devin_session_id']}"))
    end_time = session.get("updated_at_ms", 0)

    # Build artifact content from messages (same structure evaluators expect)
    artifact_content = []
    for msg in messages:
        msg_type = msg.get("type", "unknown")
        is_user = msg_type == "user"
        artifact_content.append({
            "type": "text" if is_user else "tool_use",
            "tool_name": None if is_user else msg_type,
            "message": msg.get("message", ""),
        })
    return {
        **({"project": project} if project else {}),
        "event_id": end_event_id,
        "session_id": hh_session_id,
        "parent_id": hh_parent_event_id,
        "event_type": "chain",
        "event_name": "session.end",
        "source": "devin-export",
        "inputs": {},
        "outputs": {
            "artifact": {
                "type": "transcript",
                "format": "json",
                "content": artifact_content,
                "reason": "session_end",
            }
        },
        "start_time": end_time,
        "end_time": end_time,
        "duration": 0,
        "metadata": {
            "devin_session_id": session["devin_session_id"],
            "devin_status": session.get("status", ""),
        },
    }


def map_devin_to_hh_update(session: dict) -> dict:
    return {
        "metadata": _build_session_metadata(session),
        "metrics": {
            "acus_consumed": session.get("acus_consumed", 0),
        },
        "duration": max(0, (session.get("updated_at_ms", 0) - session.get("created_at_ms", 0))),
    }


def sync_sessions(
    devin: DevinClient,
    hh: HoneyHiveClient,
    state: SyncState,
) -> int:
    synced_count = 0
    cursor = None
    updated_after = state.last_sync_epoch if state.last_sync_epoch > 0 else None
    new_sync_epoch = int(time.time())

    while True:
        try:
            result = devin.list_sessions(
                updated_after=updated_after,
                limit=BATCH_SIZE,
                cursor=cursor,
            )
        except requests.RequestException as e:
            log.error("Failed to fetch Devin sessions: %s", e)
            break

        sessions = result["sessions"]
        if not sessions:
            break

        for session in sessions:
            devin_sid = session["devin_session_id"]
            existing_hh_id = state.get_hh_event_id(devin_sid)
            hh_session_id = devin_session_id_to_uuid(devin_sid)

            try:
                if existing_hh_id:
                    last_updated = state.get_last_updated(devin_sid)
                    if session.get("updated_at_epoch", 0) <= last_updated:
                        continue

                    updates = map_devin_to_hh_update(session)
                    hh.update_event(existing_hh_id, updates)
                    log.info("Updated session %s (HH: %s)", devin_sid[:12], existing_hh_id[:8])
                else:
                    hh_session = map_devin_to_hh_session(session, hh.project)
                    resp = hh.create_session(hh_session)
                    existing_hh_id = resp.get("event_id", resp.get("session_id", ""))
                    log.info("Created session %s → HH %s", devin_sid[:12], existing_hh_id[:8])

                msg_count, evt_count = _sync_session_details(
                    devin, hh, state, devin_sid, hh_session_id, existing_hh_id,
                    session=session,
                )
                state.set_synced(
                    devin_sid, existing_hh_id,
                    session.get("updated_at_epoch", 0),
                    message_count=msg_count,
                    internal_event_count=evt_count,
                )
                synced_count += 1
            except requests.RequestException as e:
                log.error("Failed to sync session %s: %s", devin_sid[:12], e)
                continue

        if not result["has_more"]:
            break
        cursor = result.get("cursor")

    state.last_sync_epoch = new_sync_epoch
    state.save()
    return synced_count


def _sync_session_details(
    devin: DevinClient,
    hh: HoneyHiveClient,
    state: SyncState,
    devin_sid: str,
    hh_session_id: str,
    hh_parent_event_id: str,
    session: Optional[dict] = None,
) -> tuple[int, int]:
    """Sync messages and internal events for a session.

    Returns ``(message_count, internal_event_count)``.
    """
    msg_count = _sync_session_messages(
        devin, hh, state, devin_sid, hh_session_id, hh_parent_event_id,
        session=session,
    )
    evt_count = _sync_session_internal_events(
        devin, hh, state, devin_sid, hh_session_id, hh_parent_event_id,
    )
    return msg_count, evt_count


def _sync_session_messages(
    devin: DevinClient,
    hh: HoneyHiveClient,
    state: SyncState,
    devin_sid: str,
    hh_session_id: str,
    hh_parent_event_id: str,
    session: Optional[dict] = None,
) -> int:
    try:
        messages = devin.get_session_messages(devin_sid)
    except requests.RequestException as e:
        log.warning("Failed to fetch messages for session %s: %s", devin_sid[:12], e)
        return state.get_synced_message_count(devin_sid)

    if not messages:
        return 0

    # ── Update chat_history + structured_output + inputs on the parent session ──
    chat_history = build_chat_history(messages)
    session_outputs: dict = {"chat_history": chat_history}
    structured_output = session.get("structured_output") if session else None
    if structured_output is not None:
        session_outputs["structured_output"] = structured_output

    # Extract initial user query from the first user message and store
    # in metadata (PUT /events does not support updating inputs).
    update_payload: dict = {"outputs": session_outputs}
    initial_msg = next(
        (m for m in messages if m.get("type") in ("initial_user_message", "user_message", "user")),
        None,
    )
    if initial_msg:
        update_payload["metadata"] = {"initial_query": initial_msg.get("message", "")}

    try:
        hh.update_event(hh_parent_event_id, update_payload)
    except requests.RequestException as e:
        log.warning(
            "Failed to update chat_history on session %s: %s",
            devin_sid[:12], e,
        )

    # ── Emit session.end for completed sessions (once only) ──
    # Only emit session.end once per session to avoid duplicates, since
    # HoneyHive's events/batch endpoint does not deduplicate by event_id.
    if session and not state.is_session_end_emitted(devin_sid):
        status = session.get("status", "")
        if status in ("finished", "stopped", "failed"):
            try:
                end_event = map_devin_session_end(
                    session=session,
                    hh_session_id=hh_session_id,
                    hh_parent_event_id=hh_parent_event_id,
                    project=hh.project,
                    messages=messages,
                )
                hh.create_event_batch([end_event])
                state.set_session_end_emitted(devin_sid)
                log.info("Created session.end event for session %s (status=%s)", devin_sid[:12], status)
            except requests.RequestException as e:
                log.warning("Failed to create session.end for %s: %s", devin_sid[:12], e)

    # ── Create child message events (incremental) ──
    previously_synced = state.get_synced_message_count(devin_sid)
    if len(messages) <= previously_synced:
        return previously_synced

    new_events = map_devin_messages_to_hh_events(
        messages=messages,
        devin_session_id=devin_sid,
        hh_session_id=hh_session_id,
        hh_parent_event_id=hh_parent_event_id,
        project=hh.project,
        skip_count=previously_synced,
    )

    if not new_events:
        return previously_synced

    batch_size = 50
    for i in range(0, len(new_events), batch_size):
        batch = new_events[i : i + batch_size]
        try:
            hh.create_event_batch(batch)
            log.info(
                "Created %d message events for session %s (batch %d)",
                len(batch), devin_sid[:12], i // batch_size + 1,
            )
        except requests.RequestException as e:
            log.error(
                "Failed to create message batch for session %s: %s",
                devin_sid[:12], e,
            )
            return previously_synced + i

    total = len(messages)
    log.info(
        "Synced %d/%d messages for session %s (%d new)",
        total, total, devin_sid[:12], total - previously_synced,
    )

    return total


def _sync_session_internal_events(
    devin: DevinClient,
    hh: HoneyHiveClient,
    state: SyncState,
    devin_sid: str,
    hh_session_id: str,
    hh_parent_event_id: str,
) -> int:
    """Fetch and sync Devin internal processing events for a session."""
    try:
        raw_events = devin.get_session_events(devin_sid)
    except requests.RequestException as e:
        log.warning("Failed to fetch internal events for session %s: %s", devin_sid[:12], e)
        return state.get_synced_event_count(devin_sid)

    if not raw_events:
        return 0

    previously_synced = state.get_synced_event_count(devin_sid)
    if len(raw_events) <= previously_synced:
        return previously_synced

    new_events = map_devin_internal_events_to_hh_events(
        events=raw_events,
        devin_session_id=devin_sid,
        hh_session_id=hh_session_id,
        hh_parent_event_id=hh_parent_event_id,
        project=hh.project,
        skip_count=previously_synced,
    )

    if not new_events:
        return len(raw_events)

    batch_size = 50
    last_raw_index_synced = previously_synced
    for i in range(0, len(new_events), batch_size):
        batch = new_events[i : i + batch_size]
        try:
            hh.create_event_batch(batch)
            # Track the raw event index of the last event in this batch
            # so error recovery resumes from the correct position.
            last_raw_index_synced = batch[-1]["metadata"]["event_index"] + 1
            log.info(
                "Created %d internal events for session %s (batch %d)",
                len(batch), devin_sid[:12], i // batch_size + 1,
            )
        except requests.RequestException as e:
            log.error(
                "Failed to create internal event batch for session %s: %s",
                devin_sid[:12], e,
            )
            return last_raw_index_synced

    total = len(raw_events)
    log.info(
        "Synced %d internal events for session %s (%d new, %d after filtering)",
        total, devin_sid[:12], total - previously_synced, len(new_events),
    )
    return total


def run_once(devin: DevinClient, hh: HoneyHiveClient, state: SyncState) -> int:
    log.info(
        "Starting sync (last_sync_epoch=%d, %s)",
        state.last_sync_epoch,
        datetime.fromtimestamp(state.last_sync_epoch, tz=timezone.utc).isoformat()
        if state.last_sync_epoch > 0
        else "first run",
    )
    count = sync_sessions(devin, hh, state)
    log.info("Sync complete: %d sessions processed", count)
    return count


def run_daemon(
    devin: DevinClient,
    hh: HoneyHiveClient,
    state: SyncState,
    interval: int,
) -> None:
    log.info("Starting daemon mode (interval=%ds)", interval)
    while True:
        try:
            run_once(devin, hh, state)
        except KeyboardInterrupt:
            log.info("Shutting down")
            break
        except Exception:
            log.exception("Unexpected error during sync cycle")

        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            log.info("Shutting down")
            break


def main() -> None:
    parser = argparse.ArgumentParser(description="Export Devin sessions to HoneyHive")
    parser.add_argument("--daemon", action="store_true", help="Run continuously")
    parser.add_argument(
        "--interval",
        type=int,
        default=int(os.environ.get("SYNC_INTERVAL_SECONDS", str(DEFAULT_SYNC_INTERVAL))),
        help=f"Polling interval in seconds (default: {DEFAULT_SYNC_INTERVAL})",
    )
    parser.add_argument(
        "--state-file",
        default=os.environ.get("STATE_FILE_PATH", DEFAULT_STATE_FILE),
        help="Path to sync state file",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="Enable debug logging")
    args = parser.parse_args()

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)

    devin_api_key = os.environ.get("DEVIN_API_KEY", "")
    devin_org_id = os.environ.get("DEVIN_ORG_ID", "")
    hh_api_key = os.environ.get("HH_API_KEY", "")
    hh_api_url = os.environ.get("HH_API_URL", "")
    hh_project = os.environ.get("HH_PROJECT", "")

    if not devin_api_key:
        log.error("DEVIN_API_KEY is required")
        sys.exit(1)
    if not hh_api_key:
        log.error("HH_API_KEY is required")
        sys.exit(1)
    if not hh_api_url:
        log.error("HH_API_URL is required")
        sys.exit(1)
    devin = DevinClient(api_key=devin_api_key, org_id=devin_org_id or None)
    hh = HoneyHiveClient(api_key=hh_api_key, api_url=hh_api_url, project=hh_project)
    state = SyncState(args.state_file)

    log.info("Devin API: v3 (org=%s)", devin.org_id)
    if hh.project:
        log.info("HoneyHive: %s → project '%s'", hh.api_url, hh.project)
    else:
        log.info("HoneyHive: %s (project resolved from API key)", hh.api_url)

    if args.daemon:
        run_daemon(devin, hh, state, args.interval)
    else:
        run_once(devin, hh, state)


if __name__ == "__main__":
    main()
