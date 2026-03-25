"""Minimal HoneyHive event exporter for daemon events."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from .config import DaemonConfig
from .state import log_message

try:
    from honeyhive import HoneyHive
    from honeyhive._generated.models import PostEventRequest
except ImportError:  # pragma: no cover - exercised in local repo usage
    sdk_src = Path(__file__).resolve().parents[2] / "python-sdk" / "src"
    if str(sdk_src) not in sys.path:
        sys.path.insert(0, str(sdk_src))
    from honeyhive import HoneyHive
    from honeyhive._generated.models import PostEventRequest


def export_event(config: DaemonConfig, event: Dict[str, Any]) -> None:
    """Export a normalized event through the HoneyHive Python SDK."""
    payload = _build_event_payload(config, event)
    log_message(
        "export attempt "
        f"event_name={event['event_name']} "
        f"url={_get_events_endpoint(config.base_url)} "
        f"api_key_fingerprint={_key_fingerprint(config.api_key)}"
    )
    client = HoneyHive(api_key=config.api_key, base_url=config.base_url)
    client.events.create_event(PostEventRequest(event=payload["event"]))


def export_events(config: DaemonConfig, events: Iterable[Dict[str, Any]]) -> None:
    """Export multiple normalized events sequentially."""
    for event in events:
        export_event(config, event)


def update_event_outputs(
    config: DaemonConfig,
    *,
    event_id: str,
    outputs: Dict[str, Any],
) -> None:
    """Update an existing HoneyHive event with additional outputs."""
    update_event(config, event_id=event_id, outputs=outputs)


def update_event(
    config: DaemonConfig,
    *,
    event_id: str,
    inputs: Optional[Dict[str, Any]] = None,
    outputs: Optional[Dict[str, Any]] = None,
    metrics: Optional[Dict[str, Any]] = None,
) -> None:
    """Update an existing HoneyHive event with additional inputs, outputs, and/or metrics."""
    log_message(
        "update attempt "
        f"event_id={event_id} "
        f"url={_get_events_endpoint(config.base_url)} "
        f"api_key_fingerprint={_key_fingerprint(config.api_key)}"
    )
    data: Dict[str, Any] = {"event_id": event_id}
    if inputs is not None:
        data["inputs"] = inputs
    if outputs is not None:
        data["outputs"] = outputs
    if metrics is not None:
        data["metrics"] = metrics
    client = HoneyHive(api_key=config.api_key, base_url=config.base_url)
    client.events.update(data=data)


def _get_events_endpoint(base_url: str) -> str:
    base_url = base_url.rstrip("/")
    if base_url.endswith("/events"):
        return base_url
    return f"{base_url}/events"


def _build_event_payload(
    config: DaemonConfig, event: Dict[str, Any]
) -> Dict[str, Any]:
    metadata = dict(event.get("metadata", {}))
    raw_payload = event.get("raw")
    inputs = dict(event.get("inputs", {}))
    outputs = dict(event.get("outputs", {}))
    raw_pre = event.get("raw_pre")
    raw_post = event.get("raw_post")
    if raw_pre is not None or raw_post is not None:
        # Merged pre+post tool event — store both phases
        if raw_pre is not None:
            metadata["raw_pre"] = raw_pre
        if raw_post is not None:
            metadata["raw_post"] = raw_post
    elif raw_payload is not None:
        metadata["raw"] = raw_payload
    event_payload: Dict[str, Any] = {
        "project": config.project,
        "event_id": str(event["event_id"]),
        "session_id": str(event["session_id"]),
        "event_type": str(event["event_type"]),
        "event_name": str(event["event_name"]),
        "source": "claude-code",
        "start_time": int(event["start_time"]),
        "end_time": int(event.get("end_time", event["start_time"])),
        "duration": int(event.get("duration", 0)),
        "inputs": inputs,
        "outputs": outputs,
        "metadata": metadata,
        "children_ids": [],
    }
    if event.get("error"):
        event_payload["error"] = str(event["error"])
    if event.get("metrics"):
        event_payload["metrics"] = event["metrics"]
    if event.get("parent_id"):
        event_payload["parent_id"] = str(event["parent_id"])

    return {"event": event_payload}


def _key_fingerprint(value: str) -> str:
    if len(value) >= 10:
        return f"****{value[-4:]}"
    return "****"
