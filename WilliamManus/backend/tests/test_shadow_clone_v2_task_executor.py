from __future__ import annotations

from types import SimpleNamespace

import pytest

from agentscope_integration.shadow_clone_v2.models import AgentIdentity, EventType, Task
from agentscope_integration.shadow_clone_v2 import task_executor as task_executor_module
from agentscope_integration.shadow_clone_v2.task_executor import (
    AgentScopeShadowCloneV2TaskExecutor,
    ShadowCloneV2RunContext,
    _build_worker_opening_progress_activity,
    _build_worker_task_prompt,
    _default_agentscope_worker_factory,
)


def test_worker_task_prompt_documents_broadcast_send_message_contract() -> None:
    prompt = _build_worker_task_prompt(
        task=Task(
            id="task-1",
            run_id="run-1",
            project_id="project-1",
            thread_id="thread-1",
            subject="Coordinate peers",
            description="Broadcast the revised direction when needed.",
            created_at="2026-06-03T00:00:00+00:00",
            updated_at="2026-06-03T00:00:00+00:00",
        ),
        agent=AgentIdentity(
            agent_id="coordinator@thread-1",
            agent_name="coordinator",
            thread_id="thread-1",
            project_id="project-1",
            role="coordinator",
        ),
    )

    assert 'recipient="*"' in prompt
    assert 'message_type="broadcast"' in prompt
    assert "broadcast" in prompt.lower()


def test_worker_task_prompt_requires_visible_assistant_progress_before_tools() -> None:
    prompt = _build_worker_task_prompt(
        task=Task(
            id="task-1",
            run_id="run-1",
            project_id="project-1",
            thread_id="thread-1",
            subject="Realtime progress",
            description=(
                "Start by outputting SUB_STREAM_MARKER_example in assistant text, "
                "then run a long shell wait and write a file."
            ),
            created_at="2026-06-03T00:00:00+00:00",
            updated_at="2026-06-03T00:00:00+00:00",
        ),
        agent=AgentIdentity(
            agent_id="realtime-agent-1@thread-1",
            agent_name="realtime-agent-1",
            thread_id="thread-1",
            project_id="project-1",
            role="writer",
        ),
    )

    assert "Before calling any tool" in prompt
    assert "assistant-visible" in prompt
    assert "not hidden reasoning" in prompt
    assert "If the task asks you to start by outputting a marker" in prompt
    assert "SUB_STREAM_MARKER" in prompt


def test_worker_opening_progress_extracts_fragment_style_marker_before_tools() -> None:
    activity = _build_worker_opening_progress_activity(
        task=Task(
            id="realtime-e2e-agent-1",
            run_id="run-1",
            project_id="project-1",
            thread_id="thread-1",
            subject="E2E realtime visibility test - agent 1",
            description=(
                "STEP 1 — SUB_STREAM_MARKER OUTPUT:\n"
                "At the very beginning of your execution, produce in your own "
                "assistant natural language output a concatenated marker string. "
                "This marker is built by joining exactly three fragments with "
                "underscore characters (`_`):\n"
                "- Fragment A: the literal string `SUB_STREAM_MARKER`\n"
                "- Fragment B: the literal string `realtime_agent_1`\n"
                "- Fragment C: the literal string `1780619555147`\n\n"
                "Join them as: FragmentA + \"_\" + FragmentB + \"_\" + FragmentC\n"
                "Then call a shell tool."
            ),
            created_at="2026-06-05T00:00:00+00:00",
            updated_at="2026-06-05T00:00:00+00:00",
        ),
        agent=AgentIdentity(
            agent_id="realtime-agent-1@thread-1",
            agent_name="realtime-agent-1",
            thread_id="thread-1",
            project_id="project-1",
            role="writer",
        ),
        run_context=ShadowCloneV2RunContext(
            agent_run_id="run-1",
            thread_id="thread-1",
            project_id="project-1",
            account_id="account-1",
            model_key="model-1",
            thread_run_id="thread-run-1",
        ),
        sequence=11,
    )

    assert activity["metadata"]["opening_progress"] is True
    assert activity["metadata"]["stream_status"] == "chunk"
    assert activity["subtask_id"] == "realtime-e2e-agent-1"
    assert "SUB_STREAM_MARKER_realtime_agent_1_1780619555147" in (
        activity["content"]["content"]
    )


def test_worker_opening_progress_extracts_equals_fragment_marker_before_tools() -> None:
    activity = _build_worker_opening_progress_activity(
        task=Task(
            id="realtime-e2e-agent-2",
            run_id="run-1",
            project_id="project-1",
            thread_id="thread-1",
            subject="E2E realtime visibility test - agent 2",
            description=(
                "Your first action MUST output a marker formed by joining "
                "these fragments with underscores: fragment A = "
                '"SUB_STREAM_MARKER", fragment B = "realtime_agent_2", '
                'fragment C = "1780619555147". Then use a tool.'
            ),
            created_at="2026-06-05T00:00:00+00:00",
            updated_at="2026-06-05T00:00:00+00:00",
        ),
        agent=AgentIdentity(
            agent_id="realtime-agent-2@thread-1",
            agent_name="realtime-agent-2",
            thread_id="thread-1",
            project_id="project-1",
            role="writer",
        ),
        run_context=ShadowCloneV2RunContext(
            agent_run_id="run-1",
            thread_id="thread-1",
            project_id="project-1",
            account_id="account-1",
            model_key="model-1",
            thread_run_id="thread-run-1",
        ),
        sequence=12,
    )

    assert "SUB_STREAM_MARKER_realtime_agent_2_1780619555147" in (
        activity["content"]["content"]
    )


class _FakeWorkerResponse:
    def __init__(self, text: str) -> None:
        self._text = text

    def get_text_content(self) -> str:
        return self._text


class _FakeWorker:
    def __init__(self, calls: list[object], response_text: str) -> None:
        self._calls = calls
        self._response_text = response_text
        self.agent = object()

    async def __call__(self, msg):
        self._calls.append(msg)
        return _FakeWorkerResponse(self._response_text)

    def get_agent(self):
        return self.agent


class _FakeStreamMsg:
    role = "assistant"

    def __init__(self, text: str) -> None:
        self._text = text
        self.content = [{"type": "text", "text": text}]

    def get_text_content(self) -> str:
        return self._text


class _FakeToolChunk:
    def __init__(self, content: str, *, is_last: bool = True) -> None:
        self.content = content
        self.is_last = is_last
        self.is_interrupted = False
        self.metadata = {}


class _FakeShadowTools:
    def __init__(self, next_sequence: int) -> None:
        self._next_sequence = next_sequence

    @property
    def next_sequence(self) -> int:
        return self._next_sequence

    def _reserve_sequences(self, count: int) -> int:
        start = self._next_sequence
        self._next_sequence += count
        return start


class _FakeToolkit:
    async def call_tool_function(self, tool_call):
        async def _chunks():
            yield _FakeToolChunk("wrote /workspace/story.md")

        return _chunks()


class _FakeMixedToolkit:
    def __init__(self) -> None:
        self.management_sequences: list[int] = []

    async def call_tool_function(self, tool_call):
        name = tool_call.get("name")
        if name == "task_update":
            shadow_tools = getattr(self, "_shadow_clone_v2_agent_tools")
            self.management_sequences.append(shadow_tools._reserve_sequences(1))

            async def _management_chunks():
                yield _FakeToolChunk("task updated")

            return _management_chunks()

        async def _ordinary_chunks():
            yield _FakeToolChunk(f"{name} completed")

        return _ordinary_chunks()


class _FakeMixedToolUsingWorker:
    def __init__(self) -> None:
        self.toolkit = _FakeMixedToolkit()

    async def __call__(self, _msg):
        for tool_call in [
            {"id": "ordinary-1", "name": "write_file", "input": {"path": "a.md"}},
            {"id": "management-1", "name": "task_update", "input": {}},
            {"id": "ordinary-2", "name": "read_file", "input": {"path": "a.md"}},
        ]:
            tool_result = await self.toolkit.call_tool_function(tool_call)
            async for _chunk in tool_result:
                pass
        return _FakeWorkerResponse("mixed tool worker response")


class _FakeManagementOnlyToolkit:
    def __init__(self) -> None:
        self.called_names: list[str] = []

    async def call_tool_function(self, tool_call):
        self.called_names.append(tool_call.get("name"))
        shadow_tools = getattr(self, "_shadow_clone_v2_agent_tools")
        shadow_tools._reserve_sequences(1)

        async def _chunks():
            yield _FakeToolChunk("management result")

        return _chunks()


class _FakeManagementOnlyWorker:
    MANAGEMENT_TOOL_NAMES = [
        "team_shutdown",
        "message_read",
        "message_ack",
        "message_dead_letter",
    ]

    def __init__(self) -> None:
        self.toolkit = _FakeManagementOnlyToolkit()

    async def __call__(self, _msg):
        for name in self.MANAGEMENT_TOOL_NAMES:
            tool_result = await self.toolkit.call_tool_function(
                {"id": f"{name}-1", "name": name, "input": {}}
            )
            async for _chunk in tool_result:
                pass
        return _FakeWorkerResponse("management only worker response")




class _FakeSensitiveResultToolkit:
    async def call_tool_function(self, tool_call):
        async def _chunks():
            yield _FakeToolChunk(
                {
                    "token": "secret-dict-token",
                    "nested": {"api_key": "secret-dict-api-key"},
                    "message": "password: secret-colon-password authorization: Bearer secret-bearer-token cookie=session=secret-cookie",
                    "json_text": '{"secret":"secret-json-value"}',
                }
            )

        return _chunks()


class _FakeSensitiveResultWorker:
    def __init__(self) -> None:
        self.toolkit = _FakeSensitiveResultToolkit()

    async def __call__(self, _msg):
        tool_result = await self.toolkit.call_tool_function(
            {
                "id": "tool-sensitive-result",
                "name": "read_file",
                "input": {"path": "/workspace/result.txt"},
            }
        )
        async for _chunk in tool_result:
            pass
        return _FakeWorkerResponse("sensitive result worker response")


class _FakeFailingSensitiveToolkit:
    async def call_tool_function(self, tool_call):
        raise RuntimeError("authorization: Bearer secret-error-token password=secret-error-password")


class _FakeFailingSensitiveWorker:
    def __init__(self) -> None:
        self.toolkit = _FakeFailingSensitiveToolkit()

    async def __call__(self, _msg):
        tool_result = await self.toolkit.call_tool_function(
            {
                "id": "tool-sensitive-error",
                "name": "read_file",
                "input": {"path": "/workspace/error.txt"},
            }
        )
        async for _chunk in tool_result:
            pass
        return _FakeWorkerResponse("unreachable")


class _FakeToolUsingWorker:
    def __init__(self) -> None:
        self.toolkit = _FakeToolkit()

    async def __call__(self, _msg):
        tool_result = await self.toolkit.call_tool_function(
            {
                "id": "tool-1",
                "name": "write_file",
                "input": {"path": "/workspace/story.md"},
            }
        )
        async for _chunk in tool_result:
            pass
        return _FakeWorkerResponse("worker response text")


class _FakeLargeSensitiveToolUsingWorker:
    def __init__(self) -> None:
        self.toolkit = _FakeToolkit()

    async def __call__(self, _msg):
        tool_result = await self.toolkit.call_tool_function(
            {
                "id": "tool-sensitive",
                "name": "write_file",
                "input": {
                    "path": "/workspace/secret.md",
                    "content": "A" * 20_000,
                    "password": "super-secret-password",
                    "nested": {"api_key": "secret-api-key"},
                },
            }
        )
        async for _chunk in tool_result:
            pass
        return _FakeWorkerResponse("sensitive worker response")


def _task(task_id: str = "task-1") -> Task:
    return Task(
        id=task_id,
        run_id="run-1",
        thread_id="thread-1",
        subject="Prepare memo",
        description="Create a concise market-entry memo with risks and recommendations.",
        created_at="2026-05-31T00:00:00+00:00",
        updated_at="2026-05-31T00:00:00+00:00",
    )


def _agent(agent_id: str = "agent-1") -> AgentIdentity:
    return AgentIdentity(
        agent_id=agent_id,
        agent_name="Market Analyst",
        thread_id="thread-1",
        project_id="project-1",
        role="research analyst",
    )


@pytest.mark.asyncio
async def test_agentscope_task_executor_builds_worker_with_deepseek_v4_pro_max_context() -> (
    None
):
    factory_calls = []
    worker_messages = []

    async def _worker_factory(**kwargs):
        factory_calls.append(kwargs)
        return _FakeWorker(worker_messages, "worker response text")

    executor = AgentScopeShadowCloneV2TaskExecutor(worker_factory=_worker_factory)

    result = await executor.execute(
        task=_task(),
        agent=_agent(),
        run_context=ShadowCloneV2RunContext(
            thread_id="thread-1",
            project_id="project-1",
            agent_run_id="run-1",
            model_key="fallback-model",
            subagent_model="deepseek-v4-pro-max",
            db_client=object(),
            trace=object(),
        ),
    )

    assert result.output == "worker response text"
    assert len(factory_calls) == 1
    assert factory_calls[0]["model_key"] == "deepseek-v4-pro-max"
    assert factory_calls[0]["task"].id == "task-1"
    assert factory_calls[0]["agent"].agent_name == "Market Analyst"
    assert len(worker_messages) == 1
    prompt = worker_messages[0].content
    assert "Create a concise market-entry memo" in prompt
    assert "Market Analyst" in prompt
    assert "research analyst" in prompt

    await executor.execute(
        task=_task("task-2"),
        agent=_agent("agent-2"),
        run_context=ShadowCloneV2RunContext(
            thread_id="thread-1",
            project_id="project-1",
            agent_run_id="run-1",
            model_key="fallback-model",
            subagent_model="   ",
        ),
    )

    assert factory_calls[1]["model_key"] == "fallback-model"


@pytest.mark.asyncio
async def test_agentscope_task_executor_includes_inbox_messages_in_worker_prompt() -> None:
    worker_messages = []

    async def _worker_factory(**_kwargs):
        return _FakeWorker(worker_messages, "consumed peer message")

    executor = AgentScopeShadowCloneV2TaskExecutor(worker_factory=_worker_factory)

    result = await executor.execute(
        task=_task(),
        agent=_agent(),
        run_context=ShadowCloneV2RunContext(
            thread_id="thread-1",
            project_id="project-1",
            agent_run_id="run-1",
            model_key="frontend-selected-model",
        ),
        inbox_messages=[
            SimpleNamespace(
                id="msg-1",
                sender="comm-agent-a",
                recipient="Market Analyst",
                summary="Peer handoff",
                text="P2P_FROM_A_TO_B_TEST_MARKER",
                payload={"marker": "P2P_FROM_A_TO_B_TEST_MARKER"},
                created_at="2026-06-03T00:00:00+00:00",
            )
        ],
    )

    assert result.output == "consumed peer message"
    prompt = worker_messages[0].content
    assert "## Inbox messages" in prompt
    assert "comm-agent-a" in prompt
    assert "P2P_FROM_A_TO_B_TEST_MARKER" in prompt


@pytest.mark.asyncio
async def test_agentscope_task_executor_records_worker_tool_call_lifecycle(
    monkeypatch,
) -> None:
    appended_events = []

    async def _append_event(**kwargs):
        appended_events.append(kwargs)
        return object()

    async def _worker_factory(**_kwargs):
        return _FakeToolUsingWorker()

    monkeypatch.setattr(task_executor_module.event_log, "append_event", _append_event)

    executor = AgentScopeShadowCloneV2TaskExecutor(worker_factory=_worker_factory)

    result = await executor.execute(
        task=_task(),
        agent=_agent(),
        run_context=ShadowCloneV2RunContext(
            thread_id="thread-1",
            project_id="project-1",
            agent_run_id="run-1",
            model_key="fallback-model",
            tool_sequence_start=40,
        ),
    )

    assert result.output == "worker response text"
    assert result.next_sequence == 42
    assert [event["event_type"] for event in appended_events] == [
        EventType.TOOL_CALL_STARTED,
        EventType.TOOL_CALL_COMPLETED,
    ]
    assert [event["sequence"] for event in appended_events] == [40, 41]
    assert appended_events[0]["activity_owner"] == "agent:Market Analyst"
    assert appended_events[0]["payload"]["tool_name"] == "write_file"
    assert appended_events[0]["payload"]["task_id"] == "task-1"
    assert appended_events[0]["payload"]["arguments_summary"]["path"] == (
        "/workspace/story.md"
    )
    assert appended_events[0]["payload"]["arguments_redacted"] is False
    assert appended_events[1]["payload"]["result_summary"] == (
        "wrote /workspace/story.md"
    )


@pytest.mark.asyncio
async def test_agentscope_task_executor_emits_subagent_text_chunk_before_worker_completion(
    monkeypatch,
) -> None:
    calls = []
    streamed_activities = []
    worker = _FakeWorker(calls, "worker final response")

    async def _worker_factory(**_kwargs):
        return worker

    async def _fake_stream_printing_messages(*, agents, coroutine_task):
        assert agents == [worker.agent]
        yield _FakeStreamMsg("SUB_STREAM_MARKER_executor_chunk"), False
        result = await coroutine_task
        yield _FakeStreamMsg(result.get_text_content()), True

    async def _on_stream_activity(activity):
        streamed_activities.append(activity)

    monkeypatch.setattr(
        task_executor_module,
        "stream_printing_messages",
        _fake_stream_printing_messages,
        raising=False,
    )

    executor = AgentScopeShadowCloneV2TaskExecutor(worker_factory=_worker_factory)

    result = await executor.execute(
        task=_task(),
        agent=_agent(),
        run_context=ShadowCloneV2RunContext(
            thread_id="thread-1",
            project_id="project-1",
            agent_run_id="run-1",
            model_key="fallback-model",
            tool_sequence_start=50,
            thread_run_id="thread-run-1",
        ),
        on_stream_activity=_on_stream_activity,
    )

    assert result.output == "worker final response"
    assert streamed_activities
    first_activity = streamed_activities[0]
    assert first_activity["type"] == "subagent_activity"
    assert first_activity["subtask_id"] == "task-1"
    assert first_activity["agent_name"] == "Market Analyst"
    assert first_activity["message_type"] == "assistant"
    assert first_activity["metadata"]["stream_status"] == "chunk"
    assert first_activity["content"]["content"] == "SUB_STREAM_MARKER_executor_chunk"


@pytest.mark.asyncio
async def test_agentscope_task_executor_emits_opening_visible_progress_before_first_tool_when_no_model_chunk(
    monkeypatch,
) -> None:
    appended_events = []
    streamed_activities = []
    worker = _FakeToolUsingWorker()

    async def _append_event(**kwargs):
        appended_events.append(kwargs)
        return object()

    async def _worker_factory(**_kwargs):
        return worker

    async def _fake_stream_printing_messages(*, agents, coroutine_task):
        await coroutine_task
        if False:
            yield None

    async def _on_stream_activity(activity):
        streamed_activities.append(activity)

    monkeypatch.setattr(task_executor_module.event_log, "append_event", _append_event)
    monkeypatch.setattr(
        task_executor_module,
        "stream_printing_messages",
        _fake_stream_printing_messages,
        raising=False,
    )

    task = _task().model_copy(
        update={
            "description": (
                "Start by outputting a marker assembled from these parts: "
                '["SUB_STREAM_MARKER", "realtime_agent_1", "1780613951313"]. '
                "Then call write_file."
            )
        }
    )
    agent = _agent().model_copy(update={"agent_name": "realtime-agent-1"})
    executor = AgentScopeShadowCloneV2TaskExecutor(worker_factory=_worker_factory)

    result = await executor.execute(
        task=task,
        agent=agent,
        run_context=ShadowCloneV2RunContext(
            thread_id="thread-1",
            project_id="project-1",
            agent_run_id="run-1",
            model_key="fallback-model",
            tool_sequence_start=50,
            thread_run_id="thread-run-1",
        ),
        on_stream_activity=_on_stream_activity,
    )

    assert result.output == "worker response text"
    assert streamed_activities, "executor should synthesize visible opening progress before first tool"
    first_activity = streamed_activities[0]
    assert first_activity["type"] == "subagent_activity"
    assert first_activity["message_type"] == "assistant"
    assert first_activity["subtask_id"] == task.id
    assert first_activity["agent_name"] == "realtime-agent-1"
    assert first_activity["metadata"]["stream_status"] == "chunk"
    assert "SUB_STREAM_MARKER_realtime_agent_1_1780613951313" in first_activity["content"]["content"]
    assert appended_events[0]["event_type"] == EventType.TOOL_CALL_STARTED
    assert first_activity["sequence"] < appended_events[0]["sequence"]


@pytest.mark.asyncio
async def test_agentscope_task_executor_does_not_call_worker_twice_when_stream_has_no_printed_messages(
    monkeypatch,
) -> None:
    calls = []
    streamed_activities = []
    worker = _FakeWorker(calls, "worker final response without printed stream")

    async def _worker_factory(**_kwargs):
        return worker

    async def _fake_stream_printing_messages(*, agents, coroutine_task):
        assert agents == [worker.agent]
        await coroutine_task
        if False:
            yield None

    async def _on_stream_activity(activity):
        streamed_activities.append(activity)

    monkeypatch.setattr(
        task_executor_module,
        "stream_printing_messages",
        _fake_stream_printing_messages,
        raising=False,
    )

    executor = AgentScopeShadowCloneV2TaskExecutor(worker_factory=_worker_factory)

    result = await executor.execute(
        task=_task(),
        agent=_agent(),
        run_context=ShadowCloneV2RunContext(
            thread_id="thread-1",
            project_id="project-1",
            agent_run_id="run-1",
            model_key="fallback-model",
            tool_sequence_start=50,
            thread_run_id="thread-run-1",
        ),
        on_stream_activity=_on_stream_activity,
    )

    assert result.output == "worker final response without printed stream"
    assert len(calls) == 1
    assert streamed_activities == []


@pytest.mark.asyncio
async def test_agentscope_task_executor_does_not_call_worker_twice_when_empty_stream_returns_none(
    monkeypatch,
) -> None:
    calls = []
    streamed_activities = []

    class _NoneReturningWorker:
        agent = object()

        async def __call__(self, msg):
            calls.append(msg)
            return None

        def get_agent(self):
            return self.agent

    worker = _NoneReturningWorker()

    async def _worker_factory(**_kwargs):
        return worker

    async def _fake_stream_printing_messages(*, agents, coroutine_task):
        assert agents == [worker.agent]
        assert await coroutine_task is None
        if False:
            yield None

    async def _on_stream_activity(activity):
        streamed_activities.append(activity)

    monkeypatch.setattr(
        task_executor_module,
        "stream_printing_messages",
        _fake_stream_printing_messages,
        raising=False,
    )

    executor = AgentScopeShadowCloneV2TaskExecutor(worker_factory=_worker_factory)

    result = await executor.execute(
        task=_task(),
        agent=_agent(),
        run_context=ShadowCloneV2RunContext(
            thread_id="thread-1",
            project_id="project-1",
            agent_run_id="run-1",
            model_key="fallback-model",
            tool_sequence_start=50,
            thread_run_id="thread-run-1",
        ),
        on_stream_activity=_on_stream_activity,
    )

    assert result.output == ""
    assert len(calls) == 1
    assert streamed_activities == []


@pytest.mark.asyncio
async def test_agentscope_task_executor_preserves_streamed_final_when_worker_returns_none(
    monkeypatch,
) -> None:
    calls = []
    streamed_activities = []

    class _NoneReturningWorker:
        agent = object()

        async def __call__(self, msg):
            calls.append(msg)
            return None

        def get_agent(self):
            return self.agent

    worker = _NoneReturningWorker()

    async def _worker_factory(**_kwargs):
        return worker

    async def _fake_stream_printing_messages(*, agents, coroutine_task):
        assert agents == [worker.agent]
        assert await coroutine_task is None
        yield _FakeStreamMsg("streamed final response"), True

    async def _on_stream_activity(activity):
        streamed_activities.append(activity)

    monkeypatch.setattr(
        task_executor_module,
        "stream_printing_messages",
        _fake_stream_printing_messages,
        raising=False,
    )

    executor = AgentScopeShadowCloneV2TaskExecutor(worker_factory=_worker_factory)

    result = await executor.execute(
        task=_task(),
        agent=_agent(),
        run_context=ShadowCloneV2RunContext(
            thread_id="thread-1",
            project_id="project-1",
            agent_run_id="run-1",
            model_key="fallback-model",
            tool_sequence_start=50,
            thread_run_id="thread-run-1",
        ),
        on_stream_activity=_on_stream_activity,
    )

    assert result.output == "streamed final response"
    assert len(calls) == 1
    assert streamed_activities[-1]["metadata"]["stream_status"] == "complete"


@pytest.mark.asyncio
async def test_agentscope_task_executor_records_bounded_redacted_tool_argument_summary(
    monkeypatch,
) -> None:
    appended_events = []

    async def _append_event(**kwargs):
        appended_events.append(kwargs)
        return object()

    async def _worker_factory(**_kwargs):
        return _FakeLargeSensitiveToolUsingWorker()

    monkeypatch.setattr(task_executor_module.event_log, "append_event", _append_event)

    executor = AgentScopeShadowCloneV2TaskExecutor(worker_factory=_worker_factory)

    result = await executor.execute(
        task=_task(),
        agent=_agent(),
        run_context=ShadowCloneV2RunContext(
            thread_id="thread-1",
            project_id="project-1",
            agent_run_id="run-1",
            model_key="fallback-model",
            tool_sequence_start=50,
        ),
    )

    assert result.output == "sensitive worker response"
    started_payload = appended_events[0]["payload"]
    assert "arguments" not in started_payload
    assert started_payload["arguments_redacted"] is True
    assert started_payload["arguments_size_bytes"] > 20_000
    assert started_payload["arguments_summary"]["password"] == "[REDACTED]"
    assert started_payload["arguments_summary"]["nested"]["api_key"] == "[REDACTED]"
    assert started_payload["arguments_summary"]["path"] == "/workspace/secret.md"
    assert len(started_payload["arguments_summary"]["content"]) < 600
    assert "super-secret-password" not in str(started_payload)
    assert "secret-api-key" not in str(started_payload)
    assert "A" * 1000 not in str(started_payload)


@pytest.mark.asyncio
async def test_agentscope_task_executor_redacts_sensitive_tool_result_summary(
    monkeypatch,
) -> None:
    appended_events = []

    async def _append_event(**kwargs):
        appended_events.append(kwargs)
        return object()

    async def _worker_factory(**_kwargs):
        return _FakeSensitiveResultWorker()

    monkeypatch.setattr(task_executor_module.event_log, "append_event", _append_event)

    executor = AgentScopeShadowCloneV2TaskExecutor(worker_factory=_worker_factory)

    result = await executor.execute(
        task=_task(),
        agent=_agent(),
        run_context=ShadowCloneV2RunContext(
            thread_id="thread-1",
            project_id="project-1",
            agent_run_id="run-1",
            model_key="fallback-model",
            tool_sequence_start=60,
        ),
    )

    assert result.output == "sensitive result worker response"
    completed_payload = appended_events[1]["payload"]
    result_summary = completed_payload["result_summary"]
    assert "[REDACTED]" in result_summary
    assert "secret-dict-token" not in result_summary
    assert "secret-dict-api-key" not in result_summary
    assert "secret-colon-password" not in result_summary
    assert "secret-bearer-token" not in result_summary
    assert "secret-cookie" not in result_summary
    assert "secret-json-value" not in result_summary


@pytest.mark.asyncio
async def test_agentscope_task_executor_redacts_sensitive_tool_failure_error(
    monkeypatch,
) -> None:
    appended_events = []

    async def _append_event(**kwargs):
        appended_events.append(kwargs)
        return object()

    async def _worker_factory(**_kwargs):
        return _FakeFailingSensitiveWorker()

    monkeypatch.setattr(task_executor_module.event_log, "append_event", _append_event)

    executor = AgentScopeShadowCloneV2TaskExecutor(worker_factory=_worker_factory)

    with pytest.raises(RuntimeError):
        await executor.execute(
            task=_task(),
            agent=_agent(),
            run_context=ShadowCloneV2RunContext(
                thread_id="thread-1",
                project_id="project-1",
                agent_run_id="run-1",
                model_key="fallback-model",
                tool_sequence_start=70,
            ),
        )

    failed_payload = appended_events[1]["payload"]
    assert failed_payload["error"] == (
        "authorization: Bearer [REDACTED] password=[REDACTED]"
    )
    assert "secret-error-token" not in str(failed_payload)
    assert "secret-error-password" not in str(failed_payload)


@pytest.mark.asyncio
async def test_agentscope_task_executor_keeps_ordinary_and_v2_management_tool_sequences_unique(
    monkeypatch,
) -> None:
    appended_events = []

    async def _append_event(**kwargs):
        appended_events.append(kwargs)
        return object()

    async def _worker_factory(**_kwargs):
        worker = _FakeMixedToolUsingWorker()
        worker._shadow_clone_v2_agent_tools = _FakeShadowTools(next_sequence=40)
        worker.toolkit._shadow_clone_v2_agent_tools = worker._shadow_clone_v2_agent_tools
        return worker

    monkeypatch.setattr(task_executor_module.event_log, "append_event", _append_event)

    executor = AgentScopeShadowCloneV2TaskExecutor(worker_factory=_worker_factory)

    result = await executor.execute(
        task=_task(),
        agent=_agent(),
        run_context=ShadowCloneV2RunContext(
            thread_id="thread-1",
            project_id="project-1",
            agent_run_id="run-1",
            model_key="fallback-model",
            tool_sequence_start=40,
        ),
    )

    assert result.output == "mixed tool worker response"
    assert result.next_sequence == 45
    assert [event["sequence"] for event in appended_events] == [40, 41, 43, 44]
    assert len({event["sequence"] for event in appended_events}) == len(
        appended_events
    )
    assert appended_events[0]["payload"]["tool_name"] == "write_file"
    assert appended_events[2]["payload"]["tool_name"] == "read_file"


@pytest.mark.asyncio
async def test_agentscope_task_executor_does_not_double_record_v2_management_tools(
    monkeypatch,
) -> None:
    appended_events = []

    async def _append_event(**kwargs):
        appended_events.append(kwargs)
        return object()

    async def _worker_factory(**_kwargs):
        worker = _FakeManagementOnlyWorker()
        worker._shadow_clone_v2_agent_tools = _FakeShadowTools(next_sequence=10)
        worker.toolkit._shadow_clone_v2_agent_tools = worker._shadow_clone_v2_agent_tools
        return worker

    monkeypatch.setattr(task_executor_module.event_log, "append_event", _append_event)

    executor = AgentScopeShadowCloneV2TaskExecutor(worker_factory=_worker_factory)

    result = await executor.execute(
        task=_task(),
        agent=_agent(),
        run_context=ShadowCloneV2RunContext(
            thread_id="thread-1",
            project_id="project-1",
            agent_run_id="run-1",
            model_key="fallback-model",
            tool_sequence_start=10,
        ),
    )

    assert result.output == "management only worker response"
    assert result.next_sequence == 14
    assert appended_events == []


@pytest.mark.asyncio
async def test_default_agentscope_worker_factory_uses_non_strict_sandbox_and_full_model_resolution(
    monkeypatch,
) -> None:
    captured = {"create_calls": [], "create_from_full_name_calls": []}

    class _FakeModelFactory:
        @staticmethod
        def create(model_key=None, **kwargs):
            captured["create_calls"].append((model_key, kwargs))
            return object(), object()

        @staticmethod
        def create_from_full_name(model_name=None, **kwargs):
            captured["create_from_full_name_calls"].append((model_name, kwargs))
            return object(), object()

    class _FakeToolkit:
        def register_tool_function(self, _func):
            return None

    class _FakeToolkitAdapter:
        def __init__(self, **kwargs):
            captured["toolkit_kwargs"] = kwargs

        def get_toolkit(self):
            return _FakeToolkit()

    class _FakeWorkerAgent:
        def __init__(self, **kwargs):
            captured["worker_kwargs"] = kwargs

    monkeypatch.setattr(
        "agentscope_integration.models.ModelFactory",
        _FakeModelFactory,
    )
    monkeypatch.setattr(
        "agentscope_integration.tools.ToolkitAdapter",
        _FakeToolkitAdapter,
    )
    monkeypatch.setattr(
        "agentscope_integration.agents.worker.WorkerAgent",
        _FakeWorkerAgent,
    )

    worker = await _default_agentscope_worker_factory(
        model_key="openrouter/deepseek/deepseek-v4-pro",
        run_context=ShadowCloneV2RunContext(
            thread_id="thread-1",
            project_id="project-1",
            agent_run_id="run-1",
            model_key="deepseek-v4-pro-max",
            db_client=object(),
            trace=object(),
        ),
    )

    assert isinstance(worker, _FakeWorkerAgent)
    assert captured["create_calls"] == []
    assert (
        captured["create_from_full_name_calls"][0][0]
        == "openrouter/deepseek/deepseek-v4-pro"
    )
    assert captured["toolkit_kwargs"]["strict_sandbox"] is False
    assert captured["toolkit_kwargs"]["shadow_clone_fail_fast"] is False
    assert captured["toolkit_kwargs"]["shadow_clone_run_id"] == "run-1"


@pytest.mark.asyncio
async def test_default_worker_factory_registers_shadow_clone_v2_agent_tools(
    monkeypatch,
) -> None:
    captured = {"registered_tool_names": []}

    class _FakeModelFactory:
        @staticmethod
        def create(model_key=None, **kwargs):
            return object(), object()

    class _FakeToolkit:
        def __init__(self) -> None:
            self.tools = {}

        def register_tool_function(self, func):
            captured["registered_tool_names"].append(func.__name__)
            self.tools[func.__name__] = func

    class _FakeToolkitAdapter:
        def __init__(self, **kwargs):
            captured["toolkit_kwargs"] = kwargs
            self._toolkit = _FakeToolkit()

        def get_toolkit(self):
            return self._toolkit

    class _FakeWorkerAgent:
        def __init__(self, **kwargs):
            captured["worker_kwargs"] = kwargs

    monkeypatch.setattr(
        "agentscope_integration.models.ModelFactory",
        _FakeModelFactory,
    )
    monkeypatch.setattr(
        "agentscope_integration.tools.ToolkitAdapter",
        _FakeToolkitAdapter,
    )
    monkeypatch.setattr(
        "agentscope_integration.agents.worker.WorkerAgent",
        _FakeWorkerAgent,
    )

    await _default_agentscope_worker_factory(
        model_key="deepseek-v4-pro-max",
        task=_task("task-1"),
        agent=_agent("agent-1"),
        run_context=ShadowCloneV2RunContext(
            thread_id="thread-1",
            project_id="project-1",
            agent_run_id="run-1",
            model_key="deepseek-v4-pro-max",
            db_client=object(),
            trace=object(),
        ),
    )

    assert {
        "team_create",
        "task_create",
        "task_get",
        "task_update",
        "task_list",
        "send_message",
    }.issubset(set(captured["registered_tool_names"]))
    assert captured["worker_kwargs"]["toolkit"].tools["send_message"]


@pytest.mark.asyncio
async def test_default_worker_factory_tools_are_callable_during_worker_turn(
    monkeypatch,
) -> None:
    import agentscope_integration.shadow_clone_v2.agent_tools as agent_tools_module

    listed_tasks = []
    appended_events = []
    sent_messages = []
    captured = {}

    class _FakeModelFactory:
        @staticmethod
        def create(model_key=None, **kwargs):
            assert model_key == "frontend-selected-model"
            return object(), object()

    class _FakeToolkit:
        def __init__(self) -> None:
            self.tools = {}

        def register_tool_function(self, func):
            self.tools[func.__name__] = func

    class _FakeToolkitAdapter:
        def __init__(self, **kwargs):
            captured["toolkit_kwargs"] = kwargs
            self._toolkit = _FakeToolkit()

        def get_toolkit(self):
            return self._toolkit

    class _FakeWorkerAgent:
        def __init__(self, **kwargs):
            captured["worker_kwargs"] = kwargs
            self.toolkit = kwargs["toolkit"]

        async def __call__(self, _msg):
            task_list_json = await self.toolkit.tools["task_list"]()
            listed_tasks.append(task_list_json)
            await self.toolkit.tools["send_message"](
                recipient="facilitator@thread-1",
                text="Worker has enough information to proceed.",
                summary="Worker update",
                idempotency_key="worker-main-1",
            )
            return _FakeWorkerResponse("worker completed via registered tools")

    team = agent_tools_module.TeamConfig(
        team_id="shadow-clone-v2:project-1:thread-1",
        thread_id="thread-1",
        project_id="project-1",
        account_id="account-1",
        facilitator_id="facilitator@thread-1",
        members=[
            AgentIdentity(
                agent_id="market_analyst@thread-1",
                agent_name="market_analyst",
                thread_id="thread-1",
                project_id="project-1",
                account_id="account-1",
                role="research analyst",
            )
        ],
    )

    async def _read_team(**kwargs):
        assert kwargs["account_id"] == "account-1"
        return team

    async def _list_tasks(*, run_id, timeout=None):
        assert run_id == "run-1"
        return [_task()]

    async def _append_event(**kwargs):
        appended_events.append(kwargs)
        return object()

    async def _send_message(**kwargs):
        sent_messages.append(kwargs)
        message = agent_tools_module.MailboxMessage(
            id="1-0",
            run_id=kwargs["run_id"],
            thread_id=kwargs["thread_id"],
            project_id=kwargs["project_id"],
            account_id=kwargs["account_id"],
            sender=kwargs["sender"],
            recipient=kwargs["recipient"],
            kind=kwargs["kind"],
            type=kwargs["message_type"],
            idempotency_key=kwargs["idempotency_key"],
            summary=kwargs["summary"],
            text=kwargs["text"],
            payload=kwargs["payload"],
            created_at=kwargs["created_at"],
        )
        object.__setattr__(message, "_was_created", True)
        return message

    async def _claim_sent_projection(_message):
        return True

    async def _mark_sent_projected(_message):
        return None

    monkeypatch.setattr(
        "agentscope_integration.models.ModelFactory",
        _FakeModelFactory,
    )
    monkeypatch.setattr(
        "agentscope_integration.tools.ToolkitAdapter",
        _FakeToolkitAdapter,
    )
    monkeypatch.setattr(
        "agentscope_integration.agents.worker.WorkerAgent",
        _FakeWorkerAgent,
    )
    monkeypatch.setattr(agent_tools_module.team_store, "read_team", _read_team)
    monkeypatch.setattr(agent_tools_module.task_pool, "list_tasks", _list_tasks)
    monkeypatch.setattr(agent_tools_module.event_log, "append_event", _append_event)
    monkeypatch.setattr(agent_tools_module.mailbox, "send_message", _send_message)
    monkeypatch.setattr(
        agent_tools_module.mailbox, "claim_sent_projection", _claim_sent_projection
    )
    monkeypatch.setattr(
        agent_tools_module.mailbox, "mark_sent_projected", _mark_sent_projected
    )

    executor = AgentScopeShadowCloneV2TaskExecutor()
    result = await executor.execute(
        task=_task(),
        agent=_agent().model_copy(
            update={
                "agent_id": "market_analyst@thread-1",
                "agent_name": "market_analyst",
                "account_id": "account-1",
            }
        ),
        run_context=ShadowCloneV2RunContext(
            thread_id="thread-1",
            project_id="project-1",
            account_id="account-1",
            agent_run_id="run-1",
            model_key="frontend-selected-model",
            tool_sequence_start=20,
        ),
    )

    assert result.output == "worker completed via registered tools"
    assert '"task-1"' in listed_tasks[0].content[0]["text"]
    assert sent_messages[0]["account_id"] == "account-1"
    assert sent_messages[0]["sender"] == "market_analyst"
    assert appended_events[0]["event_type"] == agent_tools_module.EventType.MAILBOX_SENT
    assert result.next_sequence == 21


@pytest.mark.asyncio
async def test_default_worker_real_react_agent_dispatches_v2_tools(
    monkeypatch,
) -> None:
    from types import SimpleNamespace

    from agentscope.tool import Toolkit
    import agentscope_integration.shadow_clone_v2.agent_tools as agent_tools_module

    appended_events = []
    sent_messages = []
    model_tool_schemas = []

    class _FakeFormatter:
        async def format(self, msgs):
            return list(msgs)

    class _DeterministicWorkerModel:
        stream = False

        def __init__(self):
            self.calls = 0

        async def __call__(self, _prompt, tools=None, tool_choice=None):
            self.calls += 1
            model_tool_schemas.append(tools)
            if self.calls == 1:
                return SimpleNamespace(
                    content=[
                        {
                            "type": "tool_use",
                            "id": "call-task-list",
                            "name": "task_list",
                            "input": {},
                        }
                    ],
                    metadata={"model_call": "task_list"},
                )
            if self.calls == 2:
                return SimpleNamespace(
                    content=[
                        {
                            "type": "tool_use",
                            "id": "call-send-message",
                            "name": "send_message",
                            "input": {
                                "recipient": "facilitator@thread-1",
                                "text": "Worker has enough information to proceed.",
                                "summary": "Worker update",
                                "idempotency_key": "worker-main-1",
                            },
                        }
                    ],
                    metadata={"model_call": "send_message"},
                )
            return SimpleNamespace(
                content=[{"type": "text", "text": "Worker complete."}],
                metadata={"model_call": "final"},
            )

    model = _DeterministicWorkerModel()

    class _FakeModelFactory:
        @staticmethod
        def create(model_key=None, **kwargs):
            assert model_key == "frontend-selected-model"
            return model, _FakeFormatter()

    class _FakeToolkitAdapter:
        def __init__(self, **_kwargs):
            self._toolkit = Toolkit()

        def get_toolkit(self):
            return self._toolkit

    team = agent_tools_module.TeamConfig(
        team_id="shadow-clone-v2:project-1:thread-1",
        thread_id="thread-1",
        project_id="project-1",
        account_id="account-1",
        facilitator_id="facilitator@thread-1",
        members=[
            AgentIdentity(
                agent_id="market_analyst@thread-1",
                agent_name="market_analyst",
                thread_id="thread-1",
                project_id="project-1",
                account_id="account-1",
                role="research analyst",
            )
        ],
    )

    async def _read_team(**kwargs):
        assert kwargs["account_id"] == "account-1"
        return team

    async def _list_tasks(*, run_id, timeout=None):
        assert run_id == "run-1"
        return [_task()]

    async def _append_event(**kwargs):
        appended_events.append(kwargs)
        return object()

    async def _send_message(**kwargs):
        sent_messages.append(kwargs)
        message = agent_tools_module.MailboxMessage(
            id="1-0",
            run_id=kwargs["run_id"],
            thread_id=kwargs["thread_id"],
            project_id=kwargs["project_id"],
            account_id=kwargs["account_id"],
            sender=kwargs["sender"],
            recipient=kwargs["recipient"],
            kind=kwargs["kind"],
            type=kwargs["message_type"],
            idempotency_key=kwargs["idempotency_key"],
            summary=kwargs["summary"],
            text=kwargs["text"],
            payload=kwargs["payload"],
            created_at=kwargs["created_at"],
        )
        object.__setattr__(message, "_was_created", True)
        return message

    async def _claim_sent_projection(_message):
        return True

    async def _mark_sent_projected(_message):
        return None

    monkeypatch.setattr(
        "agentscope_integration.models.ModelFactory",
        _FakeModelFactory,
    )
    monkeypatch.setattr(
        "agentscope_integration.tools.ToolkitAdapter",
        _FakeToolkitAdapter,
    )
    monkeypatch.setattr(agent_tools_module.team_store, "read_team", _read_team)
    monkeypatch.setattr(agent_tools_module.task_pool, "list_tasks", _list_tasks)
    monkeypatch.setattr(agent_tools_module.event_log, "append_event", _append_event)
    monkeypatch.setattr(agent_tools_module.mailbox, "send_message", _send_message)
    monkeypatch.setattr(
        agent_tools_module.mailbox, "claim_sent_projection", _claim_sent_projection
    )
    monkeypatch.setattr(
        agent_tools_module.mailbox, "mark_sent_projected", _mark_sent_projected
    )

    executor = AgentScopeShadowCloneV2TaskExecutor()
    result = await executor.execute(
        task=_task(),
        agent=_agent().model_copy(
            update={
                "agent_id": "market_analyst@thread-1",
                "agent_name": "market_analyst",
                "account_id": "account-1",
            }
        ),
        run_context=ShadowCloneV2RunContext(
            thread_id="thread-1",
            project_id="project-1",
            account_id="account-1",
            agent_run_id="run-1",
            model_key="frontend-selected-model",
            tool_sequence_start=20,
        ),
    )

    assert model.calls == 3
    assert any(
        schema["function"]["name"] == "task_list" for schema in model_tool_schemas[0]
    )
    assert result.output == "Worker complete."
    assert sent_messages[0]["account_id"] == "account-1"
    assert sent_messages[0]["sender"] == "market_analyst"
    assert appended_events[0]["event_type"] == agent_tools_module.EventType.MAILBOX_SENT
    assert result.next_sequence == 21


@pytest.mark.asyncio
async def test_default_worker_factory_passes_tool_sequence_start_to_v2_tools(
    monkeypatch,
) -> None:
    captured = {}

    class _FakeModelFactory:
        @staticmethod
        def create(model_key=None, **kwargs):
            return object(), object()

    class _FakeToolkit:
        def register_tool_function(self, _func):
            return None

    class _FakeToolkitAdapter:
        def __init__(self, **kwargs):
            self._toolkit = _FakeToolkit()

        def get_toolkit(self):
            return self._toolkit

    class _FakeWorkerAgent:
        def __init__(self, **kwargs):
            captured["worker_kwargs"] = kwargs

    def _register_agent_tools(_toolkit, *, context, clock=None):
        captured["tool_context"] = context
        return object()

    monkeypatch.setattr("agentscope_integration.models.ModelFactory", _FakeModelFactory)
    monkeypatch.setattr(
        "agentscope_integration.tools.ToolkitAdapter", _FakeToolkitAdapter
    )
    monkeypatch.setattr(
        "agentscope_integration.agents.worker.WorkerAgent", _FakeWorkerAgent
    )
    monkeypatch.setattr(
        "agentscope_integration.shadow_clone_v2.agent_tools.register_agent_tools",
        _register_agent_tools,
    )

    await _default_agentscope_worker_factory(
        model_key="deepseek-v4-pro-max",
        task=_task("task-1"),
        agent=_agent("agent-1"),
        run_context=ShadowCloneV2RunContext(
            thread_id="thread-1",
            project_id="project-1",
            agent_run_id="run-1",
            model_key="deepseek-v4-pro-max",
            tool_sequence_start=42,
        ),
    )

    assert captured["tool_context"].sequence_start == 42
    assert captured["tool_context"].actor_name == "Market Analyst"


@pytest.mark.asyncio
async def test_agentscope_task_executor_raises_sequenced_error_after_tool_use() -> None:
    import agentscope_integration.shadow_clone_v2.task_executor as task_executor_module

    class _FailingWorker:
        def __init__(self) -> None:
            self._shadow_clone_v2_agent_tools = SimpleNamespace(next_sequence=14)

        async def __call__(self, _msg):
            raise RuntimeError("worker failed after tool use")

    async def _worker_factory(**_kwargs):
        return _FailingWorker()

    executor = AgentScopeShadowCloneV2TaskExecutor(worker_factory=_worker_factory)

    with pytest.raises(
        task_executor_module.ShadowCloneV2TaskExecutionError
    ) as exc_info:
        await executor.execute(
            task=_task(),
            agent=_agent(),
            run_context=ShadowCloneV2RunContext(
                thread_id="thread-1",
                project_id="project-1",
                agent_run_id="run-1",
                model_key="deepseek-v4-pro-max",
                tool_sequence_start=13,
            ),
        )

    assert exc_info.value.next_sequence == 14
    assert isinstance(exc_info.value.__cause__, RuntimeError)
