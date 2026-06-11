"""Tests for MetadataAwareReActAgent — including fake-completion regression."""

import types
from unittest.mock import AsyncMock, MagicMock

import pytest
from agentscope.formatter._formatter_base import FormatterBase
from agentscope.message import Msg, TextBlock, ThinkingBlock

from agentscope_integration.agents.metadata_aware_react_agent import (
    MetadataAwareReActAgent,
)


def test_apply_response_metadata_copies_dict_metadata() -> None:
    msg = Msg(
        name="assistant",
        role="assistant",
        content=[TextBlock(type="text", text="hello")],
    )
    response = types.SimpleNamespace(
        metadata={
            "_openrouter_reasoning_details": [
                {"type": "reasoning.encrypted", "data": "sig-123"},
            ],
        },
    )

    MetadataAwareReActAgent._apply_response_metadata(msg, response)

    assert msg.metadata == response.metadata
    assert msg.metadata is not response.metadata


# ── Fake-completion regression tests ───────────────────────────────────


class _MockFormatter(FormatterBase):
    """Minimal formatter that passes Msg content through unchanged."""

    async def format(self, msgs, **kwargs):
        return [
            {"role": m.role if hasattr(m, "role") else "user", "content": str(m.content)}
            for m in msgs
        ]


class _MockResponse:
    """Simulates a non-streaming model response."""

    def __init__(self, content: list, metadata: dict | None = None):
        self.content = content
        self.metadata = metadata


class _MockModel:
    """Model that returns a pre-scripted sequence of responses."""

    stream: bool = False

    def __init__(self, *responses: list):
        self._responses = list(responses)
        self._cursor = 0

    async def __call__(self, prompt, tools=None, tool_choice=None, **kwargs):
        if self._cursor >= len(self._responses):
            # Fallback: return empty text response
            return _MockResponse([TextBlock(type="text", text="done")])
        resp = self._responses[self._cursor]
        self._cursor += 1
        return _MockResponse(resp)


class TestThinkingOnlyDoesNotExitEarly:
    """Reproduces the bug where thinking-only responses cause fake completion."""

    @pytest.mark.asyncio
    async def test_thinking_only_response_does_not_mark_completion(self):
        """Agent MUST NOT exit when model returns only thinking blocks."""
        model = _MockModel(
            # First call: thinking-only (model still reasoning, no action yet)
            [ThinkingBlock(type="thinking", thinking="Let me analyze this task...")],
            # Second call: text response (genuine completion)
            [TextBlock(type="text", text="Task completed successfully.")],
        )

        agent = MetadataAwareReActAgent(
            name="test_agent",
            sys_prompt="You are a helpful assistant.",
            model=model,
            formatter=_MockFormatter(),
            max_iters=5,
        )

        reply = await agent.reply(Msg(name="user", content="Hello", role="user"))

        # The reply should come from the SECOND call (text response),
        # not the first (thinking-only).
        text_blocks = reply.get_content_blocks("text")
        assert len(text_blocks) == 1
        assert text_blocks[0]["text"] == "Task completed successfully."

        # Model should have been called at least twice
        # (once for thinking-only, once for text)
        assert model._cursor >= 2, (
            f"Model called only {model._cursor} time(s); "
            "thinking-only response caused premature exit"
        )

    @pytest.mark.asyncio
    async def test_thinking_followed_by_tool_use_continues_loop(self):
        """After thinking, agent should proceed to execute tool calls."""
        model = _MockModel(
            # First call: thinking only
            [ThinkingBlock(type="thinking", thinking="I need to search for this...")],
            # Second call: tool_use
            [
                {
                    "type": "tool_use",
                    "id": "call_1",
                    "name": "execute_command",
                    "input": {"command": "echo hello"},
                },
            ],
            # Third call: text
            [TextBlock(type="text", text="Done.")],
        )

        # Register a dummy tool so the agent can execute tool_use blocks
        from agentscope.tool import Toolkit

        toolkit = Toolkit()

        async def _fake_execute_command(command: str) -> str:
            return f"executed: {command}"

        toolkit.register_tool_function(_fake_execute_command)

        agent = MetadataAwareReActAgent(
            name="test_agent",
            sys_prompt="You are a helpful assistant.",
            model=model,
            formatter=_MockFormatter(),
            toolkit=toolkit,
            max_iters=5,
        )

        reply = await agent.reply(Msg(name="user", content="Do something", role="user"))

        text_blocks = reply.get_content_blocks("text")
        assert len(text_blocks) >= 1
        assert model._cursor >= 3, (
            f"Model called only {model._cursor} time(s); "
            "thinking-only response caused premature exit before tool execution"
        )

    @pytest.mark.asyncio
    async def test_empty_response_triggers_retry_not_exit(self):
        """Agent should retry on completely empty response, not exit."""
        model = _MockModel(
            # First call: completely empty
            [],
            # Second call: text
            [TextBlock(type="text", text="Here is my response.")],
        )

        agent = MetadataAwareReActAgent(
            name="test_agent",
            sys_prompt="You are a helpful assistant.",
            model=model,
            formatter=_MockFormatter(),
            max_iters=5,
        )

        reply = await agent.reply(Msg(name="user", content="Hello", role="user"))

        text_blocks = reply.get_content_blocks("text")
        assert len(text_blocks) == 1
        assert text_blocks[0]["text"] == "Here is my response."
        assert model._cursor >= 2


class _MockStreamingModel:
    """Model that returns pre-scripted responses in streaming mode.

    Each call returns an async generator that yields chunks. Each chunk
    has a ``.content`` attribute (a list of content blocks), mirroring
    the AgentScope streaming protocol.
    """

    stream: bool = True

    def __init__(self, *response_chunks: list):
        # Each element is the content list for one streaming chunk.
        self._response_chunks = list(response_chunks)
        self._cursor = 0

    async def __call__(self, prompt, tools=None, tool_choice=None, **kwargs):
        if self._cursor >= len(self._response_chunks):
            return _MockStreamingResponse(
                [[TextBlock(type="text", text="done")]]
            )
        chunks = self._response_chunks[self._cursor]
        self._cursor += 1
        return _MockStreamingResponse(chunks)


class _MockStreamingResponse:
    """Simulates an async generator of content chunks."""

    def __init__(self, chunks: list):
        # chunks: list of content-block lists, e.g. [[ThinkingBlock(...)], [TextBlock(...)]]
        self._chunks = list(chunks)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._chunks:
            raise StopAsyncIteration
        return _MockStreamingChunk(self._chunks.pop(0))


class _MockStreamingChunk:
    """Simulates a single streaming chunk with .content attribute."""

    def __init__(self, content: list):
        self.content = content
        self.metadata = None


class TestStreamingModeThinkingOnly:
    """Streaming-specific regression tests for the positive completion check."""

    @pytest.mark.asyncio
    async def test_streaming_thinking_only_then_text_does_not_exit_early(self):
        """In streaming mode, thinking-only should also not cause premature exit."""
        model = _MockStreamingModel(
            # First call: one chunk with thinking-only content
            [[ThinkingBlock(type="thinking", thinking="Let me think...")]],
            # Second call: one chunk with text content
            [[TextBlock(type="text", text="Streaming response complete.")]],
        )

        agent = MetadataAwareReActAgent(
            name="test_agent",
            sys_prompt="You are a helpful assistant.",
            model=model,
            formatter=_MockFormatter(),
            max_iters=5,
        )

        reply = await agent.reply(Msg(name="user", content="Hello", role="user"))

        text_blocks = reply.get_content_blocks("text")
        assert len(text_blocks) >= 1
        assert model._cursor >= 2, (
            f"Streaming model called only {model._cursor} time(s); "
            "thinking-only response caused premature exit in streaming mode"
        )


class _PersistentThinkingModel:
    """Model that always returns thinking-only, simulating a stuck model."""

    stream: bool = False

    def __init__(self):
        self.call_count = 0

    async def __call__(self, prompt, tools=None, tool_choice=None, **kwargs):
        self.call_count += 1
        return _MockResponse(
            [ThinkingBlock(type="thinking", thinking=f"Still reasoning... attempt {self.call_count}")]
        )


class TestProgressiveEscalation:
    """Tests for progressive hint escalation during persistent thinking-only loops."""

    @pytest.mark.asyncio
    async def test_stuck_thinking_agent_does_not_loop_forever(self):
        """Agent with persistent thinking-only should exit via _summarizing()
        rather than looping all max_iters or falsely claiming completion."""
        model = _PersistentThinkingModel()

        agent = MetadataAwareReActAgent(
            name="test_agent",
            sys_prompt="You are a helpful assistant.",
            model=model,
            formatter=_MockFormatter(),
            max_iters=15,
        )

        reply = await agent.reply(Msg(name="user", content="Do something", role="user"))

        # Agent should exit (via _summarizing or stuck detection), not loop 500 times.
        # The reply should exist and not be thinking-only.
        assert reply is not None
        assert model.call_count < 15, (
            f"Agent looped all {model.call_count} iterations without exiting; "
            "escalation/stuck detection should have stopped it earlier"
        )

        # The reply should have content — either text or a failure summary.
        assert len(reply.get_content_blocks()) > 0

    @pytest.mark.asyncio
    async def test_escalation_reduces_thinking_burn(self):
        """Escalation should prevent the agent from burning through all max_iters
        on thinking-only. A small max_iters (e.g. 12) with escalation should
        complete in far fewer model calls than max_iters."""
        model = _PersistentThinkingModel()

        agent = MetadataAwareReActAgent(
            name="test_agent",
            sys_prompt="You are a helpful assistant.",
            model=model,
            formatter=_MockFormatter(),
            max_iters=12,
        )

        await agent.reply(Msg(name="user", content="Do something", role="user"))

        # With escalation, stuck detection should trigger well before max_iters.
        # Without escalation the agent would burn all 12 iterations.
        assert model.call_count <= 12
        assert model.call_count < 12, (
            f"Even with escalation, agent used {model.call_count} calls "
            "(should exit early on stuck detection)"
        )

    @pytest.mark.asyncio
    async def test_thinking_only_then_recovery_works(self):
        """A few thinking-only responses followed by a real response should
        complete normally (escalation should not trigger stuck exit)."""
        model = _MockModel(
            # 3 thinking-only (within gentle phase)
            [ThinkingBlock(type="thinking", thinking="Hmm...")],
            [ThinkingBlock(type="thinking", thinking="Let me think more...")],
            [ThinkingBlock(type="thinking", thinking="Almost there...")],
            # Then tool_use + text together
            [
                {
                    "type": "tool_use",
                    "id": "call_1",
                    "name": "execute_command",
                    "input": {"command": "echo done"},
                },
                TextBlock(type="text", text="Executing..."),
            ],
            # Final text (completion)
            [TextBlock(type="text", text="All done.")],
        )

        from agentscope.tool import Toolkit

        toolkit = Toolkit()

        async def _fake_execute_command(command: str) -> str:
            return f"executed: {command}"

        toolkit.register_tool_function(_fake_execute_command)

        agent = MetadataAwareReActAgent(
            name="test_agent",
            sys_prompt="You are a helpful assistant.",
            model=model,
            formatter=_MockFormatter(),
            toolkit=toolkit,
            max_iters=15,
        )

        reply = await agent.reply(Msg(name="user", content="Do something", role="user"))
        text_blocks = reply.get_content_blocks("text")
        assert len(text_blocks) >= 1
        assert text_blocks[-1]["text"] == "All done."


class _CapturingMemory:
    def __init__(self):
        self.messages = []

    async def add(self, msg, marks=None):
        if isinstance(msg, list):
            self.messages.extend(msg)
        elif msg is not None:
            self.messages.append(msg)

    async def get_memory(self, *args, **kwargs):
        return list(self.messages)

    async def delete_by_mark(self, *args, **kwargs):
        return None


def test_build_tool_result_msg_uses_user_role_for_deepseek_salience():
    msg = MetadataAwareReActAgent._build_tool_result_msg(
        tool_call_id="call-1",
        tool_name="read_file",
        output="content",
    )

    assert msg.role == "user"
    assert msg.name == "tool"
    block = msg.get_content_blocks("tool_result")[0]
    assert block["id"] == "call-1"
    assert block["name"] == "read_file"


@pytest.mark.asyncio
async def test_acting_records_tool_result_as_user_role():
    from agentscope.tool import Toolkit, ToolResponse

    toolkit = Toolkit()

    async def read_file(path: str) -> ToolResponse:
        return ToolResponse(content=[TextBlock(type="text", text=f"read: {path}")])

    toolkit.register_tool_function(read_file)
    agent = MetadataAwareReActAgent(
        name="test_agent",
        sys_prompt="You are helpful.",
        model=_MockModel([TextBlock(type="text", text="done")]),
        formatter=_MockFormatter(),
        toolkit=toolkit,
        max_iters=3,
    )
    capture = _CapturingMemory()
    agent.memory = capture

    await agent._acting({
        "type": "tool_use",
        "id": "call-1",
        "name": "read_file",
        "input": {"path": "demo.txt"},
    })

    tool_result_msgs = [m for m in capture.messages if m.get_content_blocks("tool_result")]
    assert tool_result_msgs
    assert tool_result_msgs[-1].role == "user"
    assert tool_result_msgs[-1].name == "tool"


def test_thinking_only_escalation_thresholds_are_deepseek_friendly(monkeypatch):
    monkeypatch.delenv("AGENTSCOPE_THINKING_ONLY_REQUIRED_TOOL_THRESHOLD", raising=False)
    monkeypatch.delenv("AGENTSCOPE_THINKING_ONLY_EXIT_THRESHOLD", raising=False)

    assert MetadataAwareReActAgent._thinking_only_required_tool_threshold() >= 8
    assert MetadataAwareReActAgent._thinking_only_exit_threshold() >= 16
