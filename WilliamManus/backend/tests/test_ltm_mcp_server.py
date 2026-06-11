"""
Tests for LTMMCPServer — MCP server bridging ReMe LTM for Claude Code.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from agentscope_integration.memory.ltm_mcp_server import (
    LTMMCPServer,
    create_ltm_mcp_server,
    _extract_tool_response_text,
)


class TestExtractToolResponseText:
    """Tests for _extract_tool_response_text helper."""

    def test_extracts_text_from_tool_response_content_list(self):
        """ToolResponse with content=[{type:text, text:...}] extracts text."""

        class FakeResponse:
            content = [{"type": "text", "text": "memory content here"}]
            metadata = {}

        result = _extract_tool_response_text(FakeResponse())
        assert result == "memory content here"

    def test_extracts_text_from_multiple_blocks(self):
        """Multiple content blocks are joined with newlines."""

        class FakeResponse:
            content = [
                {"type": "text", "text": "first block"},
                {"type": "text", "text": "second block"},
            ]
            metadata = {}

        result = _extract_tool_response_text(FakeResponse())
        assert result == "first block\nsecond block"

    def test_returns_empty_string_for_none(self):
        assert _extract_tool_response_text(None) == ""

    def test_falls_back_to_str(self):
        """Non-ToolResponse objects fall back to str()."""

        class UnknownResponse:
            pass

        result = _extract_tool_response_text(UnknownResponse())
        assert result != ""


class TestLTMMCPServer:
    """Tests for LTMMCPServer tool implementations."""

    @pytest.fixture
    def mock_ltm(self):
        ltm = AsyncMock()
        ltm.retrieve_from_memory = AsyncMock()
        ltm.record_to_memory = AsyncMock()
        return ltm

    @pytest.fixture
    def server(self, mock_ltm):
        srv = LTMMCPServer(thread_id="thread-1")
        srv._ltm = mock_ltm
        return srv

    # ── retrieve_long_term_memory ──

    @pytest.mark.asyncio
    async def test_retrieve_delegates_to_ltm(self, server, mock_ltm):
        """retrieve_long_term_memory calls ltm.retrieve_from_memory with keywords."""

        class FakeResponse:
            content = [{"type": "text", "text": "<task_experience>\nfound\n</task_experience>"}]
            metadata = {}

        mock_ltm.retrieve_from_memory.return_value = FakeResponse()

        result = await server.retrieve_long_term_memory(
            keywords=["python", "error"],
            limit=3,
        )

        mock_ltm.retrieve_from_memory.assert_called_once_with(["python", "error"], limit=3)
        assert "task_experience" in result

    @pytest.mark.asyncio
    async def test_retrieve_returns_message_when_no_ltm(self):
        """When LTM is not configured, retrieve returns a helpful message."""
        srv = LTMMCPServer(thread_id="thread-no-ltm")
        srv._ltm = None

        result = await srv.retrieve_long_term_memory(keywords=["test"])

        assert "not enabled" in result.lower()

    # ── record_long_term_memory ──

    @pytest.mark.asyncio
    async def test_record_delegates_to_ltm(self, server, mock_ltm):
        """record_long_term_memory calls ltm.record_to_memory."""

        class FakeResponse:
            content = [{"type": "text", "text": "recorded 2 memories"}]
            metadata = {"recorded_task": 1, "recorded_tool": 1}

        mock_ltm.record_to_memory.return_value = FakeResponse()

        result = await server.record_long_term_memory(
            thinking="Useful Python tip",
            content=["Always use async context managers for resource cleanup."],
        )

        mock_ltm.record_to_memory.assert_called_once_with(
            thinking="Useful Python tip",
            content=["Always use async context managers for resource cleanup."],
        )
        assert "recorded" in result

    @pytest.mark.asyncio
    async def test_record_returns_message_when_no_ltm(self):
        """When LTM is not configured, record returns a helpful message."""
        srv = LTMMCPServer(thread_id="thread-no-ltm")
        srv._ltm = None

        result = await srv.record_long_term_memory(
            thinking="test",
            content=["test memory"],
        )

        assert "not enabled" in result.lower()

    # ── _ensure_ltm ──

    @pytest.mark.asyncio
    async def test_ensure_ltm_creates_when_none(self):
        """_ensure_ltm calls create_long_term_memory when _ltm is None."""
        srv = LTMMCPServer(thread_id="thread-new")

        with patch(
            "agentscope_integration.memory.ltm_mcp_server.create_long_term_memory"
        ) as mock_create:
            mock_create.return_value = None
            result = await srv._ensure_ltm()
            mock_create.assert_called_once_with(thread_id="thread-new")
            assert result is None


class TestCreateLtmMcpServer:
    """Tests for create_ltm_mcp_server factory function."""

    def test_returns_mcp_server_config_dict(self):
        """create_ltm_mcp_server returns a dict with the expected MCP config shape."""
        config = create_ltm_mcp_server(thread_id="thread-cfg")
        assert isinstance(config, dict)
        assert "ltm" in config
        assert "transport" in config["ltm"] or "url" in config["ltm"] or "server" in config["ltm"]
