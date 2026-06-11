"""
LTM MCP Server — bridges ReMe Long-Term Memory for Claude Code.

Exposes retrieve_long_term_memory and record_long_term_memory tools
so Claude Code running inside a sandbox can access the same Qdrant-backed
ReMe LTM that AgentScope uses.

The MCP server runs inside the WilliamManus backend process (not in the
sandbox). Claude Code connects via MCP SSE protocol.

Write path: record_long_term_memory is exposed but Post-Run Review
(not the Agent) is the primary writer — same policy as AgentScope.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from agentscope.tool import ToolResponse

from .long_term.ltm_factory import create_long_term_memory

logger = logging.getLogger(__name__)


def _extract_tool_response_text(response: Any) -> str:
    """Extract text content from a ToolResponse or similar object."""
    if response is None:
        return ""

    content = getattr(response, "content", None)
    if isinstance(content, list):
        lines = []
        for block in content:
            if isinstance(block, dict):
                text = str(block.get("text") or "").strip()
                if text:
                    lines.append(text)
        return "\n".join(lines).strip()

    return str(response).strip()


class LTMMCPServer:
    """MCP server bridging ReMe LTM for Claude Code.

    Lazily initializes LTM via create_long_term_memory() on first use.
    """

    def __init__(self, thread_id: str) -> None:
        self.thread_id = thread_id
        self._ltm: Optional[Any] = None

    async def _ensure_ltm(self):
        """Return the LTM instance, creating it lazily if needed."""
        if self._ltm is None:
            try:
                self._ltm = create_long_term_memory(thread_id=self.thread_id)
            except Exception as exc:
                logger.error("[LTMMCPServer] LTM init failed: %s", exc)
                self._ltm = None
        return self._ltm

    async def retrieve_long_term_memory(
        self,
        keywords: list[str],
        limit: int = 5,
    ) -> str:
        """Retrieve relevant long-term memories by keywords.

        Args:
            keywords: Search keywords for memory retrieval.
            limit: Max results to return (default 5).

        Returns:
            XML-tagged memory sections or a status message.
        """
        ltm = await self._ensure_ltm()
        if ltm is None:
            return "Long-term memory is not enabled. Set AGENTSCOPE_LTM_ENABLED=true to activate."

        try:
            result = await ltm.retrieve_from_memory(keywords, limit=limit)
            return _extract_tool_response_text(result)
        except Exception as exc:
            logger.error("[LTMMCPServer] retrieve failed: %s", exc)
            return f"Memory retrieval error: {exc}"

    async def record_long_term_memory(
        self,
        thinking: str,
        content: list[str],
    ) -> str:
        """Record new long-term memories.

        Args:
            thinking: Your reasoning about why these are worth recording.
            content: Memory strings to store.

        Returns:
            Confirmation message with record counts.
        """
        ltm = await self._ensure_ltm()
        if ltm is None:
            return "Long-term memory is not enabled. Set AGENTSCOPE_LTM_ENABLED=true to activate."

        try:
            result = await ltm.record_to_memory(thinking=thinking, content=content)
            return _extract_tool_response_text(result)
        except Exception as exc:
            logger.error("[LTMMCPServer] record failed: %s", exc)
            return f"Memory recording error: {exc}"


def create_ltm_mcp_server(thread_id: str) -> dict:
    """Create an MCP server configuration dict for Claude Code's mcp_servers.

    Returns a dict suitable for passing to ClaudeAgentOptions(mcp_servers=...).
    The returned config references the LTMMCPServer instance.

    In production, the actual MCP transport (SSE/stdio) is wired by the
    sandbox bridge. This factory returns the logical server config.
    """
    server = LTMMCPServer(thread_id=thread_id)

    return {
        "ltm": {
            "server": server,
            "transport": "sse",
        },
    }
