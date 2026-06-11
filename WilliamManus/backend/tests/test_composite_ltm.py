import json
import sys
import types
from pathlib import Path

import pytest

from agentscope.tool import ToolResponse

if "structlog" not in sys.modules:
    class _DummyBoundLogger:
        def info(self, *args, **kwargs):
            return None

        def warning(self, *args, **kwargs):
            return None

        def error(self, *args, **kwargs):
            return None

        def debug(self, *args, **kwargs):
            return None

    class _DummyProcessorFormatter:
        @staticmethod
        def wrap_for_formatter(*args, **kwargs):
            return None

    dummy_structlog = types.SimpleNamespace(
        configure=lambda **kwargs: None,
        get_logger=lambda *args, **kwargs: _DummyBoundLogger(),
        stdlib=types.SimpleNamespace(
            add_log_level=lambda *args, **kwargs: None,
            PositionalArgumentsFormatter=lambda *args, **kwargs: None,
            ProcessorFormatter=_DummyProcessorFormatter,
            LoggerFactory=lambda *args, **kwargs: None,
            BoundLogger=_DummyBoundLogger,
        ),
        processors=types.SimpleNamespace(TimeStamper=lambda *args, **kwargs: None),
        contextvars=types.SimpleNamespace(
            clear_contextvars=lambda: None,
            bind_contextvars=lambda **kwargs: None,
            get_contextvars=lambda: {},
        ),
    )
    sys.modules["structlog"] = dummy_structlog

if "services.postgresql" not in sys.modules:
    services_pkg = types.ModuleType("services")
    postgresql_mod = types.ModuleType("services.postgresql")

    class _DummyDBConnection:
        @property
        async def client(self):
            raise RuntimeError("DB client is not available in this unit test")

    postgresql_mod.DBConnection = _DummyDBConnection
    services_pkg.postgresql = postgresql_mod
    sys.modules["services"] = services_pkg
    sys.modules["services.postgresql"] = postgresql_mod

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentscope_integration.memory.long_term.composite_ltm import CompositeLongTermMemory


def _tool_response(text: str) -> ToolResponse:
    return ToolResponse(content=[{"type": "text", "text": text}], metadata={})


class _FakeMemory:
    def __init__(self, name: str, *, fail_on: str = "") -> None:
        self.name = name
        self.fail_on = fail_on
        self.entered = False
        self.exited = False
        self.record_calls = 0
        self.record_to_memory_calls = 0
        self.retrieve_from_memory_calls = []

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, *_args):
        self.exited = True

    async def record(self, _msgs, **_kwargs):
        self.record_calls += 1
        if self.fail_on == "record":
            raise RuntimeError(f"{self.name} record failed")

    async def retrieve(self, _msg, limit=5, **_kwargs):
        if self.fail_on == "retrieve":
            raise RuntimeError(f"{self.name} retrieve failed")
        return f"{self.name} retrieved limit={limit}"

    async def record_to_memory(self, thinking: str, content: list[str], **_kwargs):
        self.record_to_memory_calls += 1
        if self.fail_on == "record_to_memory":
            raise RuntimeError(f"{self.name} record_to_memory failed")
        return _tool_response(f"{self.name} saved {len(content)} items ({thinking[:20]})")

    async def retrieve_from_memory(self, keywords: list[str], limit=5, **_kwargs):
        if self.fail_on == "retrieve_from_memory":
            raise RuntimeError(f"{self.name} retrieve_from_memory failed")
        self.retrieve_from_memory_calls.append((list(keywords), limit))
        return _tool_response(f"{self.name} retrieved {keywords} limit={limit}")


@pytest.mark.asyncio
async def test_composite_context_manager_enters_and_exits() -> None:
    task = _FakeMemory("task")
    tool = _FakeMemory("tool")
    composite = CompositeLongTermMemory(task_memory=task, tool_memory=tool)

    async with composite:
        assert task.entered is True
        assert tool.entered is True
    assert task.exited is True
    assert tool.exited is True


@pytest.mark.asyncio
async def test_composite_retrieve_merges_sections_and_fail_open() -> None:
    task = _FakeMemory("task")
    tool = _FakeMemory("tool", fail_on="retrieve")
    composite = CompositeLongTermMemory(
        task_memory=task,
        tool_memory=tool,
        fail_open=True,
    )

    result = await composite.retrieve(msg=None, limit=3)
    assert "<task_experience>" in result
    assert "</task_experience>" in result
    assert "<tool_experience>" not in result


@pytest.mark.asyncio
async def test_composite_record_to_memory_applies_bucketing() -> None:
    task = _FakeMemory("task")
    tool = _FakeMemory("tool")
    composite = CompositeLongTermMemory(
        task_memory=task,
        tool_memory=tool,
        write_gate_enabled=True,
    )

    tool_payload = json.dumps(
        {
            "create_time": "2026-02-28 12:00:00",
            "tool_name": "execute_command",
            "input": {"cmd": "ls"},
            "output": "ok output from command execution",
            "token_cost": 8,
            "success": True,
            "time_cost": 0.4,
        }
    )
    response = await composite.record_to_memory(
        thinking="Save useful patterns",
        content=[
            "Use smaller patches to reduce merge conflicts when editing many files.",
            tool_payload,
            "token=abc123456",
        ],
    )
    text = response.content[0]["text"]
    assert "[task]" in text
    assert "[tool]" in text
    assert response.metadata["recorded_task"] == 1
    assert response.metadata["recorded_tool"] == 1
    assert response.metadata["dropped_count"] >= 1


@pytest.mark.asyncio
async def test_composite_static_record_disabled_by_default() -> None:
    task = _FakeMemory("task")
    tool = _FakeMemory("tool")
    composite = CompositeLongTermMemory(task_memory=task, tool_memory=tool)

    await composite.record(msgs=[])
    assert task.record_calls == 0
    assert tool.record_calls == 0


@pytest.mark.asyncio
async def test_composite_retrieve_from_memory_uses_merged_task_query_by_default() -> None:
    task = _FakeMemory("task")
    tool = _FakeMemory("tool")
    composite = CompositeLongTermMemory(task_memory=task, tool_memory=tool)

    response = await composite.retrieve_from_memory(
        keywords=["manim", "latex", "animation", "latex"],
        limit=4,
    )

    assert "<task_experience>" in response.content[0]["text"]
    assert task.retrieve_from_memory_calls == [(["manim, latex, animation"], 4)]
    assert tool.retrieve_from_memory_calls == [
        (["manim", "latex", "animation", "latex"], 4),
    ]


@pytest.mark.asyncio
async def test_composite_retrieve_from_memory_per_keyword_mode_honors_limit() -> None:
    task = _FakeMemory("task")
    composite = CompositeLongTermMemory(
        task_memory=task,
        task_query_mode="per_keyword",
        task_query_keyword_limit=2,
    )

    await composite.retrieve_from_memory(
        keywords=["manim", "latex", "animation"],
        limit=3,
    )

    assert task.retrieve_from_memory_calls == [(["manim", "latex"], 3)]


# --------------- delete_from_memory tests ---------------


class _FakePoint:
    def __init__(self, pid: str, content: str, meta_content: str = "") -> None:
        self.id = pid
        self.payload = {
            "content": content,
            "metadata": {"content": meta_content} if meta_content else {},
        }


class _FakeQdrantClient:
    """Lightweight async Qdrant client stub for testing delete_from_memory."""

    def __init__(self, url: str = "") -> None:
        self.url = url
        self.points: list[_FakePoint] = []
        self.deleted_ids: list[str] = []
        self.closed = False

    async def scroll(self, *, collection_name, limit, with_payload, with_vectors, **kwargs):
        return self.points, None

    async def delete(self, *, collection_name, points_selector):
        self.deleted_ids.extend(points_selector.points)

    async def close(self):
        self.closed = True


class _FakePointIdsList:
    def __init__(self, points):
        self.points = points


@pytest.mark.asyncio
async def test_delete_from_memory_success() -> None:
    """Matching content should be deleted; non-matching should remain."""
    fake_client = _FakeQdrantClient()
    fake_client.points = [
        _FakePoint("aaa", "good memory"),
        _FakePoint("bbb", "bad memory to delete"),
        _FakePoint("ccc", "another good one", meta_content="some meta"),
    ]

    import unittest.mock as mock

    composite = CompositeLongTermMemory(
        task_memory=_FakeMemory("task"),
        qdrant_collection="test_col",
        qdrant_url="http://localhost:6333",
    )

    with mock.patch(
        "agentscope_integration.memory.long_term.composite_ltm.CompositeLongTermMemory._find_and_delete_points",
        new_callable=lambda: lambda: None,
    ):
        pass

    async def _patched_find_and_delete(client_cls, content_to_delete):
        targets = {s.strip().lower() for s in content_to_delete if s}
        matched = []
        for p in fake_client.points:
            c = p.payload.get("content", "").strip().lower()
            mc = (p.payload.get("metadata", {}).get("content", "") or "").strip().lower()
            if c in targets or mc in targets:
                matched.append(str(p.id))
        fake_client.deleted_ids = matched
        return matched

    composite._find_and_delete_points = _patched_find_and_delete

    response = await composite.delete_from_memory(
        thinking="This memory is wrong",
        content_to_delete=["bad memory to delete"],
    )

    assert response.metadata["deleted"] == 1
    assert "bbb" in fake_client.deleted_ids
    assert "aaa" not in fake_client.deleted_ids
    assert "Successfully deleted 1" in response.content[0]["text"]


@pytest.mark.asyncio
async def test_delete_from_memory_meta_content_match() -> None:
    """Should match against metadata.content as well."""
    fake_client = _FakeQdrantClient()
    fake_client.points = [
        _FakePoint("aaa", "trigger condition", meta_content="Avoid html2pptx use pptxgenjs"),
    ]

    composite = CompositeLongTermMemory(
        task_memory=_FakeMemory("task"),
        qdrant_collection="test_col",
        qdrant_url="http://localhost:6333",
    )

    async def _patched(client_cls, content_to_delete):
        targets = {s.strip().lower() for s in content_to_delete if s}
        matched = []
        for p in fake_client.points:
            c = p.payload.get("content", "").strip().lower()
            mc = (p.payload.get("metadata", {}).get("content", "") or "").strip().lower()
            if c in targets or mc in targets:
                matched.append(str(p.id))
        return matched

    composite._find_and_delete_points = _patched

    response = await composite.delete_from_memory(
        thinking="Wrong tool preference",
        content_to_delete=["Avoid html2pptx use pptxgenjs"],
    )

    assert response.metadata["deleted"] == 1


@pytest.mark.asyncio
async def test_delete_from_memory_no_qdrant_url() -> None:
    """Should fail gracefully when Qdrant is not configured."""
    composite = CompositeLongTermMemory(
        task_memory=_FakeMemory("task"),
        fail_open=True,
    )

    response = await composite.delete_from_memory(
        thinking="test",
        content_to_delete=["something"],
    )

    assert response.metadata["deleted"] == 0
    assert "not configured" in response.content[0]["text"].lower() or "not available" in response.content[0]["text"].lower()


@pytest.mark.asyncio
async def test_delete_from_memory_empty_content() -> None:
    """Should return early when content_to_delete is empty."""
    composite = CompositeLongTermMemory(
        task_memory=_FakeMemory("task"),
        qdrant_collection="test_col",
        qdrant_url="http://localhost:6333",
    )

    response = await composite.delete_from_memory(
        thinking="nothing to delete",
        content_to_delete=[],
    )

    assert response.metadata["deleted"] == 0
    assert "no content" in response.content[0]["text"].lower()


@pytest.mark.asyncio
async def test_delete_from_memory_no_match() -> None:
    """Should report no matches when content doesn't exist."""
    composite = CompositeLongTermMemory(
        task_memory=_FakeMemory("task"),
        qdrant_collection="test_col",
        qdrant_url="http://localhost:6333",
    )

    async def _patched(client_cls, content_to_delete):
        return []

    composite._find_and_delete_points = _patched

    response = await composite.delete_from_memory(
        thinking="trying to delete",
        content_to_delete=["nonexistent memory"],
    )

    assert response.metadata["deleted"] == 0
    assert "no matching" in response.content[0]["text"].lower()


@pytest.mark.asyncio
async def test_delete_from_memory_fail_open_on_error() -> None:
    """Should catch errors gracefully when fail_open=True."""
    composite = CompositeLongTermMemory(
        task_memory=_FakeMemory("task"),
        qdrant_collection="test_col",
        qdrant_url="http://localhost:6333",
        fail_open=True,
    )

    async def _patched(client_cls, content_to_delete):
        raise ConnectionError("Qdrant unreachable")

    composite._find_and_delete_points = _patched

    response = await composite.delete_from_memory(
        thinking="delete something",
        content_to_delete=["some memory"],
    )

    assert response.metadata["deleted"] == 0
    assert "failed" in response.content[0]["text"].lower()


@pytest.mark.asyncio
async def test_delete_from_memory_fail_closed_on_error() -> None:
    """Should raise when fail_open=False and Qdrant errors."""
    composite = CompositeLongTermMemory(
        task_memory=_FakeMemory("task"),
        qdrant_collection="test_col",
        qdrant_url="http://localhost:6333",
        fail_open=False,
    )

    async def _patched(client_cls, content_to_delete):
        raise ConnectionError("Qdrant unreachable")

    composite._find_and_delete_points = _patched

    with pytest.raises(ConnectionError, match="Qdrant unreachable"):
        await composite.delete_from_memory(
            thinking="delete something",
            content_to_delete=["some memory"],
        )
