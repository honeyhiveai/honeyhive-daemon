#!/usr/bin/env python3
"""Batch export workflow: Devin sessions → HoneyHive events.

Polls Devin's API for sessions and pushes them to HoneyHive as session events.
Supports both Devin v1 (apk_* keys) and v3 (cog_* keys) APIs.

Usage:
    # One-shot sync
    python devin_to_honeyhive.py

    # Daemon mode (continuous polling)
    python devin_to_honeyhive.py --daemon

    # Custom interval
    python devin_to_honeyhive.py --daemon --interval 30

Environment variables:
    DEVIN_API_KEY       Devin API key (auto-detects v1 vs v3 from prefix)
    DEVIN_ORG_ID        Required for v3 (cog_*) keys (auto-discovered if admin)
    HH_API_KEY          HoneyHive API key
    HH_API_URL          HoneyHive data plane URL
    HH_PROJECT          HoneyHive project name
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
        self.is_v3 = api_key.startswith("cog_")

        if self.is_v3 and not self.org_id:
            self.org_id = self._discover_org_id()
            if not self.org_id:
                raise ValueError(
                    "DEVIN_ORG_ID is required for v3 (cog_*) API keys and "
                    "could not be auto-discovered. Set DEVIN_ORG_ID env var."
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
        updated_after: Optional[int] = None,
        limit: int = 100,
        cursor: Optional[str] = None,
    ) -> dict:
        if self.is_v3:
            return self._list_sessions_v3(updated_after, limit, cursor)
        return self._list_sessions_v1(updated_after, limit)

    def _list_sessions_v3(
        self,
        updated_after: Optional[int],
        limit: int,
        cursor: Optional[str],
    ) -> dict:
        url = f"{DEVIN_BASE_URL}/v3beta1/organizations/{self.org_id}/sessions"
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

    def _list_sessions_v1(
        self,
        updated_after: Optional[int],
        limit: int,
    ) -> dict:
        url = f"{DEVIN_BASE_URL}/v1/sessions"
        params: dict = {"limit": limit}

        resp = requests.get(url, headers=self.headers, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        sessions = []
        for item in data.get("sessions", []):
            normalized = self._normalize_v1_session(item)
            if updated_after and normalized["updated_at_epoch"]:
                if normalized["updated_at_epoch"] <= updated_after:
                    continue
            sessions.append(normalized)

        return {
            "sessions": sessions,
            "has_more": False,
            "cursor": None,
            "total": len(sessions),
        }

    def get_session(self, session_id: str) -> dict:
        if self.is_v3:
            url = f"{DEVIN_BASE_URL}/v3/organizations/{self.org_id}/sessions/devin-{session_id}"
        else:
            url = f"{DEVIN_BASE_URL}/v1/sessions/{session_id}"

        resp = requests.get(url, headers=self.headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        if self.is_v3:
            return self._normalize_v3_session(data)
        return self._normalize_v1_session(data)

    def get_session_details(self, session_id: str) -> dict:
        if self.is_v3:
            return self._get_session_details_v3(session_id)
        return self._get_session_details_v1(session_id)

    def _get_session_details_v1(self, session_id: str) -> dict:
        url = f"{DEVIN_BASE_URL}/v1/sessions/{session_id}"
        resp = requests.get(url, headers=self.headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        return {
            "messages": [
                self._normalize_v1_message(m, i)
                for i, m in enumerate(data.get("messages", []))
            ],
            "structured_output": data.get("structured_output"),
        }

    def _get_session_details_v3(self, session_id: str) -> dict:
        """Fetch session detail and messages via the v3 API.

        The v3 API serves messages on a dedicated paginated endpoint
        (``/v3/organizations/{org}/sessions/devin-{id}/messages``) rather
        than embedding them in the session detail response.
        """
        # Fetch session-level fields (structured_output, etc.)
        detail_url = (
            f"{DEVIN_BASE_URL}/v3/organizations/{self.org_id}"
            f"/sessions/devin-{session_id}"
        )
        structured_output = None
        try:
            detail_resp = requests.get(
                detail_url, headers=self.headers, timeout=30,
            )
            if detail_resp.ok:
                structured_output = detail_resp.json().get("structured_output")
        except requests.RequestException:
            pass

        # Paginate through the messages endpoint
        messages: list[dict] = []
        cursor: Optional[str] = None
        msg_index = 0
        while True:
            msgs_url = (
                f"{DEVIN_BASE_URL}/v3/organizations/{self.org_id}"
                f"/sessions/devin-{session_id}/messages"
            )
            params: dict = {"first": 200}
            if cursor:
                params["after"] = cursor

            resp = requests.get(
                msgs_url, headers=self.headers, params=params, timeout=30,
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

        return {
            "messages": messages,
            "structured_output": structured_output,
        }

    @staticmethod
    def _normalize_v3_message(item: dict, index: int) -> dict:
        """Normalize a v3 message to the common format used by the mapper."""
        source = item.get("source", "unknown")
        created_epoch = item.get("created_at", 0)
        # v3 created_at is epoch seconds; convert to ms
        if isinstance(created_epoch, (int, float)) and created_epoch > 0:
            timestamp_ms = int(created_epoch * 1000) if created_epoch < 1e12 else int(created_epoch)
        else:
            timestamp_ms = 0

        # Map v3 source values to the type vocabulary used by v1 / the mapper
        if source == "user":
            msg_type = "user_message"
        elif source == "devin":
            msg_type = "agent_message"
        else:
            msg_type = source

        return {
            "event_id": item.get("event_id", f"msg-{index}"),
            "type": msg_type,
            "message": item.get("message", ""),
            "timestamp_ms": timestamp_ms,
            "index": index,
        }

    @staticmethod
    def _normalize_v1_message(item: dict, index: int) -> dict:
        """Normalize a v1 message to the common format used by the mapper."""
        msg_type = item.get("type", "unknown")
        timestamp_str = item.get("timestamp", "")
        timestamp_ms = _iso_to_epoch_ms(timestamp_str)

        return {
            "event_id": item.get("event_id", f"msg-{index}"),
            "type": msg_type,
            "message": item.get("message", ""),
            "timestamp_ms": timestamp_ms,
            "origin": item.get("origin"),
            "user_id": item.get("user_id"),
            "username": item.get("username"),
            "index": index,
        }

    def get_session_events(self, session_id: str) -> list:
        """Fetch internal processing events for a v3 session.

        Returns a list of normalized event dicts from the
        ``/v3/organizations/{org}/sessions/devin-{id}/events`` endpoint.
        Only available for v3 API keys.
        """
        if not self.is_v3:
            return []

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
            "tags": item.get("tags", []),
            "pull_requests": item.get("pull_requests", []),
            "url": item.get("url", ""),
            "user_id": item.get("user_id", ""),
            "acus_consumed": item.get("acus_consumed", 0),
            "is_archived": item.get("is_archived", False),
            "parent_session_id": item.get("parent_session_id"),
            "child_session_ids": item.get("child_session_ids", []),
            "created_at_ms": created_ms,
            "updated_at_ms": updated_ms,
            "created_at_epoch": created_epoch,
            "updated_at_epoch": updated_epoch,
        }

    def _normalize_v1_session(self, item: dict) -> dict:
        created_str = item.get("created_at", "")
        updated_str = item.get("updated_at", "")

        created_ms = _iso_to_epoch_ms(created_str)
        updated_ms = _iso_to_epoch_ms(updated_str)
        created_epoch = created_ms // 1000 if created_ms else 0
        updated_epoch = updated_ms // 1000 if updated_ms else 0

        pr_info = item.get("pull_request", {})
        pull_requests = []
        if pr_info and pr_info.get("url"):
            pull_requests.append({"pr_url": pr_info["url"], "pr_state": ""})

        return {
            "devin_session_id": item.get("session_id", ""),
            "title": item.get("title", ""),
            "status": item.get("status_enum", item.get("status", "")),
            "tags": item.get("tags", []),
            "pull_requests": pull_requests,
            "url": f"https://app.devin.ai/sessions/{item.get('session_id', '')}",
            "user_id": item.get("requesting_user_email", ""),
            "acus_consumed": 0,
            "is_archived": False,
            "parent_session_id": None,
            "child_session_ids": [],
            "created_at_ms": created_ms,
            "updated_at_ms": updated_ms,
            "created_at_epoch": created_epoch,
            "updated_at_epoch": updated_epoch,
        }


class HoneyHiveClient:
    def __init__(self, api_key: str, api_url: str, project: str):
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
        self._state["synced_sessions"][devin_session_id] = {
            "hh_event_id": hh_event_id,
            "last_updated_epoch": updated_epoch,
            "synced_message_count": message_count,
            "synced_event_count": internal_event_count,
        }

    def get_last_updated(self, devin_session_id: str) -> int:
        return self._state.get("synced_sessions", {}).get(devin_session_id, {}).get("last_updated_epoch", 0)

    def get_synced_message_count(self, devin_session_id: str) -> int:
        return self._state.get("synced_sessions", {}).get(devin_session_id, {}).get("synced_message_count", 0)

    def get_synced_event_count(self, devin_session_id: str) -> int:
        return self._state.get("synced_sessions", {}).get(devin_session_id, {}).get("synced_event_count", 0)


def _iso_to_epoch_ms(iso_str: str) -> int:
    if not iso_str:
        return 0
    try:
        dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
        return int(dt.timestamp() * 1000)
    except (ValueError, TypeError):
        return 0


def map_devin_to_hh_session(session: dict, project: str) -> dict:
    hh_session_id = devin_session_id_to_uuid(session["devin_session_id"])

    pr_urls = [pr.get("pr_url", "") for pr in session.get("pull_requests", []) if pr.get("pr_url")]

    return {
        "project": project,
        "session_id": hh_session_id,
        "session_name": session.get("title") or f"Devin Session {session['devin_session_id'][:8]}",
        "source": "devin-export",
        "user_properties": {
            "devin_user_id": session.get("user_id", ""),
            "devin_session_id": session["devin_session_id"],
        },
        "metadata": {
            "devin_status": session.get("status", ""),
            "devin_tags": session.get("tags", []),
            "devin_url": session.get("url", ""),
            "devin_pull_requests": pr_urls,
            "devin_is_archived": session.get("is_archived", False),
            "devin_parent_session_id": session.get("parent_session_id"),
            "devin_child_session_ids": session.get("child_session_ids", []),
        },
        "metrics": {
            "acus_consumed": session.get("acus_consumed", 0),
        },
        "start_time": session.get("created_at_ms", 0),
        "end_time": session.get("updated_at_ms", 0),
    }


def build_chat_history(messages: list) -> list:
    """Build a chat-style history from normalized Devin messages.

    Returns a list of ``{"role": ..., "content": ...}`` dicts suitable for
    storing in ``outputs.chat_history`` on the session event.
    """
    history: list[dict] = []
    for msg in messages:
        msg_type = msg.get("type", "unknown")
        content = msg.get("message", "")
        if msg_type in ("user_message", "user"):
            role = "user"
        elif msg_type in ("agent_message", "devin"):
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
    project: str,
    skip_count: int = 0,
) -> list:
    """Map normalized Devin messages to HoneyHive child events.

    Messages are expected to already be in the normalized format produced by
    ``DevinClient._normalize_v3_message`` / ``_normalize_v1_message``.
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
        msg_origin = msg.get("origin")
        msg_user_id = msg.get("user_id")
        msg_username = msg.get("username")

        is_user_message = msg_type in ("user_message", "user")
        is_agent_message = msg_type in ("agent_message", "devin")

        if is_user_message:
            event_type = "tool"
            event_name = "user_message"
            inputs = {"message": msg_content}
            outputs = {}
        elif is_agent_message:
            event_type = "model"
            event_name = "agent_message"
            inputs = {}
            outputs = {"message": msg_content}
        else:
            event_type = "tool"
            event_name = msg_type
            inputs = {}
            outputs = {"message": msg_content}

        event = {
            "project": project,
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
        }
        if msg_origin:
            event["metadata"]["origin"] = msg_origin
        if msg_user_id:
            event["metadata"]["user_id"] = msg_user_id
        if msg_username:
            event["metadata"]["username"] = msg_username

        events.append(event)

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
    project: str,
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
            "project": project,
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
    project: str,
    messages: list,
) -> dict:
    """Create a session.end chain event with an artifact containing the conversation.

    This allows server-side evaluators (which trigger on session.end with
    outputs.artifact) to work on Devin sessions too.
    """
    end_event_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"devin-session-end:{session['devin_session_id']}"))
    end_time = session.get("updated_at_ms", 0)

    # Build artifact content from messages (same structure evaluators expect)
    artifact_content = []
    for msg in messages:
        msg_type = msg.get("type", "unknown")
        is_user = msg_type in ("user_message", "user")
        artifact_content.append({
            "type": "tool_use" if not is_user else "text",
            "tool_name": msg_type if not is_user else None,
            "message": msg.get("message", ""),
            "timestamp": msg.get("timestamp", ""),
            "origin": msg.get("origin"),
        })

    return {
        "project": project,
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
    pr_urls = [pr.get("pr_url", "") for pr in session.get("pull_requests", []) if pr.get("pr_url")]

    return {
        "metadata": {
            "devin_status": session.get("status", ""),
            "devin_tags": session.get("tags", []),
            "devin_url": session.get("url", ""),
            "devin_pull_requests": pr_urls,
            "devin_is_archived": session.get("is_archived", False),
            "devin_parent_session_id": session.get("parent_session_id"),
            "devin_child_session_ids": session.get("child_session_ids", []),
        },
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
        details = devin.get_session_details(devin_sid)
    except requests.RequestException as e:
        log.warning("Failed to fetch details for session %s: %s", devin_sid[:12], e)
        return state.get_synced_message_count(devin_sid)

    messages = details.get("messages", [])
    if not messages:
        return 0

    # ── Update chat_history + structured_output on the parent session event ──
    chat_history = build_chat_history(messages)
    session_outputs: dict = {"chat_history": chat_history}
    structured_output = details.get("structured_output")
    if structured_output is not None:
        session_outputs["structured_output"] = structured_output
    try:
        hh.update_event(hh_parent_event_id, {"outputs": session_outputs})
    except requests.RequestException as e:
        log.warning(
            "Failed to update chat_history on session %s: %s",
            devin_sid[:12], e,
        )

    # ── Emit session.end for completed sessions (before early return) ──
    # This must run even when there are no new messages, so that sessions
    # that transition to finished/stopped/failed get a session.end event
    # for server-side evaluators. The event_id is deterministic (UUID5),
    # so re-emitting on subsequent syncs is idempotent.
    if session:
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
    if not devin.is_v3:
        return 0

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
    if not hh_project:
        log.error("HH_PROJECT is required")
        sys.exit(1)

    devin = DevinClient(api_key=devin_api_key, org_id=devin_org_id or None)
    hh = HoneyHiveClient(api_key=hh_api_key, api_url=hh_api_url, project=hh_project)
    state = SyncState(args.state_file)

    log.info("Devin API: %s mode", "v3" if devin.is_v3 else "v1")
    log.info("HoneyHive: %s → project '%s'", hh.api_url, hh.project)

    if args.daemon:
        run_daemon(devin, hh, state, args.interval)
    else:
        run_once(devin, hh, state)


if __name__ == "__main__":
    main()
