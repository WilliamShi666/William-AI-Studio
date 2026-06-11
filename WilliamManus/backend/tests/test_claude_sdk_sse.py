"""
Tests for Claude SDK → WilliamManus SSE format conversion.

The ClaudeSDKSSEAdapter converts Claude Agent SDK message dicts
(as received from the sandbox Claude Agent Service) into the
SSE format expected by the WilliamManus frontend.

SSE format contract (from sse_adapter.py):
{
    "sequence": int,
    "message_id": str | null,
    "thread_id": str,
    "type": "assistant" | "tool" | "status",
    "is_llm_message": bool,
    "content": JSON string,
    "metadata": JSON string (with stream_status + thread_run_id),
    "created_at": ISO timestamp,
    "updated_at": ISO timestamp
}
"""

import json

import pytest

from agentscope_integration.claude_sdk_sse import ClaudeSDKSSEAdapter


class TestClaudeSDKSSEAdapter:
    """Tests for SSE format conversion from Claude SDK messages."""

    # ── helpers ──

    @staticmethod
    def _adapter(thread_id="thread-1", thread_run_id="run-1"):
        return ClaudeSDKSSEAdapter(
            thread_id=thread_id, thread_run_id=thread_run_id
        )

    @staticmethod
    def _parse_sse(sse_dict):
        """Decode content and metadata JSON strings back to dicts for assertions."""
        return {
            **sse_dict,
            "content": json.loads(sse_dict["content"]),
            "metadata": json.loads(sse_dict["metadata"]),
        }

    # ── StreamEvent: text delta ──

    def test_stream_event_text_delta_produces_chunk(self):
        """A stream_event with text_delta should produce type=assistant, stream_status=chunk."""
        adapter = self._adapter()
        msg = {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hello"}}}

        result = self._parse_sse(adapter.convert(msg))

        assert result["type"] == "assistant"
        assert result["is_llm_message"] is True
        assert result["metadata"]["stream_status"] == "chunk"
        assert result["content"]["content"] == "Hello"
        assert result["message_id"] is None  # chunks have no message_id

    def test_stream_event_text_delta_preserves_thread_info(self):
        """SSE output must carry thread_id and thread_run_id in metadata."""
        adapter = self._adapter(thread_id="t-42", thread_run_id="r-99")
        msg = {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "x"}}}

        result = self._parse_sse(adapter.convert(msg))

        assert result["thread_id"] == "t-42"
        assert result["metadata"]["thread_run_id"] == "r-99"

    # ── StreamEvent: thinking delta ──

    def test_stream_event_thinking_delta_produces_reasoning_chunk(self):
        """A stream_event with thinking_delta should have stream_status=reasoning_chunk."""
        adapter = self._adapter()
        msg = {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "Let me think..."}}}

        result = self._parse_sse(adapter.convert(msg))

        assert result["type"] == "assistant"
        assert result["metadata"]["stream_status"] == "reasoning_chunk"
        assert result["content"]["reasoning_content"] == "Let me think..."

    # ── StreamEvent: tool_use start ──

    def test_stream_event_tool_use_start(self):
        """content_block_start with tool_use produces tool_call_chunk status."""
        adapter = self._adapter()
        msg = {
            "type": "stream_event",
            "event": {
                "type": "content_block_start",
                "content_block": {"type": "tool_use", "id": "call_1", "name": "Bash", "input": {}},
            },
        }

        result = self._parse_sse(adapter.convert(msg))

        assert result["type"] == "assistant"
        assert result["metadata"]["stream_status"] == "tool_call_chunk"


    def test_stream_event_from_subagent_preserves_normal_chunk_status(self):
        """Subagent stream events should remain visible in normal Claude SDK chat streams."""
        adapter = self._adapter()
        msg = {
            "type": "stream_event",
            "parent_tool_use_id": "agent_call_1",
            "event": {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "Subagent finding"},
            },
        }

        result = self._parse_sse(adapter.convert(msg))

        assert result["metadata"]["stream_status"] == "chunk"
        assert result["metadata"]["parent_tool_use_id"] == "agent_call_1"
        assert result["metadata"]["activity_owner"] == "claude_sdk_subagent"
        assert result["content"]["content"] == "Subagent finding"

    def test_agent_tool_use_start_preserves_tool_call_chunk_status(self):
        """Agent tool invocations should remain normal tool calls with teammate metadata."""
        adapter = self._adapter()
        msg = {
            "type": "stream_event",
            "event": {
                "type": "content_block_start",
                "content_block": {
                    "type": "tool_use",
                    "id": "agent_call_1",
                    "name": "Agent",
                    "input": {"subagent_type": "teammate-1", "description": "Research docs"},
                },
            },
        }

        result = self._parse_sse(adapter.convert(msg))

        assert result["metadata"]["stream_status"] == "tool_call_chunk"
        assert result["metadata"]["activity_owner"] == "claude_sdk_subagent"
        assert result["metadata"]["subagent_tool_call_id"] == "agent_call_1"
        assert result["content"]["tool_calls"][0]["function"]["name"] == "Agent"

    def test_agent_tool_use_inside_assistant_message_is_marked_as_subagent_start(self):
        """Non-partial AssistantMessage Agent tool_use still creates a visible teammate."""
        adapter = self._adapter()
        msg = {
            "type": "assistant",
            "message": {
                "id": "msg_agent_start",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "agent_call_2",
                        "name": "Agent",
                        "input": {"agent": "teammate-2", "prompt": "Write report"},
                    }
                ],
            },
        }

        result = self._parse_sse(adapter.convert(msg))

        assert result["type"] == "assistant"
        assert result["metadata"]["stream_status"] == "tool_call_chunk"
        assert result["metadata"]["activity_owner"] == "claude_sdk_subagent"
        assert result["metadata"]["subagent_tool_call_id"] == "agent_call_2"
        assert result["content"]["tool_calls"][0]["function"]["name"] == "Agent"

    def test_agent_tool_result_after_assistant_message_start_is_routed_to_subagent_activity(self):
        """Agent tool result must remain tied to the Agent tool id even without partial start."""
        adapter = self._adapter()
        adapter.convert({
            "type": "assistant",
            "message": {
                "id": "msg_agent_start",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "agent_call_2",
                        "name": "Agent",
                        "input": {"agent": "teammate-2", "prompt": "Return token"},
                    }
                ],
            },
        })
        result_msg = {
            "type": "user",
            "parent_tool_use_id": "agent_call_2",
            "tool_use_result": "BETA_SUBAGENT_OK",
            "message": {"content": []},
        }

        result = self._parse_sse(adapter.convert(result_msg))

        assert result["type"] == "tool"
        assert result["content"]["tool_name"] == "Agent"
        assert result["content"]["tool_call_id"] == "agent_call_2"
        assert result["content"]["result"] == "BETA_SUBAGENT_OK"
        assert result["metadata"]["activity_owner"] == "claude_sdk_subagent"
        assert result["metadata"]["parent_tool_use_id"] == "agent_call_2"

    def test_agent_tool_result_is_routed_to_subagent_activity(self):
        """Agent tool results should be visible in the corresponding teammate panel."""
        adapter = self._adapter()
        start_msg = {
            "type": "stream_event",
            "event": {
                "type": "content_block_start",
                "content_block": {
                    "type": "tool_use",
                    "id": "agent_call_1",
                    "name": "Agent",
                    "input": {"agent": "teammate-1", "prompt": "Return token"},
                },
            },
        }
        result_msg = {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "agent_call_1",
                        "content": "ALPHA_SUBAGENT_OK",
                    }
                ]
            },
        }

        adapter.convert(start_msg)
        result = self._parse_sse(adapter.convert(result_msg))

        assert result["type"] == "tool"
        assert result["content"]["tool_name"] == "Agent"
        assert result["content"]["result"] == "ALPHA_SUBAGENT_OK"
        assert result["metadata"]["activity_owner"] == "claude_sdk_subagent"
        assert result["metadata"]["parent_tool_use_id"] == "agent_call_1"
        assert result["metadata"]["stream_status"] == "complete"


    def test_agent_tool_input_json_delta_updates_subagent_start_arguments(self):
        """Agent tool args may arrive after content_block_start as input_json_delta chunks."""
        adapter = self._adapter()
        start_msg = {
            "type": "stream_event",
            "event": {
                "type": "content_block_start",
                "index": 1,
                "content_block": {
                    "type": "tool_use",
                    "id": "agent_call_delta",
                    "name": "Agent",
                    "input": {},
                },
            },
        }
        delta_msg = {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "index": 1,
                "delta": {
                    "type": "input_json_delta",
                    "partial_json": '{"agent":"teammate-1","prompt":"Return ALPHA"}',
                },
            },
        }

        adapter.convert(start_msg)
        result = self._parse_sse(adapter.convert(delta_msg))

        assert result["type"] == "assistant"
        assert result["metadata"]["stream_status"] == "tool_call_chunk"
        assert result["metadata"]["activity_owner"] == "claude_sdk_subagent"
        assert result["metadata"]["subagent_tool_call_id"] == "agent_call_delta"
        tool_call = result["content"]["tool_calls"][0]
        assert tool_call["id"] == "agent_call_delta"
        assert tool_call["function"]["name"] == "Agent"
        assert json.loads(tool_call["function"]["arguments"])["agent"] == "teammate-1"
        assert json.loads(tool_call["function"]["arguments"])["prompt"] == "Return ALPHA"


    def test_agent_tool_input_json_delta_wraps_object_member_fragments(self):
        """Some SDK streams emit input_json_delta as object members without braces."""
        adapter = self._adapter()
        start_msg = {
            "type": "stream_event",
            "event": {
                "type": "content_block_start",
                "index": 1,
                "content_block": {
                    "type": "tool_use",
                    "id": "agent_call_member_delta",
                    "name": "Agent",
                    "input": {},
                },
            },
        }
        delta_msg = {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "index": 1,
                "delta": {
                    "type": "input_json_delta",
                    "partial_json": '"agent":"teammate-3","prompt":"Return MEMBER_OK"',
                },
            },
        }

        adapter.convert(start_msg)
        result = self._parse_sse(adapter.convert(delta_msg))

        args = json.loads(result["content"]["tool_calls"][0]["function"]["arguments"])
        assert args["agent"] == "teammate-3"
        assert args["prompt"] == "Return MEMBER_OK"

    # ── AssistantMessage: complete with text ──

    def test_assistant_message_text_complete(self):
        """A full AssistantMessage with text produces stream_status=complete with message_id."""
        adapter = self._adapter()
        msg = {
            "type": "assistant",
            "message": {
                "id": "msg_1",
                "content": [{"type": "text", "text": "Here is the result."}],
            },
        }

        result = self._parse_sse(adapter.convert(msg))

        assert result["type"] == "assistant"
        assert result["metadata"]["stream_status"] == "complete"
        assert result["message_id"] is not None  # complete messages have a message_id
        assert result["content"]["content"] == "Here is the result."

    def test_subagent_assistant_message_text_complete_preserves_parent_owner(self):
        """A complete subagent AssistantMessage should route to the selected teammate."""
        adapter = self._adapter()
        msg = {
            "type": "assistant",
            "parent_tool_use_id": "agent_call_1",
            "message": {
                "id": "msg_subagent_1",
                "content": [{"type": "text", "text": "SUBAGENT_FINAL_OK"}],
            },
        }

        result = self._parse_sse(adapter.convert(msg))

        assert result["type"] == "assistant"
        assert result["metadata"]["stream_status"] == "complete"
        assert result["metadata"]["activity_owner"] == "claude_sdk_subagent"
        assert result["metadata"]["parent_tool_use_id"] == "agent_call_1"
        assert result["content"]["content"] == "SUBAGENT_FINAL_OK"

    # ── AssistantMessage: complete with tool_use ──

    def test_assistant_message_with_tool_use(self):
        """AssistantMessage with ToolUseBlock produces content with tool_calls array."""
        adapter = self._adapter()
        msg = {
            "type": "assistant",
            "message": {
                "id": "msg_2",
                "content": [
                    {"type": "text", "text": "Let me check that."},
                    {"type": "tool_use", "id": "call_abc", "name": "Read", "input": {"file_path": "/workspace/foo.txt"}},
                ],
            },
        }

        result = self._parse_sse(adapter.convert(msg))

        assert result["metadata"]["stream_status"] == "complete"
        assert result["content"]["content"] == "Let me check that."
        tool_calls = result["content"]["tool_calls"]
        assert len(tool_calls) == 1
        assert tool_calls[0]["id"] == "call_abc"
        assert tool_calls[0]["type"] == "function"
        assert tool_calls[0]["function"]["name"] == "Read"
        # arguments must be a JSON string (frontend contract)
        assert isinstance(json.loads(tool_calls[0]["function"]["arguments"]), dict)

    # ── UserMessage: tool_result ──


    def test_user_message_top_level_tool_use_result_is_converted_to_tool_result(self):
        """Claude SDK may carry tool output in top-level UserMessage.tool_use_result."""
        adapter = self._adapter()
        adapter.convert({
            "type": "stream_event",
            "event": {
                "type": "content_block_start",
                "content_block": {
                    "type": "tool_use",
                    "id": "call_write_1",
                    "name": "Write",
                    "input": {"file_path": "/workspace/a.md", "content": "ok"},
                },
            },
        })
        msg = {
            "type": "user",
            "parent_tool_use_id": "call_write_1",
            "tool_use_result": {"filePath": "/workspace/a.md", "success": True},
            "message": {"content": []},
        }

        result = self._parse_sse(adapter.convert(msg))

        assert result["type"] == "tool"
        assert result["content"]["tool_name"] == "Write"
        assert result["content"]["tool_call_id"] == "call_write_1"
        assert json.loads(result["content"]["result"]) == {
            "filePath": "/workspace/a.md",
            "success": True,
        }
        assert result["metadata"]["stream_status"] == "complete"
        assert "activity_owner" not in result["metadata"]

    def test_user_message_tool_result(self):
        """UserMessage with ToolResultBlock produces type=tool, stream_status=complete."""
        adapter = self._adapter()
        # First, feed a tool_use so the adapter knows the tool name
        tool_use_msg = {
            "type": "assistant",
            "message": {
                "id": "msg_pre",
                "content": [{"type": "tool_use", "id": "call_abc", "name": "Read", "input": {"file_path": "/f"}}],
            },
        }
        adapter.convert(tool_use_msg)

        # Then, the tool result
        msg = {
            "type": "user",
            "message": {
                "content": [{"type": "tool_result", "tool_use_id": "call_abc", "content": "file contents here"}],
            },
        }

        result = self._parse_sse(adapter.convert(msg))

        assert result["type"] == "tool"
        assert result["is_llm_message"] is False
        assert result["metadata"]["stream_status"] == "complete"
        assert result["content"]["tool_name"] == "Read"
        assert result["content"]["tool_call_id"] == "call_abc"
        assert result["content"]["result"] == "file contents here"

    # ── ResultMessage: terminal ──

    def test_result_message_produces_completed_status(self):
        """ResultMessage should produce type=status, status=completed."""
        adapter = self._adapter()
        msg = {"type": "result", "subtype": "success", "result": "Task done."}

        result = adapter.convert(msg)

        assert result["type"] == "status"
        # status messages have a flat "status" field at top level
        assert result.get("status") == "completed"

    def test_result_message_is_error_when_subtype_is_error(self):
        """ResultMessage with is_error=true produces status=failed."""
        adapter = self._adapter()
        msg = {"type": "result", "subtype": "error_during_execution", "is_error": True}

        result = adapter.convert(msg)

        assert result["type"] == "status"
        assert result.get("status") == "failed"

    # ── SystemMessage: session init ──

    def test_system_init_message_is_skipped(self):
        """system.init messages should not produce SSE output (internal only)."""
        adapter = self._adapter()
        msg = {"type": "system", "subtype": "init", "session_id": "sess_1"}

        result = adapter.convert(msg)

        assert result is None

    # ── sequence monotonic ──

    def test_sequence_is_monotonic(self):
        """Each convert() call increments the sequence number."""
        adapter = self._adapter()
        msg = {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "a"}}}

        r1 = adapter.convert(msg)
        r2 = adapter.convert(msg)
        r3 = adapter.convert(msg)

        assert r1["sequence"] == 0
        assert r2["sequence"] == 1
        assert r3["sequence"] == 2

    # ── unknown message types ──

    def test_unknown_message_type_is_skipped(self):
        """Messages with unhandled types should return None (skip)."""
        adapter = self._adapter()
        msg = {"type": "task_notification", "payload": "progress update"}

        result = adapter.convert(msg)

        assert result is None
