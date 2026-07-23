"""Tests for HoneyHive event export payload shaping."""

from __future__ import annotations

from honeyhive_daemon.config import DaemonConfig
from honeyhive.models.models import PostEventRequest

from honeyhive_daemon.exporter import (
    _build_event_payload,
    export_event,
    update_event,
)


def _session_end_event(**overrides: object) -> dict:
    base = {
        "event_id": "e1",
        "session_id": "s1",
        "event_type": "chain",
        "event_name": "session.end",
        "start_time": 1000,
        "end_time": 5000,
        "duration": 0,
        "inputs": {},
        "outputs": {},
        "metadata": {},
    }
    base.update(overrides)
    return base


def test_build_event_payload_derives_duration_from_timestamps() -> None:
    config = DaemonConfig(api_key="k", base_url="https://api.honeyhive.ai")
    payload = _build_event_payload(config, _session_end_event())
    assert payload["event"]["duration"] == 4000


def test_build_event_payload_preserves_explicit_duration() -> None:
    config = DaemonConfig(api_key="k", base_url="https://api.honeyhive.ai")
    payload = _build_event_payload(
        config,
        _session_end_event(event_name="tool.bash", duration=3000),
    )
    assert payload["event"]["duration"] == 3000


def test_export_event_logs_success_after_create(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setenv("HH_DAEMON_HOME", str(tmp_path / "daemon-home"))
    log_lines: list[str] = []

    class FakeEventsAPI:
        def create_event(self, request) -> None:  # type: ignore[no-untyped-def]
            pass

    class FakeHoneyHive:
        def __init__(self, api_key: str, base_url: str) -> None:
            self.events = FakeEventsAPI()

    monkeypatch.setattr("honeyhive_daemon.exporter.HoneyHive", FakeHoneyHive)
    monkeypatch.setattr(
        "honeyhive_daemon.exporter.log_message",
        lambda msg: log_lines.append(msg),
    )

    config = DaemonConfig(api_key="k", base_url="https://api.honeyhive.ai")
    export_event(
        config,
        {
            "event_id": "end-1",
            "session_id": "sess-1",
            "event_type": "chain",
            "event_name": "session.end",
            "start_time": 1,
            "end_time": 2,
            "inputs": {},
            "outputs": {},
            "metadata": {},
        },
    )

    assert any(
        "export attempt" in line
        and "session_id=sess-1" in line
        and "event_id=end-1" in line
        for line in log_lines
    )
    assert any("exported claude event" in line and "session.end" in line for line in log_lines)
    assert any("session ended" in line and "sess-1" in line for line in log_lines)


def test_post_event_request_validates_against_installed_sdk() -> None:
    """The exporter must construct a valid PostEventRequest for the pinned SDK.

    This exercises the real (not mocked) ``PostEventRequest`` model the exporter
    builds, so a schema shape change in the pinned SDK is caught here instead of
    silently dropping every event at runtime.
    """
    config = DaemonConfig(api_key="k", base_url="https://api.honeyhive.ai")
    payload = _build_event_payload(config, _session_end_event(event_type="chain"))

    request = PostEventRequest(event=payload["event"])
    dumped = request.model_dump()

    assert dumped["event"]["event_type"] == "chain"


def test_build_event_payload_zero_duration_when_timestamps_equal() -> None:
    config = DaemonConfig(api_key="k", base_url="https://api.honeyhive.ai")
    payload = _build_event_payload(
        config,
        _session_end_event(event_name="session.start", end_time=1000),
    )
    assert payload["event"]["duration"] == 0


def test_update_event_accepts_metadata(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("HH_DAEMON_HOME", str(tmp_path / "daemon-home"))
    captured = {}

    class FakeEventsAPI:
        def update(self, data) -> None:  # type: ignore[no-untyped-def]
            captured["data"] = data

    class FakeHoneyHive:
        def __init__(self, api_key: str, base_url: str) -> None:
            self.events = FakeEventsAPI()

    monkeypatch.setattr("honeyhive_daemon.exporter.HoneyHive", FakeHoneyHive)

    config = DaemonConfig(api_key="k", base_url="https://api.honeyhive.ai")
    update_event(
        config,
        event_id="session-1",
        metadata={"total_tokens": 168},
        metrics={"coding_agent.total_tokens": 168},
    )

    data = captured["data"]
    payload = data.model_dump() if hasattr(data, "model_dump") else data
    assert payload["event_id"] == "session-1"
    assert payload["metadata"] == {"total_tokens": 168}
    assert payload["metrics"] == {"coding_agent.total_tokens": 168}
