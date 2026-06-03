"""Tests for transcript-derived session metrics."""

from __future__ import annotations

from honeyhive_daemon.metrics import compute_session_metrics


def _claude_code_transcript() -> list[dict]:
    """Representative Claude Code JSONL records (assistant + tools)."""
    return [
        {"type": "custom-title", "customTitle": "smoke-test"},
        {"type": "user", "message": {"role": "user", "content": "hello"}},
        {"type": "assistant", "message": {"role": "assistant", "content": "hi there"}},
        {"type": "tool_use", "tool_name": "Bash", "name": "Bash", "tool_input": {"command": "ls"}},
        {"type": "tool_result", "tool_name": "Bash", "is_error": False},
        {"type": "assistant", "message": {"role": "assistant", "content": "done"}},
    ]


def test_compute_session_metrics_counts_assistant_turns() -> None:
    metrics = compute_session_metrics(_claude_code_transcript())
    assert metrics["coding_agent.event_count"] == 6.0
    assert metrics["coding_agent.model_count"] >= 2.0


def test_compute_session_metrics_counts_legacy_text_and_thinking() -> None:
    metrics = compute_session_metrics(
        [
            {"type": "text", "text": "some output"},
            {"type": "thinking", "text": "reasoning"},
        ]
    )
    assert metrics["coding_agent.model_count"] >= 2.0


def test_compute_session_metrics_aggregates_token_usage() -> None:
    transcript = [
        {
            "type": "assistant",
            "message": {"role": "assistant", "content": "hi"},
            "usage": {"input_tokens": 100, "output_tokens": 20},
        },
        {
            "type": "assistant",
            "message": {"role": "assistant", "content": "done"},
            "usage": {"input_tokens": 200, "output_tokens": 50},
        },
    ]
    metrics = compute_session_metrics(transcript)
    assert metrics["coding_agent.total_input_tokens"] == 300.0
    assert metrics["coding_agent.total_output_tokens"] == 70.0
    assert metrics["coding_agent.total_tokens"] == 370.0


def test_compute_session_metrics_reads_usage_from_message() -> None:
    """Claude Code nests usage under message, not at the record top level."""
    metrics = compute_session_metrics(
        [
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": "hi",
                    "usage": {"input_tokens": 100, "output_tokens": 20},
                },
                "requestId": "req-1",
            },
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": "done",
                    "usage": {"input_tokens": 200, "output_tokens": 50},
                },
                "requestId": "req-2",
            },
        ]
    )
    assert metrics["coding_agent.total_tokens"] == 370.0


def test_compute_session_metrics_dedupes_streaming_chunks_by_request_id() -> None:
    chunk = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [],
            "usage": {"input_tokens": 2739, "output_tokens": 238},
            "stop_reason": "tool_use",
        },
        "requestId": "req-same",
    }
    metrics = compute_session_metrics([chunk, dict(chunk), dict(chunk)])
    assert metrics["coding_agent.total_input_tokens"] == 2739.0
    assert metrics["coding_agent.total_output_tokens"] == 238.0
    assert metrics["coding_agent.total_tokens"] == 2977.0


def test_compute_session_metrics_aggregates_cache_token_fields() -> None:
    metrics = compute_session_metrics(
        [
            {
                "type": "assistant",
                "message": {"role": "assistant", "content": "hi"},
                "usage": {
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "cache_read_input_tokens": 50,
                    "cache_creation_input_tokens": 10,
                },
            },
        ]
    )
    assert metrics["coding_agent.total_cache_read_tokens"] == 50.0
    assert metrics["coding_agent.total_cache_creation_tokens"] == 10.0


def test_compute_session_metrics_counts_nested_tool_use_blocks() -> None:
    """Claude Code embeds tool_use blocks inside assistant messages."""
    metrics = compute_session_metrics(
        [
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "name": "Read", "id": "tool-1"},
                        {"type": "tool_use", "name": "Bash", "id": "tool-2"},
                    ],
                },
                "requestId": "req-tools",
            },
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "tool-1", "is_error": False},
                    ],
                },
            },
        ]
    )
    assert metrics["coding_agent.tool_count"] == 2.0
    assert metrics["coding_agent.unique_tools"] == 2.0
    assert metrics["coding_agent.bash_ratio"] == 0.5


