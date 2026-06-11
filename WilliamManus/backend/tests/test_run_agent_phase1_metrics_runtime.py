from __future__ import annotations

import json

from run_agent_background import _build_phase1_tool_events_from_responses


def _status_payload(
    *,
    status_type: str,
    function_name: str,
    message: str | None = None,
) -> str:
    content = {
        "role": "assistant",
        "status_type": status_type,
        "function_name": function_name,
        "arguments": json.dumps({"path": "/workspace/demo.txt"}),
        "xml_tag_name": function_name,
        "tool_index": 0,
    }
    if message is not None:
        content["message"] = message
    return json.dumps(
        {
            "type": "status",
            "content": json.dumps(content),
        }
    )


def test_build_phase1_tool_events_uses_completed_and_failed_lifecycle_only() -> None:
    responses = [
        _status_payload(status_type="tool_started", function_name="execute_command"),
        _status_payload(status_type="tool_completed", function_name="execute_command"),
        _status_payload(status_type="tool_failed", function_name="read_file"),
    ]

    result = _build_phase1_tool_events_from_responses(responses)

    assert result == [
        {"tool_name": "execute_command", "success": True},
        {"tool_name": "read_file", "success": False},
    ]


def test_build_phase1_tool_events_ignores_terminal_write_file_compensation() -> None:
    responses = [
        _status_payload(status_type="tool_completed", function_name="execute_command"),
        _status_payload(
            status_type="tool_failed",
            function_name="write_file",
            message="Run ended with status 'failed' before write_file finalized.",
        ),
    ]

    result = _build_phase1_tool_events_from_responses(responses)

    assert result == [{"tool_name": "execute_command", "success": True}]
