from __future__ import annotations

import pytest

from agentscope_integration.shadow_clone_v2 import main_agent_planner as planner_module
from agentscope_integration.shadow_clone_v2 import agent_tools as agent_tools_module
from agentscope_integration.shadow_clone_v2.agent_tools import (
    ShadowCloneV2AgentTools,
    ShadowCloneV2ToolContext,
)
from agentscope_integration.shadow_clone_v2.main_agent_planner import (
    AgentScopeShadowCloneV2MainAgentSynthesizer,
    AgentScopeShadowCloneV2MainAgentPlanner,
)


def test_main_agent_planning_prompt_forbids_main_agent_coordination_tasks() -> None:
    prompt = planner_module.build_main_agent_planning_prompt(
        user_message="Coordinate a live click task.",
        user_media_refs=[],
        thread_run_id="thread-run-1",
        dynamic_team_hint="Do not create more than 10 subagents.",
    )

    assert "Do not create TaskCreate records for the main agent" in prompt
    assert "facilitator" in prompt
    assert "metadata.agent_name must exactly match one TeamCreate member" in prompt


def test_main_agent_planning_prompt_requires_user_visible_natural_language_after_tools() -> None:
    prompt = planner_module.build_main_agent_planning_prompt(
        user_message="Have two teammates write separate markdown files.",
        user_media_refs=[],
        thread_run_id="thread-run-1",
        dynamic_team_hint="Do not create more than 10 subagents.",
    )

    assert "user-visible natural-language planning update" in prompt
    assert "after the durable TeamCreate, TaskCreate, and SendMessage tool calls" in prompt
    assert "durable state must come only from tools" in prompt
    assert "not system progress" in prompt


@pytest.mark.asyncio
async def test_agent_scope_main_agent_planner_runs_tool_turn_and_reads_tool_state(
    monkeypatch,
) -> None:
    stored_team = None
    stored_tasks = []
    factory_calls = []
    read_team_calls = []

    async def _read_team(**kwargs):
        read_team_calls.append(kwargs)
        return stored_team

    async def _store_team(team, *, ttl_seconds, timeout=None):
        nonlocal stored_team
        stored_team = team

    async def _create_task(task, *, ttl_seconds, timeout=None):
        stored_tasks.append(task)
        return True

    async def _list_tasks(*, run_id, timeout=None):
        assert run_id == "run-1"
        return list(stored_tasks)

    async def _append_event(**_kwargs):
        return object()

    async def _agent_factory(**kwargs):
        factory_calls.append(kwargs)
        assert kwargs["model_key"] == "frontend-selected-model"
        assert isinstance(kwargs["tools"], ShadowCloneV2AgentTools)

        class _Response:
            def get_text_content(self):
                return (
                    "I created the researcher teammate and assigned the initial "
                    "research task."
                )

        async def _agent(_message):
            await kwargs["tools"].team_create(
                members=[{"agent_name": "researcher", "role": "research analyst"}]
            )
            await kwargs["tools"].task_create(
                task_id="research",
                subject="Research",
                description="Research the user's request.",
                metadata={"agent_name": "researcher"},
            )
            return _Response()

        return _agent

    monkeypatch.setattr(planner_module.team_store, "read_team", _read_team)
    monkeypatch.setattr(planner_module.team_store, "store_team", _store_team)
    monkeypatch.setattr(planner_module.task_pool, "create_task", _create_task)
    monkeypatch.setattr(planner_module.task_pool, "list_tasks", _list_tasks)
    monkeypatch.setattr(agent_tools_module.event_log, "append_event", _append_event)

    tools = ShadowCloneV2AgentTools(
        context=ShadowCloneV2ToolContext(
            run_id="run-1",
            thread_id="thread-1",
            project_id="project-1",
            actor_name="facilitator@thread-1",
            sequence_start=2,
            account_id="account-1",
        )
    )
    planner = AgentScopeShadowCloneV2MainAgentPlanner(
        agent_factory=_agent_factory,
    )

    result = await planner.plan(
        user_message="Research competitors.",
        user_media_refs=[],
        tools=tools,
        model_key="frontend-selected-model",
        thread_run_id="thread-run-1",
        created_at="2026-06-03T00:00:00+00:00",
        dynamic_team_hint="Do not create more than 10 subagents.",
        db_client=object(),
        trace=object(),
    )

    assert len(factory_calls) == 1
    assert "Research competitors." in factory_calls[0]["prompt"]
    assert "TeamCreate" in factory_calls[0]["prompt"]
    assert result.team.members[0].agent_name == "researcher"
    assert [task.id for task in result.tasks] == ["research"]
    assert read_team_calls[-1] == {
        "project_id": "project-1",
        "thread_id": "thread-1",
        "account_id": "account-1",
    }
    assert result.next_sequence == tools.next_sequence
    assert result.execute_dynamic_plan is True
    assert result.output == (
        "I created the researcher teammate and assigned the initial research task."
    )


@pytest.mark.asyncio
async def test_agent_scope_main_agent_planner_rejects_unassigned_initial_task(
    monkeypatch,
) -> None:
    stored_team = None
    stored_tasks = []

    async def _read_team(**_kwargs):
        return stored_team

    async def _store_team(team, *, ttl_seconds, timeout=None):
        nonlocal stored_team
        stored_team = team

    async def _create_task(task, *, ttl_seconds, timeout=None):
        stored_tasks.append(task)
        return True

    async def _list_tasks(*, run_id, timeout=None):
        return list(stored_tasks)

    async def _append_event(**_kwargs):
        return object()

    async def _agent_factory(**kwargs):
        async def _agent(_message):
            await kwargs["tools"].team_create(
                members=[{"agent_name": "researcher", "role": "research analyst"}]
            )
            await kwargs["tools"].task_create(
                task_id="research",
                subject="Research",
                description="Research the user's request.",
            )

        return _agent

    monkeypatch.setattr(planner_module.team_store, "read_team", _read_team)
    monkeypatch.setattr(planner_module.team_store, "store_team", _store_team)
    monkeypatch.setattr(planner_module.task_pool, "create_task", _create_task)
    monkeypatch.setattr(planner_module.task_pool, "list_tasks", _list_tasks)
    monkeypatch.setattr(agent_tools_module.event_log, "append_event", _append_event)

    tools = ShadowCloneV2AgentTools(
        context=ShadowCloneV2ToolContext(
            run_id="run-1",
            thread_id="thread-1",
            project_id="project-1",
            actor_name="facilitator@thread-1",
            sequence_start=2,
        )
    )
    planner = AgentScopeShadowCloneV2MainAgentPlanner(
        agent_factory=_agent_factory,
    )

    with pytest.raises(RuntimeError, match="metadata.agent_name"):
        await planner.plan(
            user_message="Research competitors.",
            user_media_refs=[],
            tools=tools,
            model_key="frontend-selected-model",
            thread_run_id="thread-run-1",
            created_at="2026-06-03T00:00:00+00:00",
            dynamic_team_hint="Do not create more than 10 subagents.",
        )


@pytest.mark.asyncio
async def test_default_main_agent_factory_registers_v2_tools_and_uses_model_key(
    monkeypatch,
) -> None:
    captured = {}
    registered = {}

    class _Toolkit:
        def register_tool_function(self, func):
            registered[func.__name__] = func

    class _ModelFactory:
        @staticmethod
        def create(*, model_key, trace=None):
            captured["model_key"] = model_key
            captured["trace"] = trace
            return "model", "formatter"

    class _Memory:
        pass

    class _Agent:
        def __init__(self, **kwargs):
            captured["agent_kwargs"] = kwargs

    tools = ShadowCloneV2AgentTools(
        context=ShadowCloneV2ToolContext(
            run_id="run-1",
            thread_id="thread-1",
            project_id="project-1",
            actor_name="facilitator@thread-1",
            sequence_start=2,
        )
    )

    agent = await planner_module._default_agentscope_main_agent_factory(
        model_key="frontend-selected-model",
        tools=tools,
        tool_context=tools.context,
        trace="trace",
        toolkit_factory=_Toolkit,
        model_factory=_ModelFactory,
        memory_factory=_Memory,
        agent_class=_Agent,
    )

    assert isinstance(agent, _Agent)
    assert captured["model_key"] == "frontend-selected-model"
    assert captured["agent_kwargs"]["model"] == "model"
    assert captured["agent_kwargs"]["formatter"] == "formatter"
    assert captured["agent_kwargs"]["parallel_tool_calls"] is False
    assert "do not copy prior delivery marker strings" in captured["agent_kwargs"]["sys_prompt"]
    assert getattr(agent, "_shadow_clone_v2_agent_tools") is tools
    assert set(registered) == {
        "team_create",
        "team_shutdown",
        "task_create",
        "task_get",
        "task_update",
        "task_list",
        "send_message",
        "message_read",
        "message_ack",
        "message_dead_letter",
    }


def test_main_agent_planning_prompt_documents_broadcast_send_message_contract() -> None:
    prompt = planner_module.build_main_agent_planning_prompt(
        user_message="Broadcast a pivot to every teammate.",
        user_media_refs=[],
        thread_run_id="thread-run-1",
        dynamic_team_hint="Use at most 10 subagents.",
    )

    assert 'recipient="*"' in prompt
    assert 'message_type="broadcast"' in prompt
    assert "broadcast" in prompt.lower()


def test_main_agent_planning_prompt_forbids_copying_prior_delivery_markers_into_new_task_descriptions() -> None:
    prompt = planner_module.build_main_agent_planning_prompt(
        user_message=(
            "Reuse teammate-1 from idle and apply feedback. The previous delivery "
            "had marker IDLE_DRAFT_V1_12345, but the new task result must contain "
            "WAKE_REUSED_TEAMMATE_OK, IDLE_FEEDBACK_REQUEST_12345, and "
            "IDLE_FEEDBACK_APPLIED_12345."
        ),
        user_media_refs=[],
        thread_run_id="thread-run-1",
        dynamic_team_hint="Use the existing idle teammate when appropriate.",
    )

    assert "TaskCreate descriptions" in prompt
    assert "do not copy prior delivery marker strings" in prompt
    assert "reference prior work by artifact path" in prompt
    assert "new required output markers" in prompt


@pytest.mark.asyncio
async def test_default_main_agent_factory_tools_are_callable_during_agent_turn(
    monkeypatch,
) -> None:
    stored_team = None
    stored_tasks = []
    appended_events = []
    sent_messages = []
    registered = {}

    async def _read_team(**_kwargs):
        return stored_team

    async def _store_team(team, *, ttl_seconds, timeout=None):
        nonlocal stored_team
        stored_team = team

    async def _create_task(task, *, ttl_seconds, timeout=None):
        stored_tasks.append(task)
        return True

    async def _list_tasks(*, run_id, timeout=None):
        assert run_id == "run-1"
        return list(stored_tasks)

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

    class _Toolkit:
        def __init__(self):
            self.tools = {}

        def register_tool_function(self, func):
            self.tools[func.__name__] = func
            registered[func.__name__] = func

    class _ModelFactory:
        @staticmethod
        def create(*, model_key, trace=None):
            assert model_key == "frontend-selected-model"
            return "model", "formatter"

    class _Memory:
        pass

    class _Agent:
        def __init__(self, **kwargs):
            self.toolkit = kwargs["toolkit"]

        async def __call__(self, _message):
            await self.toolkit.tools["team_create"](
                members=[{"agent_name": "researcher", "role": "research analyst"}]
            )
            await self.toolkit.tools["task_create"](
                task_id="research",
                subject="Research",
                description="Research the user's request.",
                metadata={"agent_name": "researcher"},
            )
            await self.toolkit.tools["send_message"](
                recipient="researcher",
                text="Please research this request.",
                summary="Research request",
                idempotency_key="main-researcher-1",
            )
            return object()

    async def _agent_factory(**kwargs):
        return await planner_module._default_agentscope_main_agent_factory(
            **kwargs,
            toolkit_factory=_Toolkit,
            model_factory=_ModelFactory,
            memory_factory=_Memory,
            agent_class=_Agent,
        )

    monkeypatch.setattr(planner_module.team_store, "read_team", _read_team)
    monkeypatch.setattr(planner_module.team_store, "store_team", _store_team)
    monkeypatch.setattr(planner_module.task_pool, "create_task", _create_task)
    monkeypatch.setattr(planner_module.task_pool, "list_tasks", _list_tasks)
    monkeypatch.setattr(agent_tools_module.event_log, "append_event", _append_event)
    monkeypatch.setattr(agent_tools_module.mailbox, "send_message", _send_message)
    monkeypatch.setattr(
        agent_tools_module.mailbox, "claim_sent_projection", _claim_sent_projection
    )
    monkeypatch.setattr(
        agent_tools_module.mailbox, "mark_sent_projected", _mark_sent_projected
    )

    tools = ShadowCloneV2AgentTools(
        context=ShadowCloneV2ToolContext(
            run_id="run-1",
            thread_id="thread-1",
            project_id="project-1",
            account_id="account-1",
            actor_name="facilitator@thread-1",
            sequence_start=2,
        )
    )
    planner = AgentScopeShadowCloneV2MainAgentPlanner(agent_factory=_agent_factory)

    result = await planner.plan(
        user_message="Research competitors.",
        user_media_refs=[],
        tools=tools,
        model_key="frontend-selected-model",
        thread_run_id="thread-run-1",
        created_at="2026-06-03T00:00:00+00:00",
        dynamic_team_hint="Do not create more than 10 subagents.",
    )

    assert {"team_create", "task_create", "send_message"}.issubset(registered)
    assert result.team.members[0].agent_name == "researcher"
    assert [task.id for task in result.tasks] == ["research"]
    assert sent_messages[0]["account_id"] == "account-1"
    assert [event["event_type"] for event in appended_events] == [
        agent_tools_module.EventType.TEAM_CREATED,
        agent_tools_module.EventType.AGENT_SPAWNED,
        agent_tools_module.EventType.TASK_CREATED,
        agent_tools_module.EventType.MAILBOX_SENT,
    ]


@pytest.mark.asyncio
async def test_default_main_agent_real_react_agent_dispatches_v2_tools(
    monkeypatch,
) -> None:
    from types import SimpleNamespace

    stored_team = None
    stored_tasks = []
    appended_events = []
    sent_messages = []
    model_tool_schemas = []

    class _FakeFormatter:
        async def format(self, msgs):
            return list(msgs)

    class _DeterministicToolModel:
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
                            "id": "call-team-create",
                            "name": "team_create",
                            "input": {
                                "members": [
                                    {
                                        "agent_name": "researcher",
                                        "role": "research analyst",
                                    }
                                ]
                            },
                        }
                    ],
                    metadata={"model_call": "team_create"},
                )
            if self.calls == 2:
                return SimpleNamespace(
                    content=[
                        {
                            "type": "tool_use",
                            "id": "call-task-create",
                            "name": "task_create",
                            "input": {
                                "task_id": "research",
                                "subject": "Research",
                                "description": "Research the user's request.",
                                "metadata": {"agent_name": "researcher"},
                            },
                        }
                    ],
                    metadata={"model_call": "task_create"},
                )
            if self.calls == 3:
                return SimpleNamespace(
                    content=[
                        {
                            "type": "tool_use",
                            "id": "call-send-message",
                            "name": "send_message",
                            "input": {
                                "recipient": "researcher",
                                "text": "Please research this request.",
                                "summary": "Research request",
                                "idempotency_key": "main-researcher-1",
                            },
                        }
                    ],
                    metadata={"model_call": "send_message"},
                )
            return SimpleNamespace(
                content=[{"type": "text", "text": "Planning complete."}],
                metadata={"model_call": "final"},
            )

    model = _DeterministicToolModel()

    class _ModelFactory:
        @staticmethod
        def create(*, model_key, trace=None):
            assert model_key == "frontend-selected-model"
            return model, _FakeFormatter()

    async def _read_team(**_kwargs):
        return stored_team

    async def _store_team(team, *, ttl_seconds, timeout=None):
        nonlocal stored_team
        stored_team = team

    async def _create_task(task, *, ttl_seconds, timeout=None):
        stored_tasks.append(task)
        return True

    async def _list_tasks(*, run_id, timeout=None):
        assert run_id == "run-1"
        return list(stored_tasks)

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

    monkeypatch.setattr(planner_module.team_store, "read_team", _read_team)
    monkeypatch.setattr(planner_module.team_store, "store_team", _store_team)
    monkeypatch.setattr(planner_module.task_pool, "create_task", _create_task)
    monkeypatch.setattr(planner_module.task_pool, "list_tasks", _list_tasks)
    monkeypatch.setattr(agent_tools_module.event_log, "append_event", _append_event)
    monkeypatch.setattr(agent_tools_module.mailbox, "send_message", _send_message)
    monkeypatch.setattr(
        agent_tools_module.mailbox, "claim_sent_projection", _claim_sent_projection
    )
    monkeypatch.setattr(
        agent_tools_module.mailbox, "mark_sent_projected", _mark_sent_projected
    )

    async def _agent_factory(**kwargs):
        return await planner_module._default_agentscope_main_agent_factory(
            **kwargs,
            model_factory=_ModelFactory,
        )

    tools = ShadowCloneV2AgentTools(
        context=ShadowCloneV2ToolContext(
            run_id="run-1",
            thread_id="thread-1",
            project_id="project-1",
            account_id="account-1",
            actor_name="facilitator@thread-1",
            sequence_start=2,
        )
    )
    planner = AgentScopeShadowCloneV2MainAgentPlanner(agent_factory=_agent_factory)

    result = await planner.plan(
        user_message="Research competitors.",
        user_media_refs=[],
        tools=tools,
        model_key="frontend-selected-model",
        thread_run_id="thread-run-1",
        created_at="2026-06-03T00:00:00+00:00",
        dynamic_team_hint="Do not create more than 10 subagents.",
    )

    assert model.calls == 4
    assert any(
        schema["function"]["name"] == "team_create" for schema in model_tool_schemas[0]
    )
    assert result.team.members[0].agent_name == "researcher"
    assert [task.id for task in result.tasks] == ["research"]
    assert sent_messages[0]["account_id"] == "account-1"
    assert [event["event_type"] for event in appended_events] == [
        agent_tools_module.EventType.TEAM_CREATED,
        agent_tools_module.EventType.AGENT_SPAWNED,
        agent_tools_module.EventType.TASK_CREATED,
        agent_tools_module.EventType.MAILBOX_SENT,
    ]


@pytest.mark.asyncio
async def test_default_main_agent_real_react_agent_repeated_turn_preserves_memory_and_state(
    monkeypatch,
) -> None:
    from types import SimpleNamespace

    from agentscope.message import Msg

    stored_team = None
    stored_tasks = []
    appended_events = []
    sent_messages = []

    class _FakeFormatter:
        async def format(self, msgs):
            return list(msgs)

    class _RepeatedTurnModel:
        stream = False

        def __init__(self):
            self.calls = 0
            self.prompts = []

        async def __call__(self, prompt, tools=None, tool_choice=None):
            self.calls += 1
            self.prompts.append(list(prompt))
            if self.calls == 1:
                return SimpleNamespace(
                    content=[
                        {
                            "type": "tool_use",
                            "id": "first-team-create",
                            "name": "team_create",
                            "input": {
                                "members": [
                                    {
                                        "agent_name": "researcher",
                                        "role": "research analyst",
                                    }
                                ]
                            },
                        }
                    ],
                    metadata={"turn": "first", "step": "team_create"},
                )
            if self.calls == 2:
                return SimpleNamespace(
                    content=[
                        {
                            "type": "tool_use",
                            "id": "first-task-create",
                            "name": "task_create",
                            "input": {
                                "task_id": "research",
                                "subject": "Research",
                                "description": "Research the user's request.",
                                "metadata": {"agent_name": "researcher"},
                            },
                        }
                    ],
                    metadata={"turn": "first", "step": "task_create"},
                )
            if self.calls == 3:
                return SimpleNamespace(
                    content=[{"type": "text", "text": "First turn complete."}],
                    metadata={"turn": "first", "step": "final"},
                )
            if self.calls == 4:
                return SimpleNamespace(
                    content=[
                        {
                            "type": "tool_use",
                            "id": "second-task-list",
                            "name": "task_list",
                            "input": {},
                        }
                    ],
                    metadata={"turn": "second", "step": "task_list"},
                )
            if self.calls == 5:
                return SimpleNamespace(
                    content=[
                        {
                            "type": "tool_use",
                            "id": "second-send-message",
                            "name": "send_message",
                            "input": {
                                "recipient": "researcher",
                                "text": "Continue with the existing research task.",
                                "summary": "Continue research",
                                "idempotency_key": "main-researcher-second-turn",
                            },
                        }
                    ],
                    metadata={"turn": "second", "step": "send_message"},
                )
            return SimpleNamespace(
                content=[{"type": "text", "text": "Second turn complete."}],
                metadata={"turn": "second", "step": "final"},
            )

    model = _RepeatedTurnModel()

    class _ModelFactory:
        @staticmethod
        def create(*, model_key, trace=None):
            assert model_key == "frontend-selected-model"
            return model, _FakeFormatter()

    async def _read_team(**kwargs):
        assert kwargs["account_id"] == "account-1"
        return stored_team

    async def _store_team(team, *, ttl_seconds, timeout=None):
        nonlocal stored_team
        stored_team = team

    async def _create_task(task, *, ttl_seconds, timeout=None):
        stored_tasks.append(task)
        return True

    async def _list_tasks(*, run_id, timeout=None):
        assert run_id == "run-1"
        return list(stored_tasks)

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

    monkeypatch.setattr(planner_module.team_store, "read_team", _read_team)
    monkeypatch.setattr(planner_module.team_store, "store_team", _store_team)
    monkeypatch.setattr(planner_module.task_pool, "create_task", _create_task)
    monkeypatch.setattr(planner_module.task_pool, "list_tasks", _list_tasks)
    monkeypatch.setattr(agent_tools_module.event_log, "append_event", _append_event)
    monkeypatch.setattr(agent_tools_module.mailbox, "send_message", _send_message)
    monkeypatch.setattr(
        agent_tools_module.mailbox, "claim_sent_projection", _claim_sent_projection
    )
    monkeypatch.setattr(
        agent_tools_module.mailbox, "mark_sent_projected", _mark_sent_projected
    )

    tools = ShadowCloneV2AgentTools(
        context=ShadowCloneV2ToolContext(
            run_id="run-1",
            thread_id="thread-1",
            project_id="project-1",
            account_id="account-1",
            actor_name="facilitator@thread-1",
            sequence_start=2,
        )
    )
    agent = await planner_module._default_agentscope_main_agent_factory(
        model_key="frontend-selected-model",
        tools=tools,
        tool_context=tools.context,
        model_factory=_ModelFactory,
    )

    first = await agent(
        Msg(
            name="ShadowCloneV2MainAgent",
            content="First turn: create the team and task.",
            role="user",
        )
    )
    second = await agent(
        Msg(
            name="ShadowCloneV2MainAgent",
            content="Second turn: continue with the existing teammate.",
            role="user",
        )
    )

    assert first.get_text_content() == "First turn complete."
    assert second.get_text_content() == "Second turn complete."
    assert model.calls == 6
    second_turn_first_prompt = model.prompts[3]
    assert any(
        block.get("type") == "tool_result" and block.get("name") == "team_create"
        for msg in second_turn_first_prompt
        for block in (msg.content if isinstance(msg.content, list) else [])
        if isinstance(block, dict)
    )
    assert stored_team.members[0].agent_id == "researcher@thread-1"
    assert sent_messages[0]["recipient"] == "researcher"
    assert sent_messages[0]["account_id"] == "account-1"
    assert [event["event_type"] for event in appended_events] == [
        agent_tools_module.EventType.TEAM_CREATED,
        agent_tools_module.EventType.AGENT_SPAWNED,
        agent_tools_module.EventType.TASK_CREATED,
        agent_tools_module.EventType.MAILBOX_SENT,
    ]


@pytest.mark.asyncio
async def test_main_agent_synthesizer_runs_agent_turn_with_frontend_model() -> None:
    factory_calls = []
    received_messages = []

    async def _agent_factory(**kwargs):
        factory_calls.append(kwargs)

        class _Response:
            content = "main synthesized answer"

        async def _agent(message):
            received_messages.append(message)
            return _Response()

        return _agent

    synthesizer = AgentScopeShadowCloneV2MainAgentSynthesizer(
        agent_factory=_agent_factory,
    )

    result = await synthesizer.synthesize(
        user_message="Summarize this memo.",
        tasks=[
            {
                "id": "memo",
                "subject": "Summary",
                "description": "Summarize memo.",
                "status": "completed",
                "owner_agent": "summarizer",
                "result_ref": "artifact://memo",
                "metadata": {"progress": "done"},
            }
        ],
        team={
            "team_id": "team-1",
            "members": [
                {
                    "agent_name": "summarizer",
                    "role": "summarizer",
                    "status": "idle",
                }
            ],
        },
        messages=[
            {
                "message_id": "msg-1",
                "sender": "reviewer",
                "recipient": "summarizer",
                "summary": "QA passed",
                "status": "acked",
            }
        ],
        worker_outputs=[
            {
                "agent_name": "summarizer",
                "task_id": "memo",
                "task_subject": "Summary",
                "task_description": "Summarize memo.",
                "output": "worker summary",
            }
        ],
        model_key="frontend-selected-model",
        thread_run_id="thread-run-1",
        db_client=object(),
        trace="trace",
        sequence_start=42,
    )

    assert result.output == "main synthesized answer"
    assert result.next_sequence == 42
    assert len(factory_calls) == 1
    assert factory_calls[0]["model_key"] == "frontend-selected-model"
    assert "Summarize this memo." in factory_calls[0]["prompt"]
    assert "artifact://memo" in factory_calls[0]["prompt"]
    assert "QA passed" in factory_calls[0]["prompt"]
    assert "summarizer" in factory_calls[0]["prompt"]
    assert "worker summary" in factory_calls[0]["prompt"]
    assert len(received_messages) == 1


@pytest.mark.asyncio
async def test_default_main_synthesis_factory_uses_model_key(monkeypatch) -> None:
    captured = {}

    class _ModelFactory:
        @staticmethod
        def create(*, model_key, trace=None):
            captured["model_key"] = model_key
            captured["trace"] = trace
            return "model", "formatter"

    class _Memory:
        pass

    class _Agent:
        def __init__(self, **kwargs):
            captured["agent_kwargs"] = kwargs

    agent = await planner_module._default_agentscope_main_synthesis_agent_factory(
        model_key="frontend-selected-model",
        trace="trace",
        model_factory=_ModelFactory,
        memory_factory=_Memory,
        agent_class=_Agent,
    )

    assert isinstance(agent, _Agent)
    assert captured["model_key"] == "frontend-selected-model"
    assert captured["agent_kwargs"]["model"] == "model"
    assert captured["agent_kwargs"]["formatter"] == "formatter"
    assert captured["agent_kwargs"]["parallel_tool_calls"] is False


@pytest.mark.asyncio
async def test_main_agent_synthesizer_runs_from_task_state_without_worker_outputs() -> (
    None
):
    factory_calls = []
    received_messages = []

    async def _agent_factory(**kwargs):
        factory_calls.append(kwargs)

        class _Response:
            content = "answer from task state"

        async def _agent(message):
            received_messages.append(message)
            return _Response()

        return _agent

    synthesizer = AgentScopeShadowCloneV2MainAgentSynthesizer(
        agent_factory=_agent_factory,
    )

    result = await synthesizer.synthesize(
        user_message="Summarize this memo.",
        tasks=[
            {
                "id": "memo",
                "subject": "Summary",
                "status": "completed",
                "result_ref": "artifact://completed-memo",
            }
        ],
        team={},
        messages=[],
        worker_outputs=[],
        model_key="frontend-selected-model",
        thread_run_id="thread-run-1",
        sequence_start=7,
    )

    assert result.output == "answer from task state"
    assert result.next_sequence == 7
    assert len(factory_calls) == 1
    assert "artifact://completed-memo" in factory_calls[0]["prompt"]
    assert len(received_messages) == 1
