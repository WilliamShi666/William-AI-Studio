import pytest

from agentscope.formatter import OpenAIChatFormatter
from agentscope.model import ChatModelBase
from agentscope.tool import Toolkit

from agentscope_integration.agents.worker import WorkerAgent
from agentscope_integration.memory.long_term import ReadOnlyDelegateLongTermMemory


class _DummyModel(ChatModelBase):
    async def __call__(self, *_args, **_kwargs):
        return {"ok": True}


class _FakeDelegateLTM:
    async def retrieve(self, _msg, limit=5, **_kwargs):
        return f"retrieve:{limit}"

    async def retrieve_from_memory(self, keywords, limit=5, **_kwargs):
        self.last_keywords = list(keywords)
        self.last_limit = limit
        return {"keywords": keywords, "limit": limit}


def _dummy_tool(path: str) -> str:
    """Echo back the provided path."""
    return path


@pytest.mark.asyncio
async def test_worker_agent_registers_readonly_ltm_without_mutating_source_toolkit():
    source_toolkit = Toolkit()
    source_toolkit.register_tool_function(_dummy_tool)
    readonly_ltm = ReadOnlyDelegateLongTermMemory(_FakeDelegateLTM())

    worker = WorkerAgent(
        model=_DummyModel("dummy", stream=False),
        formatter=OpenAIChatFormatter(),
        toolkit=source_toolkit,
        long_term_memory=readonly_ltm,
        long_term_memory_mode="static_control",
    )

    assert "retrieve_from_memory" in worker.toolkit.tools
    assert "record_to_memory" not in worker.toolkit.tools
    assert "retrieve_from_memory" not in source_toolkit.tools
    assert worker.get_agent().long_term_memory is readonly_ltm
    assert worker.get_agent()._static_control is True
    assert worker.get_agent()._agent_control is False
