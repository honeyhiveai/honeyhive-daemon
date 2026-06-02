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
