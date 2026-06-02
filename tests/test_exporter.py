"""Tests for HoneyHive event export payload shaping."""

from __future__ import annotations

from honeyhive_daemon.config import DaemonConfig
from honeyhive_daemon.exporter import _build_event_payload


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


def test_build_event_payload_omits_legacy_project_field() -> None:
    config = DaemonConfig(api_key="k", base_url="https://api.honeyhive.ai")
    payload = _build_event_payload(config, _session_end_event())
    assert "project" not in payload["event"]


def test_build_event_payload_zero_duration_when_timestamps_equal() -> None:
    config = DaemonConfig(api_key="k", base_url="https://api.honeyhive.ai")
    payload = _build_event_payload(
        config,
        _session_end_event(event_name="session.start", end_time=1000),
    )
    assert payload["event"]["duration"] == 0
