import asyncio
import importlib
import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace
import pytest
import uuid

_BACKEND_ROOT = Path(__file__).resolve().parents[1]
_SERVICES_DIR = str(_BACKEND_ROOT / "services")

if "services" not in sys.modules:
    services_pkg = types.ModuleType("services")
    services_pkg.__path__ = [_SERVICES_DIR]
    sys.modules["services"] = services_pkg
elif not hasattr(sys.modules["services"], "__path__"):
    sys.modules["services"].__path__ = [_SERVICES_DIR]
elif _SERVICES_DIR not in sys.modules["services"].__path__:
    sys.modules["services"].__path__.append(_SERVICES_DIR)

if "services.redis" not in sys.modules:
    try:
        services_redis_mod = importlib.import_module("services.redis")
    except Exception:
        services_redis_mod = types.SimpleNamespace(
            REDIS_KEY_TTL=3600 * 24,
            eval_script=lambda *args, **kwargs: None,
            hgetall=lambda *args, **kwargs: None,
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
        sys.modules["services.redis"] = services_redis_mod
    setattr(sys.modules["services"], "redis", services_redis_mod)

if "services.postgresql" not in sys.modules:
    postgresql_mod = types.ModuleType("services.postgresql")

    class _DummyDBConnection:
        @property
        async def client(self):
            return object()

    postgresql_mod.DBConnection = _DummyDBConnection
    sys.modules["services.postgresql"] = postgresql_mod
    setattr(sys.modules["services"], "postgresql", postgresql_mod)

if "services.workspace_artifacts" not in sys.modules:
    workspace_artifacts_mod = types.ModuleType("services.workspace_artifacts")
    workspace_artifacts_mod.is_user_visible_workspace_artifact_path = lambda _path: True
    workspace_artifacts_mod.workspace_artifacts = SimpleNamespace(
        get_artifact_record=lambda *args, **kwargs: None,
        collect_file_paths=lambda *args, **kwargs: [],
        delete_artifact=lambda *args, **kwargs: None,
        list_entries=lambda *args, **kwargs: [],
        persist_artifact=lambda *args, **kwargs: None,
        read_artifact_bytes=lambda *args, **kwargs: None,
        rehydrate_project=lambda *args, **kwargs: {"total": 0, "rehydrated": 0},
    )
    sys.modules["services.workspace_artifacts"] = workspace_artifacts_mod
    setattr(sys.modules["services"], "workspace_artifacts", workspace_artifacts_mod)

from agentscope.formatter import OpenAIChatFormatter
from agentscope.model import ChatModelBase
from agentscope_integration.context_builder import ContextBuilder
from agentscope_integration.shadow_clone.constants import ShadowCloneMode
from agentscope_integration.shadow_clone.main_agent import MainAgent
import agentscope_integration.shadow_clone.main_agent as main_agent_module


class _DummyModel(ChatModelBase):
    async def __call__(self, *_args, **_kwargs):
        return {"ok": True}


class _FakeMsg:
    def __init__(self, text):
        self._text = text
        self.content = text
        self.role = "assistant"
        self.name = "assistant"

    def get_text_content(self):
        return self._text


class _FakeToolMsg(_FakeMsg):
    def __init__(self, text, blocks):
        super().__init__(text)
        self._blocks = blocks

    def get_content_blocks(self):
        return self._blocks


class _FakeToolkit:
    def __init__(self):
        self.functions = {}
        self.schemas = {}
        self.tools = {}

    def register_tool_function(self, func, **kwargs):
        self.functions[func.__name__] = func
        self.tools[func.__name__] = type(
            "_RegisteredTool",
            (),
            {"original_func": staticmethod(func)},
        )()
        if kwargs.get("json_schema") is not None:
            self.schemas[func.__name__] = kwargs["json_schema"]

    def get_json_schemas(self):
        return [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "parameters": schema,
                },
            }
            for name, schema in self.schemas.items()
        ]


class _FakeToolkitAdapter:
    created_kwargs = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        _FakeToolkitAdapter.created_kwargs.append(kwargs)
        self._toolkit = _FakeToolkit()
        self._tool_instances = {}

    def get_toolkit(self):
        return self._toolkit

    def clone_toolkit(self):
        cloned = _FakeToolkit()
        for func in self._toolkit.functions.values():
            cloned.register_tool_function(func)
        for name, schema in self._toolkit.schemas.items():
            if name in cloned.functions:
                cloned.schemas[name] = schema
        return cloned

    def get_tool_instance(self, name):
        return self._tool_instances.get(name)


class _FakeReActAgent:
    def __init__(self, **kwargs):
        self.toolkit = kwargs["toolkit"]
        self.sys_prompt = kwargs["sys_prompt"]
        self.kwargs = kwargs
        self.hooks = []
        self.msg_queue = None
        self._disable_msg_queue = True
        self._disable_console_output = False
        self._reply_id = None

    def set_msg_queue_enabled(self, enabled, queue=None):
        if enabled:
            self.msg_queue = queue
        else:
            self.msg_queue = None
        self._disable_msg_queue = not enabled

    def set_console_output_enabled(self, enabled):
        self._disable_console_output = not enabled

    async def print(self, msg, last=True):
        if not self._disable_msg_queue and self.msg_queue is not None:
            await self.msg_queue.put((msg, last, None))

    async def reply(self, msg):
        content = str(msg.content)
        if "complex" in content:
            await self.print(_FakeMsg("planning analysis"), False)
            self.toolkit.functions["spawn_subagents"](
                subtasks=[
                    {"id": "task-1", "role": "researcher", "task_description": "part 1"},
                ],
                dependencies=[],
            )
            await self.print(
                _FakeToolMsg(
                    "internal spawn",
                    [{"type": "tool_use", "name": "spawn_subagents"}],
                ),
                True,
            )
            await asyncio.Event().wait()
        if "Subagent Result Summaries" in content or "subtask execution has finished" in content:
            response = _FakeMsg("final aggregated report")
            await self.print(response, True)
            return response
        response = _FakeMsg("simple direct answer")
        await self.print(response, True)
        return response

    async def __call__(self, msg):
        return await self.reply(msg)

    def register_instance_hook(self, **kwargs):
        self.hooks.append(kwargs)


class _FakeLTM:
    def __init__(self):
        self.entered = False
        self.exited = False

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, *_args):
        self.exited = True


def _ltm_settings(enabled: bool) -> SimpleNamespace:
    return SimpleNamespace(
        enabled=enabled,
        attach_scope="orchestrator",
        control_mode="both",
        memories=("task", "tool"),
        fail_open=True,
        reme_config_path="agentscope_integration/memory/long_term/reme_qdrant_shared.yaml",
    )


def _patch_main_agent_dependencies(monkeypatch, *, model_factory=None):
    captured = {}
    _FakeToolkitAdapter.created_kwargs = []

    if model_factory is None:
        model_factory = lambda model_key=None, **kwargs: (
            _DummyModel(str(model_key or "dummy-model"), stream=kwargs.get("stream", False)),
            OpenAIChatFormatter(),
        )

    monkeypatch.setattr(
        main_agent_module.ModelFactory,
        "create",
        model_factory,
    )

    class _FakeMemory:
        def __init__(self, **kwargs):
            captured.setdefault("memory_kwargs_list", []).append(kwargs)
            captured["memory_kwargs"] = kwargs

    monkeypatch.setattr(main_agent_module, "MessagesTableMemory", _FakeMemory)
    monkeypatch.setattr(main_agent_module, "ToolkitAdapter", _FakeToolkitAdapter)
    monkeypatch.setattr(
        main_agent_module,
        "ThreadManagerAdapter",
        lambda db_client=None: object(),
    )
    monkeypatch.setattr(main_agent_module, "ReActAgent", _FakeReActAgent)
    monkeypatch.setattr(
        main_agent_module,
        "load_ltm_settings_from_env",
        lambda: _ltm_settings(enabled=False),
    )
    monkeypatch.setattr(
        main_agent_module,
        "create_long_term_memory",
        lambda **_kwargs: pytest.fail("LTM factory should not be called when disabled"),
    )
    return captured


def test_normalize_dependencies_accepts_depends_on_format():
    dependencies = [
        {"subtask_id": "summary", "depends_on": ["openai", "google", "anthropic"]},
        {"subtask_id": "summary", "depends_on": "openai"},
    ]

    normalized = main_agent_module._normalize_dependencies(dependencies)

    assert normalized == [
        {"from_id": "openai", "to_id": "summary"},
        {"from_id": "google", "to_id": "summary"},
        {"from_id": "anthropic", "to_id": "summary"},
    ]


@pytest.mark.asyncio
async def test_main_agent_decompose_returns_proposal(monkeypatch):
    _patch_main_agent_dependencies(monkeypatch)

    async def _fake_read_summaries(_run_id):
        return []

    monkeypatch.setattr(main_agent_module, "read_summaries", _fake_read_summaries)

    agent = MainAgent(
        thread_id="thread-1",
        project_id="project-1",
        agent_run_id="run-1",
        model_key="kimi-k2.5",
        db_client=object(),
        mode=ShadowCloneMode.AUTO,
    )
    proposal = await asyncio.wait_for(
        agent.decompose("This is a complex task"),
        timeout=0.2,
    )
    assert proposal is not None
    assert proposal["subtasks"][0]["id"] == "task-1"


@pytest.mark.asyncio
async def test_main_agent_decompose_can_skip_split(monkeypatch):
    _patch_main_agent_dependencies(monkeypatch)

    async def _fake_read_summaries(_run_id):
        return []

    monkeypatch.setattr(main_agent_module, "read_summaries", _fake_read_summaries)

    agent = MainAgent(
        thread_id="thread-1",
        project_id="project-1",
        agent_run_id="run-1",
        model_key="kimi-k2.5",
        db_client=object(),
        mode=ShadowCloneMode.AUTO,
    )
    proposal = await agent.decompose("simple question")
    assert proposal is None
    assert "simple direct answer" in agent.get_last_response_text()


@pytest.mark.asyncio
async def test_main_agent_stream_decompose_filters_internal_spawn_tool_and_synthesizes_complete(monkeypatch):
    _patch_main_agent_dependencies(monkeypatch)

    async def _fake_read_summaries(_run_id):
        return []

    monkeypatch.setattr(main_agent_module, "read_summaries", _fake_read_summaries)

    agent = MainAgent(
        thread_id="thread-1",
        project_id="project-1",
        agent_run_id="run-1",
        model_key="kimi-k2.5",
        db_client=object(),
        mode=ShadowCloneMode.AUTO,
    )

    events = []
    async for message, is_last in agent.stream_decompose("This is a complex task"):
        events.append((message.get_text_content(), is_last))

    assert events == [("planning analysis", False), ("planning analysis", True)]
    proposal = agent.get_proposal()
    assert proposal is not None
    assert proposal["subtasks"][0]["id"] == "task-1"


@pytest.mark.asyncio
async def test_main_agent_setup_gates_phase_specific_tools(monkeypatch):
    _patch_main_agent_dependencies(monkeypatch)

    async def _fake_read_summaries(_run_id):
        return []

    monkeypatch.setattr(main_agent_module, "read_summaries", _fake_read_summaries)

    agent = MainAgent(
        thread_id="thread-1",
        project_id="project-1",
        agent_run_id="run-1",
        model_key="kimi-k2.5",
        db_client=object(),
        mode=ShadowCloneMode.AUTO,
    )
    await agent.setup()

    assert "spawn_subagents" in agent._decompose_agent.toolkit.functions
    assert "read_results" not in agent._decompose_agent.toolkit.functions
    assert "read_full_result" not in agent._decompose_agent.toolkit.functions

    assert "spawn_subagents" not in agent._direct_agent.toolkit.functions
    assert "read_results" not in agent._direct_agent.toolkit.functions
    assert "read_full_result" not in agent._direct_agent.toolkit.functions

    assert "spawn_subagents" not in agent._aggregate_agent.toolkit.functions
    assert "read_results" in agent._aggregate_agent.toolkit.functions
    assert "read_full_result" in agent._aggregate_agent.toolkit.functions
    assert _FakeToolkitAdapter.created_kwargs
    assert len(_FakeToolkitAdapter.created_kwargs) == 1
    assert all(kwargs["shadow_clone_run_id"] == "run-1" for kwargs in _FakeToolkitAdapter.created_kwargs)
    assert all(kwargs["strict_sandbox"] is True for kwargs in _FakeToolkitAdapter.created_kwargs)
    assert all(kwargs["shadow_clone_fail_fast"] is False for kwargs in _FakeToolkitAdapter.created_kwargs)


@pytest.mark.asyncio
async def test_main_agent_stream_decompose_can_stream_direct_answer(monkeypatch):
    _patch_main_agent_dependencies(monkeypatch)

    async def _fake_read_summaries(_run_id):
        return []

    monkeypatch.setattr(main_agent_module, "read_summaries", _fake_read_summaries)

    agent = MainAgent(
        thread_id="thread-1",
        project_id="project-1",
        agent_run_id="run-1",
        model_key="kimi-k2.5",
        db_client=object(),
        mode=ShadowCloneMode.AUTO,
    )

    events = []
    async for message, is_last in agent.stream_decompose("simple question"):
        events.append((message.get_text_content(), is_last))

    assert events == [("simple direct answer", True)]
    assert agent.get_proposal() is None
    assert "simple direct answer" in agent.get_last_response_text()


@pytest.mark.asyncio
async def test_main_agent_aggregate_returns_report(monkeypatch):
    _patch_main_agent_dependencies(monkeypatch)

    async def _fake_read_summaries(_run_id):
        return [{"subtask_id": "task-1", "summary": "ok", "status": "completed"}]

    monkeypatch.setattr(main_agent_module, "read_summaries", _fake_read_summaries)

    agent = MainAgent(
        thread_id="thread-1",
        project_id="project-1",
        agent_run_id="run-1",
        model_key="kimi-k2.5",
        db_client=object(),
        mode=ShadowCloneMode.ON,
    )
    report = await agent.aggregate()
    assert "final aggregated report" in report


@pytest.mark.asyncio
async def test_main_agent_setup_uses_shared_memory_profile_and_compression(monkeypatch):
    captured = _patch_main_agent_dependencies(monkeypatch)

    async def _fake_read_summaries(_run_id):
        return []

    monkeypatch.setattr(main_agent_module, "read_summaries", _fake_read_summaries)

    agent = MainAgent(
        thread_id="thread-1",
        project_id="project-1",
        agent_run_id="run-1",
        model_key="kimi-k2.5",
        db_client=object(),
        mode=ShadowCloneMode.ON,
    )
    await agent.setup()

    orchestrator_memory, aggregate_memory = captured["memory_kwargs_list"]

    assert orchestrator_memory["thread_id"] == "thread-1"
    assert "thread_run_id" not in orchestrator_memory
    assert orchestrator_memory["exclude_tool_calls"] is True
    assert orchestrator_memory["enable_tool_history_summary"] is True
    assert aggregate_memory["thread_id"] == "thread-1"
    assert aggregate_memory["thread_run_id"] == "run-1"
    assert aggregate_memory["exclude_tool_calls"] is True
    assert aggregate_memory["keep_tool_results"] is False
    assert aggregate_memory["drop_tool_call_only"] is True
    assert aggregate_memory["retain_complete_tool_runs"] == 0
    assert aggregate_memory["enable_tool_history_summary"] is False
    assert getattr(agent._memory, "_save_thread_run_id") == "run-1"
    assert agent._aggregate_agent.kwargs["memory"] is not agent._main_agent.kwargs["memory"]
    assert agent._aggregate_agent.kwargs["memory"] is agent._recovery_agent.kwargs["memory"]
    assert agent._aggregate_agent.kwargs["memory"] is agent._failure_review_agent.kwargs["memory"]


@pytest.mark.asyncio
async def test_main_agent_setup_attaches_shared_ltm_to_all_phases(monkeypatch):
    _patch_main_agent_dependencies(monkeypatch)
    fake_ltm = _FakeLTM()

    monkeypatch.setattr(
        main_agent_module,
        "load_ltm_settings_from_env",
        lambda: _ltm_settings(enabled=True),
    )
    monkeypatch.setattr(
        main_agent_module,
        "create_long_term_memory",
        lambda **_kwargs: fake_ltm,
    )

    async def _fake_read_summaries(_run_id):
        return []

    monkeypatch.setattr(main_agent_module, "read_summaries", _fake_read_summaries)

    agent = MainAgent(
        thread_id="thread-1",
        project_id="project-1",
        agent_run_id="run-1",
        model_key="kimi-k2.5",
        db_client=object(),
        mode=ShadowCloneMode.ON,
    )
    await agent.setup()

    assert fake_ltm.entered is True
    assert agent._main_agent.kwargs["long_term_memory"] is fake_ltm
    assert agent._decompose_agent.kwargs["long_term_memory"] is fake_ltm
    assert agent._direct_agent.kwargs["long_term_memory"] is fake_ltm
    assert agent._aggregate_agent.kwargs["long_term_memory"] is fake_ltm
    assert agent._main_agent.kwargs["long_term_memory_mode"] == "both"
    assert agent._aggregate_agent.kwargs["long_term_memory_mode"] == "both"

    await agent.close()
    assert fake_ltm.exited is True


@pytest.mark.asyncio
async def test_main_agent_aggregate_handles_uuid_rows(monkeypatch):
    _patch_main_agent_dependencies(monkeypatch)

    async def _fake_read_summaries(_run_id):
        return [
            {
                "message_id": uuid.uuid4(),
                "subtask_id": "task-1",
                "role": "researcher",
                "status": "completed",
                "summary": "ok",
            },
        ]

    monkeypatch.setattr(main_agent_module, "read_summaries", _fake_read_summaries)

    agent = MainAgent(
        thread_id="thread-1",
        project_id="project-1",
        agent_run_id="run-1",
        model_key="kimi-k2.5",
        db_client=object(),
        mode=ShadowCloneMode.ON,
    )
    report = await agent.aggregate()
    assert "final aggregated report" in report


@pytest.mark.asyncio
async def test_main_agent_read_results_tool_returns_compact_rows(monkeypatch):
    _patch_main_agent_dependencies(monkeypatch)

    async def _fake_read_summaries(_run_id):
        return [
            {
                "subtask_id": "task-1",
                "role": "researcher",
                "status": "completed",
                "summary": "short summary",
                "artifacts": ["report.md", "sources.json"],
                "debug_blob": "should-not-leak",
                "final_answer": "full answer that should stay behind read_full_result",
            },
        ]

    monkeypatch.setattr(main_agent_module, "read_summaries", _fake_read_summaries)

    agent = MainAgent(
        thread_id="thread-1",
        project_id="project-1",
        agent_run_id="run-1",
        model_key="kimi-k2.5",
        db_client=object(),
        mode=ShadowCloneMode.ON,
    )
    await agent.setup()

    tool_response = await agent._aggregate_agent.toolkit.functions["read_results"]()
    first_block = tool_response.content[0]
    payload_text = (
        first_block.get("text")
        if isinstance(first_block, dict)
        else getattr(first_block, "text", None)
    )
    payload = json.loads(payload_text)

    assert payload == [
        {
            "subtask_id": "task-1",
            "role": "researcher",
            "status": "completed",
            "summary": "short summary",
            "artifact_count": 2,
        },
    ]


def test_build_subtask_results_message_is_compact():
    agent = MainAgent(
        thread_id="thread-1",
        project_id="project-1",
        agent_run_id="run-1",
        model_key="kimi-k2.5",
        db_client=object(),
        mode=ShadowCloneMode.ON,
    )
    rows = [
        {
            "subtask_id": "task-1",
            "role": "researcher",
            "status": "completed",
            "summary": "A" * 400,
            "artifacts": [{"path": "/workspace/out.md"}],
            "full_payload": {"nested": ["x" * 200]},
        },
        {
            "subtask_id": "task-2",
            "role": "critic",
            "status": "failed",
            "summary": "short summary",
            "error": "E" * 240,
        },
    ]

    message = agent._build_subtask_results_message(rows)

    assert "Compact subtask digest" in message
    assert "Summary counts by status" in message
    assert "full_payload" not in message
    assert ("A" * 320) not in message
    assert ("E" * 200) not in message
    assert '"artifact_count": 1' in message


def test_decompose_prompt_includes_fine_grained_dag_guidance():
    """Verify that decompose prompt preserves natural parallel layers up to the concurrency cap."""
    agent = MainAgent(
        thread_id="thread-1",
        project_id="project-1",
        agent_run_id="run-1",
        model_key="kimi-k2.5",
        db_client=object(),
        mode=ShadowCloneMode.AUTO,
    )
    prompt = agent._build_decompose_prompt()

    # Check for key guidance elements
    assert "keep them in the same layer" in prompt
    assert "Only split work into additional layers or waves" in prompt
    assert "Layer 0:" in prompt
    assert "Layer 1:" in prompt
    assert "read_full_result" in prompt
    assert "cross-reference" in prompt.lower()
    assert "dependency-driven data flow" in prompt.lower()


def test_decompose_prompt_includes_example():
    """Verify that decompose prompt includes concrete example."""
    agent = MainAgent(
        thread_id="thread-1",
        project_id="project-1",
        agent_run_id="run-1",
        model_key="kimi-k2.5",
        db_client=object(),
        mode=ShadowCloneMode.ON,
    )
    prompt = agent._build_decompose_prompt()

    # Check for example section
    assert "Example:" in prompt
    assert "Research 3 Companies" in prompt or "research Co." in prompt


def test_decompose_prompt_marks_shared_workspace_as_incidental_context():
    agent = MainAgent(
        thread_id="thread-1",
        project_id="project-1",
        agent_run_id="run-1",
        model_key="kimi-k2.5",
        db_client=object(),
        mode=ShadowCloneMode.ON,
    )
    prompt = agent._build_decompose_prompt()

    assert "incidental context" in prompt
    assert "non-authoritative" in prompt


def test_main_prompt_includes_nested_deliverable_root_path_guidance():
    agent = MainAgent(
        thread_id="thread-1",
        project_id="project-1",
        agent_run_id="run-1",
        model_key="kimi-k2.5",
        db_client=object(),
        mode=ShadowCloneMode.ON,
    )
    prompt = agent._build_main_prompt()

    assert "root folder path" in prompt.lower()
    assert "directory structure intact under /workspace" in prompt.lower()


def test_aggregate_prompt_includes_gap_identification():
    """Verify that aggregate prompt includes information gap identification."""
    agent = MainAgent(
        thread_id="thread-1",
        project_id="project-1",
        agent_run_id="run-1",
        model_key="kimi-k2.5",
        db_client=object(),
        mode=ShadowCloneMode.AUTO,
    )
    prompt = agent._build_aggregate_prompt()

    # Check for gap identification guidance
    assert "information gaps" in prompt.lower() or "incomplete" in prompt.lower()
    assert "follow-up" in prompt.lower() or "suggested" in prompt.lower()


def test_aggregate_prompt_mentions_zip_manifest_and_preserve_originals():
    agent = MainAgent(
        thread_id="thread-1",
        project_id="project-1",
        agent_run_id="run-1",
        model_key="kimi-k2.5",
        db_client=object(),
        mode=ShadowCloneMode.AUTO,
    )
    prompt = agent._build_aggregate_prompt()
    lower_prompt = prompt.lower()

    assert "every deliverable is directly under /workspace" in lower_prompt
    assert "/workspace/shadow_clone_deliverables.zip" in prompt
    assert "/workspace/shadow_clone_deliverables_manifest.txt" in prompt
    assert "preserve originals" in lower_prompt
    assert "instead of moving or deleting them" in lower_prompt


def test_subtask_results_message_mentions_zip_and_manifest_requirements():
    agent = MainAgent(
        thread_id="thread-1",
        project_id="project-1",
        agent_run_id="run-1",
        model_key="kimi-k2.5",
        db_client=object(),
        mode=ShadowCloneMode.ON,
    )

    message = agent._build_subtask_results_message(
        [
            {
                "subtask_id": "task-1",
                "role": "researcher",
                "status": "completed",
                "summary": "summary",
            },
        ]
    )

    assert "/workspace/shadow_clone_deliverables.zip" in message
    assert "/workspace/shadow_clone_deliverables_manifest.txt" in message


def _extract_text_from_tool_response(response):
    content = getattr(response, "content", []) or []
    fragments = []
    for block in content:
        if isinstance(block, dict):
            fragments.append(str(block.get("text") or ""))
        else:
            fragments.append(str(getattr(block, "text", "")))
    return "\n".join(fragment for fragment in fragments if fragment)


@pytest.mark.asyncio
async def test_main_agent_spawn_subagents_schema_is_explicit(monkeypatch):
    _patch_main_agent_dependencies(monkeypatch)

    agent = MainAgent(
        thread_id="thread-1",
        project_id="project-1",
        agent_run_id="run-1",
        model_key="kimi-k2.5",
        db_client=object(),
        mode=ShadowCloneMode.ON,
    )
    await agent.setup()

    schemas = agent._main_agent.toolkit.get_json_schemas()
    schema = next(
        item["function"]
        for item in schemas
        if item.get("function", {}).get("name") == "spawn_subagents"
    )
    parameters = schema["parameters"]
    if "function" in parameters:
        parameters = parameters["function"]["parameters"]
    subtasks = parameters["properties"]["subtasks"]
    subtask_item = subtasks["items"]

    assert parameters["required"] == ["subtasks"]
    assert subtasks["minItems"] == 1
    assert subtask_item["required"] == ["id", "role", "task_description"]
    assert "task_description" in subtask_item["properties"]


@pytest.mark.asyncio
async def test_main_agent_spawn_subagents_invalid_empty_call_returns_guidance(monkeypatch):
    _patch_main_agent_dependencies(monkeypatch)

    agent = MainAgent(
        thread_id="thread-1",
        project_id="project-1",
        agent_run_id="run-1",
        model_key="kimi-k2.5",
        db_client=object(),
        mode=ShadowCloneMode.ON,
    )
    await agent.setup()

    tool = agent._main_agent.toolkit.tools["spawn_subagents"].original_func
    first = tool()
    second = tool()

    first_text = _extract_text_from_tool_response(first)
    second_text = _extract_text_from_tool_response(second)

    assert "`subtasks` is required" in first_text
    assert "Repeated invalid call detected" in second_text


@pytest.mark.asyncio
async def test_main_agent_spawn_subagents_valid_call_returns_success_guidance(monkeypatch):
    _patch_main_agent_dependencies(monkeypatch)

    captured = []
    agent = MainAgent(
        thread_id="thread-1",
        project_id="project-1",
        agent_run_id="run-1",
        model_key="kimi-k2.5",
        db_client=object(),
        mode=ShadowCloneMode.ON,
        on_proposal_captured=lambda proposal: captured.append(proposal),
    )
    await agent.setup()

    tool = agent._main_agent.toolkit.tools["spawn_subagents"].original_func
    response = tool(
        subtasks=[
            {
                "id": "task-1",
                "role": "researcher",
                "task_description": "part 1",
            },
        ],
        dependencies=[],
    )

    text = _extract_text_from_tool_response(response)

    assert "proposal captured successfully" in text.lower()
    assert "Error:" not in text
    assert captured == [
        {
            "subtasks": [
                {
                    "id": "task-1",
                    "role": "researcher",
                    "task_description": "part 1",
                },
            ],
            "dependencies": [],
        }
    ]


@pytest.mark.asyncio
async def test_main_agent_spawn_subagents_rejects_excessive_same_layer_width(monkeypatch):
    _patch_main_agent_dependencies(monkeypatch)

    agent = MainAgent(
        thread_id="thread-1",
        project_id="project-1",
        agent_run_id="run-1",
        model_key="kimi-k2.5",
        db_client=object(),
        mode=ShadowCloneMode.ON,
    )
    await agent.setup()

    tool = agent._main_agent.toolkit.tools["spawn_subagents"].original_func
    response = tool(
        subtasks=[
            {
                "id": f"task-{index}",
                "role": "designer",
                "task_description": f"poster batch item {index}",
            }
            for index in range(11)
        ],
        dependencies=[],
    )

    text = _extract_text_from_tool_response(response)

    assert "same-layer fanout" in text
    assert "keep naturally independent work together up to 10" in text.lower()


@pytest.mark.asyncio
async def test_main_agent_prepare_shadow_clone_environment_commit_uses_shared_skill_tool(monkeypatch):
    _patch_main_agent_dependencies(monkeypatch)

    fake_skill_calls = []

    class _FakeSkillTool:
        sandbox_type = "code"

        async def _load_metadata(self):
            fake_skill_calls.append("load_metadata")
            return {"skills": [{"name": "shadow-clone-validation"}]}

        def _parse_sandbox_info(self, value):
            return dict(value or {})

        def _get_runtime_sandbox(self):
            return None

        def _get_runtime_sandbox_id(self):
            return "sandbox-runtime"

    class _CommitToolkitAdapter(_FakeToolkitAdapter):
        def __init__(self, **kwargs):
            super().__init__(**kwargs)
            self._tool_instances["skill"] = _FakeSkillTool()

    monkeypatch.setattr(main_agent_module, "ToolkitAdapter", _CommitToolkitAdapter)

    import agentscope_integration.shadow_clone.sandbox_lease as lease_module
    import sandbox.tool_base as tool_base_module

    async def _fake_get_lease(_run_id):
        return {
            "sandbox_id": "sandbox-primary",
            "sandbox_type": "code",
            "binding_state": "locked",
            "sandbox_info": {"template_id": "template-code"},
        }

    monkeypatch.setattr(lease_module, "get_run_sandbox_lease", _fake_get_lease)
    monkeypatch.setattr(
        tool_base_module.SandboxToolsBase,
        "_build_template_lineage",
        classmethod(
            lambda _cls, _sandbox_type, **_kwargs: {
                "template_type": "code",
                "template_source": "config_env",
                "template_id": "template-code",
            }
        ),
    )

    agent = MainAgent(
        thread_id="thread-commit",
        project_id="project-commit",
        agent_run_id="run-commit",
        model_key="kimi-k2.5",
        db_client=object(),
        mode=ShadowCloneMode.AUTO,
    )
    await agent.setup()

    manifest = await agent.prepare_shadow_clone_environment_commit(execution_epoch=3)

    assert fake_skill_calls == ["load_metadata"]
    assert manifest["prepared_by"] == "shadow_clone_main_agent_shared_toolkit"
    assert manifest["environment_contract"] == "run_scoped_environment_commit"
    assert manifest["execution_epoch"] == 3
    assert manifest["sandbox"]["id"] == "sandbox-runtime"
    assert manifest["skills"]["count"] == 1
