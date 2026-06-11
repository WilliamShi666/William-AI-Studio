import pytest

from agentscope.message import Msg
from agentscope.message import TextBlock
from agentscope.tool import ToolResponse

from agentscope_integration.memory.long_term import ReadOnlyDelegateLongTermMemory


class _FakeDelegateLTM:
    def __init__(self):
        self.entered = False
        self.exited = False
        self.retrieve_calls = []
        self.retrieve_from_memory_calls = []
        self.record_calls = 0
        self.record_to_memory_calls = 0

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, *_args):
        self.exited = True

    async def record(self, *_args, **_kwargs):
        self.record_calls += 1

    async def retrieve(self, msg, limit=5, **kwargs):
        self.retrieve_calls.append((msg, limit, kwargs))
        return "retrieved context"

    async def record_to_memory(self, **_kwargs):
        self.record_to_memory_calls += 1
        return ToolResponse(content=[TextBlock(type="text", text="delegate write")])

    async def retrieve_from_memory(self, keywords, limit=5, **kwargs):
        self.retrieve_from_memory_calls.append((list(keywords), limit, kwargs))
        return ToolResponse(
            content=[TextBlock(type="text", text="delegate retrieve")],
        )


@pytest.mark.asyncio
async def test_readonly_delegate_ltm_manages_lifecycle_when_enabled():
    delegate = _FakeDelegateLTM()
    readonly = ReadOnlyDelegateLongTermMemory(
        delegate,
        manage_lifecycle=True,
    )

    await readonly.__aenter__()
    await readonly.__aexit__(None, None, None)

    assert delegate.entered is True
    assert delegate.exited is True


@pytest.mark.asyncio
async def test_readonly_delegate_ltm_delegates_retrieval_but_not_recording():
    delegate = _FakeDelegateLTM()
    readonly = ReadOnlyDelegateLongTermMemory(delegate)

    result = await readonly.retrieve(Msg("user", "Find prior experience", "user"), limit=3)
    tool_result = await readonly.retrieve_from_memory(["ltm", "worker"], limit=2)
    disabled = await readonly.record_to_memory(
        thinking="save this",
        content=["should not write"],
    )
    await readonly.record([Msg("user", "hello", "user")])

    assert result == "retrieved context"
    assert delegate.retrieve_calls[0][1] == 3
    assert delegate.retrieve_from_memory_calls == [(["ltm", "worker"], 2, {})]
    assert delegate.record_calls == 0
    assert delegate.record_to_memory_calls == 0
    assert disabled.metadata["write_disabled"] is True
    first_block = tool_result.content[0]
    if isinstance(first_block, dict):
        assert first_block["text"] == "delegate retrieve"
    else:
        assert first_block.text == "delegate retrieve"
