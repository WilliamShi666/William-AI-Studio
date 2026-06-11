import sys
import types
from unittest.mock import AsyncMock

import pytest

if "services" not in sys.modules:
    sys.modules["services"] = types.ModuleType("services")

if "services.regular_load_harness_stub" not in sys.modules:
    regular_load_harness_stub_mod = types.ModuleType("services.regular_load_harness_stub")
    sys.modules["services.regular_load_harness_stub"] = regular_load_harness_stub_mod
    setattr(sys.modules["services"], "regular_load_harness_stub", regular_load_harness_stub_mod)

if "services.redis" not in sys.modules:
    services_redis_stub = types.SimpleNamespace(
        REDIS_KEY_TTL=3600 * 24,
        eval_script=lambda *args, **kwargs: None,
        get=lambda *args, **kwargs: None,
        lrange=lambda *args, **kwargs: None,
        publish=lambda *args, **kwargs: None,
        rpush=lambda *args, **kwargs: None,
        sadd=lambda *args, **kwargs: None,
        set=lambda *args, **kwargs: None,
        smembers=lambda *args, **kwargs: set(),
        srem=lambda *args, **kwargs: None,
        expire=lambda *args, **kwargs: None,
    )
    sys.modules["services.redis"] = services_redis_stub
    setattr(sys.modules["services"], "redis", services_redis_stub)

if "services.postgresql" not in sys.modules:
    postgresql_mod = types.ModuleType("services.postgresql")

    class _DummyDBConnection:
        @property
        async def client(self):
            return object()

    postgresql_mod.DBConnection = _DummyDBConnection
    sys.modules["services.postgresql"] = postgresql_mod
    setattr(sys.modules["services"], "postgresql", postgresql_mod)

from agentscope.formatter import OpenAIChatFormatter
from agentscope.message import TextBlock
from agentscope.model import ChatModelBase
from agentscope.tool import ToolResponse
from agentscope_integration.shadow_clone import subagent_factory


class _DummyModel(ChatModelBase):
    async def __call__(self, *_args, **_kwargs):
        return {"ok": True}


class _DummyAgent:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.toolkit = kwargs.get("toolkit")
        self.memory = kwargs.get("memory")
        self.hooks = []

    async def __call__(self, msg):
        return msg

    def register_instance_hook(self, **kwargs):
        self.hooks.append(kwargs)


class _FakeLTM:
    def __init__(self):
        self.entered = False
        self.exited = False
        self.retrieve_calls = []
        self.retrieve_from_memory_calls = []

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, *_args):
        self.exited = True

    async def retrieve(self, msg, limit=5, **kwargs):
        self.retrieve_calls.append((msg, limit, kwargs))
        return "auto-retrieved"

    async def retrieve_from_memory(self, keywords, limit=5, **kwargs):
        self.retrieve_from_memory_calls.append((list(keywords), limit, kwargs))
        return ToolResponse(content=[TextBlock(type="text", text="ltm lookup")])


def _tool_response_text(response) -> str:
    block = response.content[0]
    if isinstance(block, dict):
        return str(block.get("text") or "")
    return str(getattr(block, "text", ""))


@pytest.mark.asyncio
async def test_create_subagent_uses_messages_table_memory(monkeypatch):
    captured = {}

    def _fake_model_create(model_key=None, **kwargs):
        captured.setdefault("model_keys", []).append(model_key)
        return _DummyModel(str(model_key or "dummy-model"), stream=kwargs.get("stream", False)), OpenAIChatFormatter()

    class _FakeMemory:
        def __init__(self, **kwargs):
            captured["memory_kwargs"] = kwargs

    class _FakeToolkit:
        def __init__(self):
            self.functions = {}

        def register_tool_function(self, func):
            self.functions[func.__name__] = func

    class _FakeToolkitAdapter:
        def __init__(self, **kwargs):
            captured["toolkit_kwargs"] = kwargs

        def get_toolkit(self):
            return _FakeToolkit()

    def _fake_react_agent(**kwargs):
        captured["agent_kwargs"] = kwargs
        captured["toolkit"] = kwargs.get("toolkit")
        return _DummyAgent(**kwargs)

    monkeypatch.setattr(subagent_factory.ModelFactory, "create", _fake_model_create)
    monkeypatch.setattr(subagent_factory, "MessagesTableMemory", _FakeMemory)
    monkeypatch.setattr(subagent_factory, "ToolkitAdapter", _FakeToolkitAdapter)
    monkeypatch.setattr(subagent_factory, "ReActAgent", _fake_react_agent)
    monkeypatch.setattr(
        subagent_factory,
        "get_state",
        AsyncMock(
            return_value={
                "subagents": {
                    "task-1": {"role": "researcher"},
                    "task-2": {"role": "analyst"},
                },
            },
        ),
    )

    agent = await subagent_factory.create_subagent(
        subtask_config={
            "id": "task-1",
            "role": "researcher",
            "task_description": "Find facts",
            "model": "kimi-k2.5",
        },
        project_id="proj-1",
        parent_run_id="run-1",
        subtask_id="task-1",
        db_client=object(),
        thread_id="thread-1",
    )
    assert isinstance(agent, _DummyAgent)
    assert captured["model_keys"][0] == "kimi-k2.5"
    assert captured["model_keys"][1] == "glm-4.7"
    assert captured["memory_kwargs"]["thread_run_id"] == "shadow:run-1:task-1"
    assert captured["memory_kwargs"]["thread_id"] == "thread-1"
    assert captured["memory_kwargs"]["enable_tool_history_summary"] is False
    assert captured["toolkit_kwargs"]["project_id"] == "proj-1"
    assert captured["toolkit_kwargs"]["thread_manager"] is not None
    assert captured["toolkit_kwargs"]["shadow_clone_run_id"] == "run-1"
    assert captured["toolkit_kwargs"]["strict_sandbox"] is True
    assert captured["toolkit_kwargs"]["shadow_clone_fail_fast"] is True
    assert captured["agent_kwargs"]["compression_config"] is not None
    assert "send_peer_note" in captured["toolkit"].functions
    assert {hook["hook_type"] for hook in agent.hooks} == {"pre_reply", "pre_reasoning"}
    assert agent.kwargs["memory"] is not None


@pytest.mark.asyncio
async def test_create_subagent_registers_readonly_ltm_retrieval_when_enabled(monkeypatch):
    captured = {}
    fake_ltm = _FakeLTM()

    def _fake_model_create(model_key=None, **kwargs):
        return _DummyModel(
            str(model_key or "dummy-model"),
            stream=kwargs.get("stream", False),
        ), OpenAIChatFormatter()

    class _FakeMemory:
        def __init__(self, **kwargs):
            captured["memory_kwargs"] = kwargs

    class _FakeToolkit:
        def __init__(self):
            self.functions = {}

        def register_tool_function(self, func):
            self.functions[func.__name__] = func

    class _FakeToolkitAdapter:
        def __init__(self, **kwargs):
            pass

        def get_toolkit(self):
            return _FakeToolkit()

    def _fake_react_agent(**kwargs):
        captured["agent_kwargs"] = kwargs
        captured["toolkit"] = kwargs.get("toolkit")
        return _DummyAgent(**kwargs)

    monkeypatch.setattr(subagent_factory.ModelFactory, "create", _fake_model_create)
    monkeypatch.setattr(subagent_factory, "MessagesTableMemory", _FakeMemory)
    monkeypatch.setattr(subagent_factory, "ToolkitAdapter", _FakeToolkitAdapter)
    monkeypatch.setattr(subagent_factory, "ReActAgent", _fake_react_agent)
    monkeypatch.setattr(
        subagent_factory,
        "load_ltm_settings_from_env",
        lambda: types.SimpleNamespace(
            enabled=True,
            attach_scope="orchestrator",
            fail_open=True,
        ),
    )
    monkeypatch.setattr(
        subagent_factory,
        "create_long_term_memory",
        lambda **_kwargs: fake_ltm,
    )
    monkeypatch.setattr(
        subagent_factory,
        "get_state",
        AsyncMock(
            return_value={
                "subagents": {
                    "task-1": {"role": "researcher"},
                },
            },
        ),
    )

    agent = await subagent_factory.create_subagent(
        subtask_config={
            "id": "task-1",
            "role": "researcher",
            "task_description": "Find facts",
        },
        project_id="proj-1",
        parent_run_id="run-1",
        subtask_id="task-1",
        db_client=object(),
        thread_id="thread-1",
    )

    assert isinstance(agent, _DummyAgent)
    assert captured["agent_kwargs"]["long_term_memory"] is not None
    assert captured["agent_kwargs"]["long_term_memory_mode"] == "static_control"
    assert "retrieve_from_memory" in captured["toolkit"].functions
    assert "record_to_memory" not in captured["toolkit"].functions
    assert getattr(agent, "_shadow_readonly_long_term_memory") is not None
    assert fake_ltm.entered is True


@pytest.mark.asyncio
async def test_layer_0_subagent_does_not_have_read_full_result(monkeypatch):
    """Verify that layer 0 subagents do NOT have read_full_result tool."""
    captured = {}

    def _fake_model_create(model_key=None, **kwargs):
        return _DummyModel(str(model_key or "dummy-model"), stream=kwargs.get("stream", False)), OpenAIChatFormatter()

    class _FakeMemory:
        def __init__(self, **kwargs):
            pass

    class _FakeToolkit:
        def __init__(self):
            self.functions = {}

        def register_tool_function(self, func):
            self.functions[func.__name__] = func

    class _FakeToolkitAdapter:
        def __init__(self, **kwargs):
            pass

        def get_toolkit(self):
            return _FakeToolkit()

    def _fake_react_agent(**kwargs):
        captured["toolkit"] = kwargs.get("toolkit")
        captured["sys_prompt"] = kwargs.get("sys_prompt")
        return _DummyAgent(**kwargs)

    monkeypatch.setattr(subagent_factory.ModelFactory, "create", _fake_model_create)
    monkeypatch.setattr(subagent_factory, "MessagesTableMemory", _FakeMemory)
    monkeypatch.setattr(subagent_factory, "ToolkitAdapter", _FakeToolkitAdapter)
    monkeypatch.setattr(subagent_factory, "ReActAgent", _fake_react_agent)
    monkeypatch.setattr(
        subagent_factory,
        "get_state",
        AsyncMock(
            return_value={
                "subagents": {
                    "task-1": {"role": "researcher"},
                    "task-2": {"role": "analyst"},
                },
            },
        ),
    )

    agent = await subagent_factory.create_subagent(
        subtask_config={
            "id": "task-1",
            "role": "researcher",
            "task_description": "Find facts",
            "layer_index": 0,
        },
        project_id="proj-1",
        parent_run_id="run-1",
        subtask_id="task-1",
        db_client=object(),
        thread_id="thread-1",
    )

    toolkit = captured["toolkit"]
    assert "read_full_result" not in toolkit.functions
    assert "send_peer_note" in toolkit.functions
    assert "read_full_result" not in captured["sys_prompt"]
    assert "send_peer_note" in captured["sys_prompt"]


@pytest.mark.asyncio
async def test_layer_1_subagent_has_read_full_result(monkeypatch):
    """Verify that layer 1+ subagents DO have read_full_result tool."""
    captured = {}

    def _fake_model_create(model_key=None, **kwargs):
        return _DummyModel(str(model_key or "dummy-model"), stream=kwargs.get("stream", False)), OpenAIChatFormatter()

    class _FakeMemory:
        def __init__(self, **kwargs):
            pass

    class _FakeToolkit:
        def __init__(self):
            self.functions = {}

        def register_tool_function(self, func):
            self.functions[func.__name__] = func

    class _FakeToolkitAdapter:
        def __init__(self, **kwargs):
            pass

        def get_toolkit(self):
            return _FakeToolkit()

    def _fake_react_agent(**kwargs):
        captured["toolkit"] = kwargs.get("toolkit")
        captured["sys_prompt"] = kwargs.get("sys_prompt")
        return _DummyAgent(**kwargs)

    monkeypatch.setattr(subagent_factory.ModelFactory, "create", _fake_model_create)
    monkeypatch.setattr(subagent_factory, "MessagesTableMemory", _FakeMemory)
    monkeypatch.setattr(subagent_factory, "ToolkitAdapter", _FakeToolkitAdapter)
    monkeypatch.setattr(subagent_factory, "ReActAgent", _fake_react_agent)
    monkeypatch.setattr(
        subagent_factory,
        "get_state",
        AsyncMock(
            return_value={
                "subagents": {
                    "task-1": {"role": "researcher"},
                    "task-2": {"role": "analyst"},
                },
            },
        ),
    )

    agent = await subagent_factory.create_subagent(
        subtask_config={
            "id": "task-2",
            "role": "analyst",
            "task_description": "Analyze results from task-1",
            "layer_index": 1,
        },
        project_id="proj-1",
        parent_run_id="run-1",
        subtask_id="task-2",
        db_client=object(),
        thread_id="thread-1",
    )

    toolkit = captured["toolkit"]
    assert "read_full_result" in toolkit.functions
    assert "send_peer_note" in toolkit.functions
    assert "read_full_result" in captured["sys_prompt"]
    assert "send_peer_note" in captured["sys_prompt"]


def test_subagent_prompt_includes_read_full_result_hint_for_layer_1():
    """Verify that layer 1+ subagent prompts mention read_full_result tool."""
    prompt = subagent_factory._build_subagent_prompt({
        "role": "analyst",
        "task_description": "Analyze data",
        "layer_index": 1,
    })

    assert "read_full_result" in prompt
    assert "earlier-layer" in prompt.lower() or "prior" in prompt.lower()
    assert "incidental hints" in prompt.lower()
    assert "send_peer_note" in prompt


def test_subagent_prompt_no_read_full_result_hint_for_layer_0():
    """Verify that layer 0 subagent prompts do NOT mention read_full_result."""
    prompt = subagent_factory._build_subagent_prompt({
        "role": "researcher",
        "task_description": "Find data",
        "layer_index": 0,
    })

    assert "read_full_result" not in prompt
    assert "shared workspace awareness" in prompt.lower()


@pytest.mark.asyncio
async def test_create_subagent_uses_full_name_resolution_for_openrouter_models(monkeypatch):
    captured = {"create": [], "create_from_full_name": []}

    def _fake_model_create(model_key=None, **kwargs):
        captured["create"].append(model_key)
        return _DummyModel(
            str(model_key or "dummy-model"),
            stream=kwargs.get("stream", False),
        ), OpenAIChatFormatter()

    def _fake_model_create_from_full_name(model_name=None, **kwargs):
        captured["create_from_full_name"].append(model_name)
        return _DummyModel(
            str(model_name or "dummy-model"),
            stream=kwargs.get("stream", False),
        ), OpenAIChatFormatter()

    class _FakeMemory:
        def __init__(self, **_kwargs):
            pass

    class _FakeToolkit:
        def __init__(self):
            self.functions = {}

        def register_tool_function(self, func):
            self.functions[func.__name__] = func

    class _FakeToolkitAdapter:
        def __init__(self, **_kwargs):
            pass

        def get_toolkit(self):
            return _FakeToolkit()

    monkeypatch.setattr(subagent_factory.ModelFactory, "create", _fake_model_create)
    monkeypatch.setattr(
        subagent_factory.ModelFactory,
        "create_from_full_name",
        _fake_model_create_from_full_name,
    )
    monkeypatch.setattr(subagent_factory, "MessagesTableMemory", _FakeMemory)
    monkeypatch.setattr(subagent_factory, "ToolkitAdapter", _FakeToolkitAdapter)
    monkeypatch.setattr(subagent_factory, "ReActAgent", lambda **kwargs: _DummyAgent(**kwargs))
    monkeypatch.setattr(
        subagent_factory,
        "get_state",
        AsyncMock(return_value={"subagents": {}}),
    )

    await subagent_factory.create_subagent(
        subtask_config={
            "id": "task-openrouter",
            "role": "researcher",
            "task_description": "Find facts",
            "model": "openrouter/minimax/minimax-m2.5",
        },
        project_id="proj-1",
        parent_run_id="run-1",
        subtask_id="task-openrouter",
        db_client=object(),
        thread_id="thread-1",
    )

    assert captured["create_from_full_name"][0] == "openrouter/minimax/minimax-m2.5"
    assert "openrouter/minimax/minimax-m2.5" not in captured["create"]


@pytest.mark.asyncio
async def test_create_subagent_passes_langfuse_trace_to_full_name_resolution(monkeypatch):
    captured = {}

    class _FakeTrace:
        pass

    class _FakeLangfuseClient:
        def trace(self, id=None):
            captured["langfuse_trace_id"] = id
            return _FakeTrace()

    services_langfuse_mod = types.ModuleType("services.langfuse")
    services_langfuse_mod.langfuse = _FakeLangfuseClient()
    sys.modules["services.langfuse"] = services_langfuse_mod
    setattr(sys.modules["services"], "langfuse", services_langfuse_mod)

    def _fake_model_create(model_key=None, **kwargs):
        captured.setdefault("create_calls", []).append((model_key, kwargs))
        return _DummyModel(
            str(model_key or "dummy-model"),
            stream=kwargs.get("stream", False),
        ), OpenAIChatFormatter()

    def _fake_model_create_from_full_name(model_name=None, **kwargs):
        captured["create_from_full_name_model"] = model_name
        captured["create_from_full_name_trace"] = kwargs.get("trace")
        return _DummyModel(
            str(model_name or "dummy-model"),
            stream=kwargs.get("stream", False),
        ), OpenAIChatFormatter()

    class _FakeMemory:
        def __init__(self, **_kwargs):
            pass

    class _FakeToolkit:
        def __init__(self):
            self.functions = {}

        def register_tool_function(self, func):
            self.functions[func.__name__] = func

    class _FakeToolkitAdapter:
        def __init__(self, **kwargs):
            captured["toolkit_trace"] = kwargs.get("trace")

        def get_toolkit(self):
            return _FakeToolkit()

    monkeypatch.setattr(subagent_factory.ModelFactory, "create", _fake_model_create)
    monkeypatch.setattr(
        subagent_factory.ModelFactory,
        "create_from_full_name",
        _fake_model_create_from_full_name,
    )
    monkeypatch.setattr(subagent_factory, "MessagesTableMemory", _FakeMemory)
    monkeypatch.setattr(subagent_factory, "ToolkitAdapter", _FakeToolkitAdapter)
    monkeypatch.setattr(subagent_factory, "ReActAgent", lambda **kwargs: _DummyAgent(**kwargs))
    monkeypatch.setattr(
        subagent_factory,
        "get_state",
        AsyncMock(return_value={"subagents": {}}),
    )

    await subagent_factory.create_subagent(
        subtask_config={
            "id": "task-openrouter-trace",
            "role": "researcher",
            "task_description": "Find facts",
            "model": "openrouter/minimax/minimax-m2.7",
        },
        project_id="proj-1",
        parent_run_id="run-1",
        subtask_id="task-openrouter-trace",
        db_client=object(),
        thread_id="thread-1",
        langfuse_trace_id="lf-trace-1",
    )

    assert captured["langfuse_trace_id"] == "lf-trace-1"
    assert captured["create_from_full_name_model"] == "openrouter/minimax/minimax-m2.7"
    assert isinstance(captured["create_from_full_name_trace"], _FakeTrace)
    assert isinstance(captured["toolkit_trace"], _FakeTrace)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model_name", "expected_factory_name"),
    [
        ("glm-4.7", "create"),
        ("openrouter/minimax/minimax-m2.7", "create_from_full_name"),
    ],
)
async def test_create_subagent_prefers_parent_observation_over_trace_id(
    monkeypatch,
    model_name,
    expected_factory_name,
):
    captured = {
        "trace_ids": [],
        "create_model_traces": [],
        "create_from_full_name_traces": [],
    }
    provided_parent_observation = object()

    class _FakeLangfuseClient:
        def trace(self, id=None):
            captured["trace_ids"].append(id)
            return object()

    services_langfuse_mod = types.ModuleType("services.langfuse")
    services_langfuse_mod.langfuse = _FakeLangfuseClient()
    sys.modules["services.langfuse"] = services_langfuse_mod
    setattr(sys.modules["services"], "langfuse", services_langfuse_mod)

    def _fake_model_create(model_key=None, **kwargs):
        captured.setdefault("create_model_keys", []).append(model_key)
        captured["create_model_traces"].append(kwargs.get("trace"))
        return _DummyModel(
            str(model_key or "dummy-model"),
            stream=kwargs.get("stream", False),
        ), OpenAIChatFormatter()

    def _fake_model_create_from_full_name(model_name=None, **kwargs):
        captured.setdefault("create_from_full_name_models", []).append(model_name)
        captured["create_from_full_name_traces"].append(kwargs.get("trace"))
        return _DummyModel(
            str(model_name or "dummy-model"),
            stream=kwargs.get("stream", False),
        ), OpenAIChatFormatter()

    class _FakeMemory:
        def __init__(self, **_kwargs):
            pass

    class _FakeToolkit:
        def __init__(self):
            self.functions = {}

        def register_tool_function(self, func):
            self.functions[func.__name__] = func

    class _FakeToolkitAdapter:
        def __init__(self, **kwargs):
            captured["toolkit_trace"] = kwargs.get("trace")

        def get_toolkit(self):
            return _FakeToolkit()

    monkeypatch.setattr(subagent_factory.ModelFactory, "create", _fake_model_create)
    monkeypatch.setattr(
        subagent_factory.ModelFactory,
        "create_from_full_name",
        _fake_model_create_from_full_name,
    )
    monkeypatch.setattr(subagent_factory, "MessagesTableMemory", _FakeMemory)
    monkeypatch.setattr(subagent_factory, "ToolkitAdapter", _FakeToolkitAdapter)
    monkeypatch.setattr(subagent_factory, "ReActAgent", lambda **kwargs: _DummyAgent(**kwargs))
    monkeypatch.setattr(
        subagent_factory,
        "get_state",
        AsyncMock(return_value={"subagents": {}}),
    )

    await subagent_factory.create_subagent(
        subtask_config={
            "id": "task-parent-observation",
            "role": "researcher",
            "task_description": "Find facts",
            "model": model_name,
        },
        project_id="proj-1",
        parent_run_id="run-1",
        subtask_id="task-parent-observation",
        db_client=object(),
        thread_id="thread-1",
        langfuse_trace_id="lf-root-trace-id",
        langfuse_parent_observation=provided_parent_observation,
    )

    if expected_factory_name == "create_from_full_name":
        assert captured["create_from_full_name_models"] == [model_name]
    else:
        assert captured["create_model_keys"][0] == model_name

    assert (
        provided_parent_observation in captured["create_model_traces"]
        or provided_parent_observation in captured["create_from_full_name_traces"]
    )
    assert captured["toolkit_trace"] is provided_parent_observation
    assert captured["trace_ids"] == []


@pytest.mark.asyncio
async def test_send_peer_note_uses_cached_recipient_map(monkeypatch):
    captured = {}
    get_state_mock = AsyncMock(
        return_value={
            "subagents": {
                "task-1": {"role": "researcher"},
                "task-2": {"role": "analyst"},
                "task-3": {"role": "reviewer"},
            },
        },
    )
    append_calls = []

    def _fake_model_create(model_key=None, **kwargs):
        return _DummyModel(str(model_key or "dummy-model"), stream=kwargs.get("stream", False)), OpenAIChatFormatter()

    class _FakeMemory:
        def __init__(self, **kwargs):
            pass

    class _FakeToolkit:
        def __init__(self):
            self.functions = {}

        def register_tool_function(self, func):
            self.functions[func.__name__] = func

    class _FakeToolkitAdapter:
        def __init__(self, **kwargs):
            pass

        def get_toolkit(self):
            toolkit = _FakeToolkit()
            captured["toolkit"] = toolkit
            return toolkit

    def _fake_react_agent(**kwargs):
        return _DummyAgent(**kwargs)

    async def _fake_append_peer_note(**kwargs):
        append_calls.append(kwargs)
        return {"note_id": "note-1"}

    monkeypatch.setattr(subagent_factory.ModelFactory, "create", _fake_model_create)
    monkeypatch.setattr(subagent_factory, "MessagesTableMemory", _FakeMemory)
    monkeypatch.setattr(subagent_factory, "ToolkitAdapter", _FakeToolkitAdapter)
    monkeypatch.setattr(subagent_factory, "ReActAgent", _fake_react_agent)
    monkeypatch.setattr(subagent_factory, "get_state", get_state_mock)
    monkeypatch.setattr(subagent_factory, "append_peer_note", _fake_append_peer_note)

    await subagent_factory.create_subagent(
        subtask_config={
            "id": "task-1",
            "role": "researcher",
            "task_description": "Find facts",
        },
        project_id="proj-1",
        parent_run_id="run-1",
        subtask_id="task-1",
        db_client=object(),
        thread_id="thread-1",
    )

    send_tool = captured["toolkit"].functions["send_peer_note"]
    success = await send_tool("task-2", "Important finding", "Check the filing")
    failure = await send_tool("missing", "Bad target")

    assert get_state_mock.await_count == 1
    assert append_calls[0]["recipient_subtask_id"] == "task-2"
    assert append_calls[0]["recipient_role"] == "analyst"
    assert "queued" in _tool_response_text(success).lower()
    assert "allowed peer recipient" in _tool_response_text(failure).lower()
    assert "task-2" in _tool_response_text(failure)


@pytest.mark.asyncio
async def test_peer_note_hooks_stage_then_inject(monkeypatch):
    captured = {}
    drain_calls = []

    note_one = {
        "note_id": "note-1",
        "sender_subtask_id": "task-2",
        "sender_role": "Analyst",
        "recipient_subtask_id": "task-1",
        "summary": "First note",
        "details": "Details one",
    }
    note_two = {
        "note_id": "note-2",
        "sender_subtask_id": "task-3",
        "sender_role": "Reviewer",
        "recipient_subtask_id": "task-1",
        "summary": "Second note",
        "details": "",
    }

    def _fake_model_create(model_key=None, **kwargs):
        return _DummyModel(str(model_key or "dummy-model"), stream=kwargs.get("stream", False)), OpenAIChatFormatter()

    class _FakeMemory:
        def __init__(self, **kwargs):
            self.add_calls = []

        async def add(self, msg, marks=None):
            self.add_calls.append((msg, marks))

    class _FakeToolkit:
        def __init__(self):
            self.functions = {}

        def register_tool_function(self, func):
            self.functions[func.__name__] = func

    class _FakeToolkitAdapter:
        def __init__(self, **kwargs):
            pass

        def get_toolkit(self):
            toolkit = _FakeToolkit()
            captured["toolkit"] = toolkit
            return toolkit

    def _fake_react_agent(**kwargs):
        agent = _DummyAgent(**kwargs)
        captured["agent"] = agent
        return agent

    async def _fake_drain_new_notes(**kwargs):
        drain_calls.append(kwargs)
        if len(drain_calls) == 1:
            return [note_one], 1
        return [note_two], 2

    monkeypatch.setattr(subagent_factory.ModelFactory, "create", _fake_model_create)
    monkeypatch.setattr(subagent_factory, "MessagesTableMemory", _FakeMemory)
    monkeypatch.setattr(subagent_factory, "ToolkitAdapter", _FakeToolkitAdapter)
    monkeypatch.setattr(subagent_factory, "ReActAgent", _fake_react_agent)
    monkeypatch.setattr(
        subagent_factory,
        "get_state",
        AsyncMock(
            return_value={
                "subagents": {
                    "task-1": {"role": "researcher"},
                    "task-2": {"role": "analyst"},
                    "task-3": {"role": "reviewer"},
                },
            },
        ),
    )
    monkeypatch.setattr(subagent_factory, "drain_new_notes", _fake_drain_new_notes)

    agent = await subagent_factory.create_subagent(
        subtask_config={
            "id": "task-1",
            "role": "researcher",
            "task_description": "Find facts",
        },
        project_id="proj-1",
        parent_run_id="run-1",
        subtask_id="task-1",
        db_client=object(),
        thread_id="thread-1",
    )

    hook_map = {hook["hook_name"]: hook["hook"] for hook in agent.hooks}
    await hook_map["shadow_peer_note_prefetch"](agent, {})
    assert agent._shadow_pending_peer_notes == [note_one]
    await hook_map["shadow_peer_note_inject"](agent, {"tool_choice": "auto"})

    added_msg, marks = agent.memory.add_calls[0]
    assert marks == "peer_note"
    assert "First note" in added_msg.content
    assert "Second note" in added_msg.content
    assert agent._shadow_pending_peer_notes == []
    assert agent._shadow_seen_peer_note_ids == {"note-1", "note-2"}


def test_build_subagent_prompt_includes_runtime_recovery_sections():
    wake_prompt = subagent_factory._build_subagent_prompt(
        {
            "id": "task-1",
            "role": "researcher",
            "task_description": "Find facts",
            "shadow_recovery_mode": "wake_first",
            "shadow_recovery_reason": "provider response parse error",
        },
    )
    replacement_prompt = subagent_factory._build_subagent_prompt(
        {
            "id": "task-1",
            "role": "researcher",
            "task_description": "Find facts",
            "shadow_recovery_mode": "replacement",
            "shadow_recovery_handoff_summary": "Previous attempt already found the source URL.",
        },
    )

    assert "RUNTIME RECOVERY" in wake_prompt
    assert "provider response parse error" in wake_prompt
    assert "RUNTIME HANDOFF" in replacement_prompt
    assert "Previous attempt already found the source URL." in replacement_prompt


@pytest.mark.asyncio
async def test_create_subagent_honors_shadow_thread_run_id_override(monkeypatch):
    captured = {}

    def _fake_model_create(model_key=None, **kwargs):
        return _DummyModel(
            str(model_key or "dummy-model"),
            stream=kwargs.get("stream", False),
        ), OpenAIChatFormatter()

    class _FakeMemory:
        def __init__(self, **kwargs):
            captured["memory_kwargs"] = kwargs

    class _FakeToolkit:
        def __init__(self):
            self.functions = {}

        def register_tool_function(self, func):
            self.functions[func.__name__] = func

    class _FakeToolkitAdapter:
        def __init__(self, **kwargs):
            pass

        def get_toolkit(self):
            return _FakeToolkit()

    def _fake_react_agent(**kwargs):
        return _DummyAgent(**kwargs)

    monkeypatch.setattr(subagent_factory.ModelFactory, "create", _fake_model_create)
    monkeypatch.setattr(subagent_factory, "MessagesTableMemory", _FakeMemory)
    monkeypatch.setattr(subagent_factory, "ToolkitAdapter", _FakeToolkitAdapter)
    monkeypatch.setattr(subagent_factory, "ReActAgent", _fake_react_agent)
    monkeypatch.setattr(
        subagent_factory,
        "get_state",
        AsyncMock(return_value={"subagents": {"task-1": {"role": "researcher"}}}),
    )

    agent = await subagent_factory.create_subagent(
        subtask_config={
            "id": "task-1",
            "role": "researcher",
            "task_description": "Find facts",
            "shadow_thread_run_id": "shadow:run-1:task-1:replacement:ctx-123",
        },
        project_id="proj-1",
        parent_run_id="run-1",
        subtask_id="task-1",
        db_client=object(),
        thread_id="thread-1",
    )

    assert isinstance(agent, _DummyAgent)
    assert captured["memory_kwargs"]["thread_run_id"] == "shadow:run-1:task-1:replacement:ctx-123"
    assert getattr(agent, "_shadow_thread_run_id") == "shadow:run-1:task-1:replacement:ctx-123"
