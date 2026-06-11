"""
Tests for claude_agent_service.py — the sandbox-resident Claude Agent Service.

Since claude_agent_sdk is not installed in the dev environment, these tests
verify the message serialization, options construction, and protocol handling
with the SDK mocked out.
"""

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ── Message serialization tests ──


class TestMessageToJson:
    """Tests for message_to_json — converting SDK messages to JSON lines."""

    def test_stream_event_text_delta(self):
        """StreamEvent with text_delta serializes with all key fields."""
        from agentscope_integration.claude_agent_service import message_to_json

        msg = {
            "type": "stream_event",
            "event": {
                "type": "content_block_delta",
                "delta": {"type": "text_delta", "text": "Hello"},
            },
        }

        result = json.loads(message_to_json(msg))
        assert result["type"] == "stream_event"
        assert result["event"]["delta"]["text"] == "Hello"


    def test_typed_stream_event_preserves_parent_tool_use_id(self):
        """Typed SDK StreamEvent serialization must preserve subagent parent tool IDs."""
        from agentscope_integration.claude_agent_service import message_to_json

        StreamEvent = type("StreamEvent", (), {})
        msg = StreamEvent()
        msg.event = {
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": "inside subagent"},
        }
        msg.parent_tool_use_id = "agent_call_1"
        msg.uuid = "event-1"
        msg.session_id = "session-1"

        result = json.loads(message_to_json(msg))

        assert result["type"] == "stream_event"
        assert result["parent_tool_use_id"] == "agent_call_1"
        assert result["uuid"] == "event-1"
        assert result["session_id"] == "session-1"

    def test_assistant_message_with_tool_use(self):
        """AssistantMessage with ToolUseBlock serializes correctly."""
        from agentscope_integration.claude_agent_service import message_to_json

        msg = {
            "type": "assistant",
            "message": {
                "id": "msg_1",
                "content": [
                    {"type": "text", "text": "Let me check."},
                    {"type": "tool_use", "id": "call_1", "name": "Read", "input": {"file_path": "/f"}},
                ],
            },
        }

        result = json.loads(message_to_json(msg))
        assert result["type"] == "assistant"
        assert len(result["message"]["content"]) == 2

    def test_result_message(self):
        """ResultMessage preserves cost and usage info."""
        from agentscope_integration.claude_agent_service import message_to_json

        msg = {
            "type": "result",
            "subtype": "success",
            "result": "Done.",
            "total_cost_usd": 0.05,
            "usage": {"input_tokens": 100, "output_tokens": 50},
            "num_turns": 3,
            "duration_ms": 5000,
        }

        result = json.loads(message_to_json(msg))
        assert result["type"] == "result"
        assert result["total_cost_usd"] == 0.05

    def test_user_message_tool_result(self):
        """UserMessage with ToolResultBlock serializes correctly."""
        from agentscope_integration.claude_agent_service import message_to_json

        msg = {
            "type": "user",
            "message": {
                "content": [{"type": "tool_result", "tool_use_id": "call_1", "content": "file contents"}],
            },
        }

        result = json.loads(message_to_json(msg))
        assert result["type"] == "user"

    def test_typed_user_message_preserves_tool_result_block_and_metadata(self):
        """Typed SDK ToolResultBlock must not be serialized as unknown."""
        from agentscope_integration.claude_agent_service import message_to_json

        ToolResultBlock = type("ToolResultBlock", (), {})
        block = ToolResultBlock()
        block.tool_use_id = "call_write_1"
        block.content = "File created successfully"
        block.is_error = False

        UserMessage = type("UserMessage", (), {})
        msg = UserMessage()
        msg.content = [block]
        msg.uuid = "user-msg-1"
        msg.parent_tool_use_id = "parent-agent-call"
        msg.tool_use_result = {"filePath": "/workspace/a.md", "success": True}

        result = json.loads(message_to_json(msg))

        assert result["type"] == "user"
        assert result["uuid"] == "user-msg-1"
        assert result["parent_tool_use_id"] == "parent-agent-call"
        assert result["tool_use_result"] == {"filePath": "/workspace/a.md", "success": True}
        assert result["message"]["content"] == [
            {
                "type": "tool_result",
                "tool_use_id": "call_write_1",
                "content": "File created successfully",
                "is_error": False,
            }
        ]

    def test_typed_assistant_message_preserves_tool_use_block(self):
        """Typed SDK ToolUseBlock must serialize as tool_use even without a .type attr."""
        from agentscope_integration.claude_agent_service import message_to_json

        ToolUseBlock = type("ToolUseBlock", (), {})
        block = ToolUseBlock()
        block.id = "call_write_1"
        block.name = "Write"
        block.input = {"file_path": "/workspace/a.md", "content": "ok"}

        AssistantMessage = type("AssistantMessage", (), {})
        msg = AssistantMessage()
        msg.content = [block]
        msg.model = "deepseek-v4-pro"
        msg.message_id = "msg-1"
        msg.session_id = "session-1"
        msg.uuid = "assistant-uuid"

        result = json.loads(message_to_json(msg))

        assert result["type"] == "assistant"
        assert result["message"]["id"] == "msg-1"
        assert result["message"]["content"] == [
            {
                "type": "tool_use",
                "id": "call_write_1",
                "name": "Write",
                "input": {"file_path": "/workspace/a.md", "content": "ok"},
            }
        ]


# ── Options construction tests ──


class TestBuildAgentOptions:
    """Tests for build_agent_options — constructing ClaudeAgentOptions from config."""

    def test_builds_basic_options(self):
        """build_agent_options returns a dict with core fields set."""
        from agentscope_integration.claude_agent_service import build_agent_options

        opts = build_agent_options(
            system_prompt="You are helpful.",
            model="deepseek-v4-pro[1m]",
            cwd="/workspace",
        )

        assert opts["system_prompt"] == "You are helpful."
        assert opts["model"] == "deepseek-v4-pro[1m]"
        assert opts["cwd"] == "/workspace"
        assert opts["permission_mode"] == "bypassPermissions"
        assert opts["include_partial_messages"] is True

    def test_request_can_override_permission_mode(self):
        """Local namespace wrappers can avoid root+bypass CLI rejection."""
        from agentscope_integration.claude_agent_service import build_agent_options

        opts = build_agent_options(permission_mode="acceptEdits")

        assert opts["permission_mode"] == "acceptEdits"

    def test_rejects_unknown_permission_mode(self):
        """Permission-related request fields must fail closed."""
        from agentscope_integration.claude_agent_service import build_agent_options

        with pytest.raises(ValueError, match="Unsupported permission_mode"):
            build_agent_options(permission_mode="futureGodMode")

    def test_includes_env_vars(self):
        """Env vars are passed through to options."""
        from agentscope_integration.claude_agent_service import build_agent_options

        opts = build_agent_options(
            system_prompt="test",
            env={"ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic"},
        )

        assert opts["env"]["ANTHROPIC_BASE_URL"] == "https://api.deepseek.com/anthropic"

    def test_includes_mcp_servers(self):
        """MCP server configs are passed through."""
        from agentscope_integration.claude_agent_service import build_agent_options

        mcp_config = {"ltm": {"url": "http://host:8080/sse"}}
        opts = build_agent_options(
            system_prompt="test",
            mcp_servers=mcp_config,
        )

        assert opts["mcp_servers"] == mcp_config

    def test_default_effort_is_max(self):
        """Default effort level is 'max'."""
        from agentscope_integration.claude_agent_service import build_agent_options

        opts = build_agent_options(system_prompt="test")
        assert opts["effort"] == "max"

    def test_empty_system_prompt_does_not_override_claude_code_default(self):
        """Do not pass an empty custom system prompt to ClaudeAgentOptions."""
        from agentscope_integration.claude_agent_service import build_agent_options

        opts = build_agent_options(system_prompt="   ")

        assert "system_prompt" not in opts

    def test_system_prompt_is_authoritative_not_claude_code_identity_preset(self):
        """A custom WilliamManus identity prompt must not be subordinate to Claude Code's preset identity."""
        from agentscope_integration.claude_agent_service import build_agent_options

        opts = build_agent_options(
            system_prompt="You are Roys Alpha.\nORCHESTRATOR\nget_available_skills()"
        )

        assert opts["system_prompt"] == (
            "You are Roys Alpha.\nORCHESTRATOR\nget_available_skills()"
        )

    def test_enables_project_skills_by_default(self):
        """Claude SDK should discover project Agent Skills from the sandbox workspace."""
        from agentscope_integration.claude_agent_service import build_agent_options

        opts = build_agent_options(system_prompt="test")

        assert opts["setting_sources"] == ["project"]
        assert opts["skills"] == "all"

    def test_enables_ten_equal_power_teammate_subagents_by_default(self):
        """Claude SDK should expose 10 generic same-capability teammate subagents."""
        from agentscope_integration.claude_agent_service import build_agent_options

        opts = build_agent_options(
            system_prompt="You are Roys Alpha.",
            model="deepseek-v4-pro[1m]",
            permission_mode="bypassPermissions",
            effort="max",
            mcp_servers={"ltm": {"url": "http://host:8080/sse"}},
        )

        assert "Agent" in opts["allowed_tools"]
        agents = opts["agents"]
        assert list(agents.keys()) == [f"teammate-{idx}" for idx in range(1, 11)]

        first = agents["teammate-1"]
        assert first.description.startswith("Generic Roys Alpha teammate 1")
        assert "teammate 1" in first.prompt
        assert "same configuration, tools, skills, and permissions" in first.prompt
        assert first.model == "deepseek-v4-pro[1m]"
        assert first.tools is None
        assert first.disallowedTools == ["Agent", "Task"]
        assert opts["skills"] == "all"
        assert first.skills is None
        assert first.mcpServers == [{"ltm": {"url": "http://host:8080/sse"}}]
        assert first.effort == "max"
        assert first.permissionMode == "bypassPermissions"

    def test_can_disable_default_teammates_for_protocol_unit_tests(self):
        """Tests and fallback callers can still build options without subagents."""
        from agentscope_integration.claude_agent_service import build_agent_options

        opts = build_agent_options(enable_default_teammates=False)

        assert "agents" not in opts
        assert "allowed_tools" not in opts

    def test_teammate_count_is_capped_at_ten(self):
        """Shadow clone teammate profile must never expose more than 10 subagents."""
        from agentscope_integration.claude_agent_service import build_agent_options

        opts = build_agent_options(teammate_count=99)

        assert list(opts["agents"].keys()) == [f"teammate-{idx}" for idx in range(1, 11)]


# ── Service runner tests ──


class TestRunService:
    """Tests for run_service — the main stdin/stdout JSON-lines protocol loop."""

    @pytest.mark.asyncio
    async def test_run_service_streams_messages(self):
        """run_service reads request from stdin_lines and yields JSON lines."""
        from agentscope_integration.claude_agent_service import run_service

        # Simulated SDK messages
        sdk_messages = [
            {"type": "stream_event", "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hi"}}},
            {"type": "assistant", "message": {"id": "m1", "content": [{"type": "text", "text": "Hi"}]}},
            {"type": "result", "subtype": "success", "result": "Done.", "total_cost_usd": 0.01, "usage": {}, "num_turns": 1, "duration_ms": 100},
        ]

        async def _async_iter():
            for msg in sdk_messages:
                yield msg

        mock_client = AsyncMock()
        mock_client.receive_response = MagicMock(return_value=_async_iter())
        mock_client.query = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=None)

        stdin_lines = [
            json.dumps({"prompt": "Say hi", "thread_id": "t1"}),
        ]

        mock_options = MagicMock()
        output_lines = []
        with patch("agentscope_integration.claude_agent_service.ClaudeSDKClient", return_value=mock_client):
            with patch("agentscope_integration.claude_agent_service.ClaudeAgentOptions", return_value=mock_options):
                async for line in run_service(stdin_lines=iter(stdin_lines)):
                    output_lines.append(line)

        assert len(output_lines) >= 3
        parsed = [json.loads(line) for line in output_lines]
        types = [p["type"] for p in parsed]
        assert "stream_event" in types
        assert "assistant" in types
        assert "result" in types

    @pytest.mark.asyncio
    async def test_run_service_yields_error_on_exception(self):
        """When the SDK raises, an error JSON line is yielded."""
        from agentscope_integration.claude_agent_service import run_service

        with patch(
            "agentscope_integration.claude_agent_service.ClaudeSDKClient",
            side_effect=RuntimeError("API unavailable"),
        ):
            stdin_lines = [json.dumps({"prompt": "test"})]
            output_lines = []
            async for line in run_service(stdin_lines=iter(stdin_lines)):
                output_lines.append(line)

        assert len(output_lines) >= 1
        last = json.loads(output_lines[-1])
        assert last["type"] == "result"
        assert last["subtype"] == "error_during_execution"
        assert last["is_error"] is True
