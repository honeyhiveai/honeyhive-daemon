"""Tests for server-side HoneyHive evaluator definitions."""

from __future__ import annotations

from evaluators.definitions import EVALUATORS


def _run_python_evaluator(name: str, event: dict) -> object:
    evaluator = next(evaluator for evaluator in EVALUATORS if evaluator["name"] == name)
    scope = {"event": event}
    exec(str(evaluator["criteria"]), scope)
    return scope["result"]


def test_python_evaluator_code_avoids_private_helper_names() -> None:
    """HoneyHive's Python sandbox rejects underscore-prefixed helper names."""
    python_evaluators = [
        evaluator for evaluator in EVALUATORS if evaluator.get("type") == "PYTHON"
    ]

    for evaluator in python_evaluators:
        criteria = str(evaluator["criteria"])
        assert "def _" not in criteria, evaluator["name"]
        assert "_normalize_tool" not in criteria, evaluator["name"]
        assert "_extract_events_from_artifact" not in criteria, evaluator["name"]


def test_python_evaluator_code_assigns_result() -> None:
    """HoneyHive executes Python evaluators as scripts and reads `result`."""
    python_evaluators = [
        evaluator for evaluator in EVALUATORS if evaluator.get("type") == "PYTHON"
    ]

    for evaluator in python_evaluators:
        assert "result = evaluate(event)" in str(evaluator["criteria"]), evaluator["name"]


def test_task_completion_runs_on_session_start() -> None:
    task_completion = next(
        evaluator
        for evaluator in EVALUATORS
        if evaluator["name"] == "Coding Agent - Task Completion"
    )

    filters = task_completion["filters"]["filterArray"]
    assert {"field": "event_type", "operator": "is", "value": "session", "type": "string"} in filters
    assert {
        "field": "event_name",
        "operator": "is",
        "value": "session.start",
        "type": "string",
    } in filters


def test_python_evaluators_read_nested_claude_tool_use_blocks() -> None:
    event = {
        "outputs": {
            "artifact": {
                "content": [
                    {
                        "type": "assistant",
                        "message": {
                            "role": "assistant",
                            "content": [
                                {"type": "tool_use", "name": "Read", "id": "toolu_1"},
                                {"type": "tool_use", "name": "Bash", "id": "toolu_2"},
                            ],
                        },
                    }
                ]
            }
        }
    }

    assert _run_python_evaluator("Coding Agent - Bash Ratio", event) == 0.5
    assert _run_python_evaluator("Coding Agent - Tool to Model Ratio", event) == 2.0

