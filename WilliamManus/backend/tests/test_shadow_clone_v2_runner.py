from __future__ import annotations

import asyncio
import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from agentscope_integration.shadow_clone.constants import ShadowCloneMode
from agentscope_integration.shadow_clone_v2 import runner as runner_module
from agentscope_integration.shadow_clone_v2 import task_executor as task_executor_module
from agentscope_integration.shadow_clone_v2.agent_loop import AgentTurnResult
from agentscope_integration.shadow_clone_v2.models import (
    AgentLifecycleStatus,
    EventRecord,
    EventType,
    MailboxMessage,
    MessageKind,
    MessageType,
    Task,
    TaskStatus,
)
from agentscope_integration.shadow_clone_v2.models import AgentIdentity, TeamConfig
from agentscope_integration.shadow_clone_v2.runner import ShadowCloneV2Runner
from agentscope_integration.shadow_clone_v2.task_executor import (
    AgentScopeShadowCloneV2TaskExecutor,
    ShadowCloneV2TaskExecutionResult,
)


@pytest.fixture(autouse=True)
def _no_existing_v2_team(monkeypatch):
    async def _read_team(**_kwargs):
        return None

    monkeypatch.setattr(runner_module.team_store, "read_team", _read_team)


@pytest.fixture(autouse=True)
def _no_expired_task_lease_recovery(monkeypatch):
    async def _project_expired_task_lease_recovery_events(**kwargs):
        return kwargs["sequence_start"]

    monkeypatch.setattr(
        runner_module.agent_loop,
        "project_expired_task_lease_recovery_events",
        _project_expired_task_lease_recovery_events,
    )


@pytest.fixture(autouse=True)
def _no_refreshed_v2_tasks(monkeypatch):
    async def _list_tasks(*_args, **_kwargs):
        return []

    monkeypatch.setattr(runner_module.task_pool, "list_tasks", _list_tasks)


class _FixedMainAgentSynthesizer:
    async def synthesize(self, **kwargs):
        return runner_module.MainAgentSynthesisResult(
            output="main synthesized answer",
            next_sequence=kwargs["sequence_start"],
        )


async def _collect_runner_events(
    runner: ShadowCloneV2Runner,
    *,
    dynamic_subtasks: list[dict[str, object]],
) -> list[dict[str, object]]:
    return [
        event
        async for event in runner.run(
            user_message="Run these independent tasks.",
            thread_run_id="thread-run-1",
            dynamic_subtasks=dynamic_subtasks,
            execute_dynamic_plan=True,
        )
    ]


@pytest.mark.asyncio
async def test_runner_run_appends_started_event_and_yields_v2_status(
    monkeypatch,
) -> None:
    appended = []

    async def _append_event(**kwargs):
        appended.append(kwargs)
        return object()

    monkeypatch.setattr(runner_module.event_log, "append_event", _append_event)

    runner = ShadowCloneV2Runner(
        thread_id="thread-1",
        project_id="project-1",
        agent_run_id="run-1",
        model_key="model-1",
        db_client=object(),
        mode=ShadowCloneMode.V2,
    )

    stream = runner.run(
        user_message="Summarize the uploaded document.",
        thread_run_id="thread-run-1",
    )
    events = [await anext(stream)]
    await stream.aclose()

    assert appended[0]["event_type"] == EventType.RUN_STARTED
    assert appended[0]["run_id"] == "run-1"
    assert appended[0]["thread_id"] == "thread-1"
    assert (
        "Do not create more than 10 subagents"
        in appended[0]["payload"]["dynamic_team_hint"]
    )
    assert events[0]["type"] == "shadow_clone_v2_started"
    assert events[0]["status"] == "running"
    assert events[0]["shadow_clone_mode"] == "v2"
    assert events[0]["ui_phase"] == "planning"
    assert events[0]["activity_owner"] == "shadow_clone"
    assert events[0]["phase_reason"] == "shadow_clone_v2_started"


@pytest.mark.asyncio
async def test_runner_persists_dynamic_team_plan_and_emits_spawn_task_events(
    monkeypatch,
) -> None:
    appended = []
    stored_tasks = []
    stored_team = []

    async def _append_event(**kwargs):
        appended.append(kwargs)
        return object()

    async def _store_task(task, *, ttl_seconds, timeout=None):
        stored_tasks.append((task, ttl_seconds, timeout))

    async def _store_team(team, *, ttl_seconds, timeout=None):
        stored_team.append((team, ttl_seconds, timeout))

    monkeypatch.setattr(runner_module.event_log, "append_event", _append_event)
    monkeypatch.setattr(runner_module.task_pool, "store_task", _store_task)
    monkeypatch.setattr(runner_module.team_store, "store_team", _store_team)

    runner = ShadowCloneV2Runner(
        thread_id="thread-1",
        project_id="project-1",
        agent_run_id="run-1",
        model_key="model-1",
        db_client=object(),
        mode=ShadowCloneMode.V2,
    )

    events = [
        event
        async for event in runner.run(
            user_message="Summarize and QA this office memo.",
            thread_run_id="thread-run-1",
            dynamic_subtasks=[
                {
                    "id": "summarize",
                    "role": "summarizer",
                    "task_description": "Summarize memo.",
                },
                {
                    "id": "qa",
                    "role": "reviewer",
                    "task_description": "Check summary quality.",
                },
            ],
        )
    ]

    assert len(stored_team) == 1
    assert [member.agent_name for member in stored_team[0][0].members] == [
        "summarizer",
        "reviewer",
    ]
    assert [task.id for task, _ttl, _timeout in stored_tasks] == ["summarize", "qa"]
    event_types = [call["event_type"] for call in appended]
    assert event_types.count(EventType.AGENT_SPAWNED) == 2
    assert event_types.count(EventType.TASK_CREATED) == 2
    assert events[-1]["type"] == "shadow_clone_v2_plan_created"
    assert events[-1]["subagent_count"] == 2
    assert events[-1]["task_count"] == 2
    assert events[-1]["subtasks"] == [
        {
            "id": "summarize",
            "role": "summarizer",
            "task_description": "Summarize memo.",
            "status": "pending",
            "agent_name": "summarizer",
        },
        {
            "id": "qa",
            "role": "reviewer",
            "task_description": "Check summary quality.",
            "status": "pending",
            "agent_name": "reviewer",
        },
    ]
    assert events[-1]["dependencies"] == []


@pytest.mark.asyncio
async def test_runner_executes_explicit_legacy_dynamic_subtasks_fixture(
    monkeypatch,
) -> None:
    appended = []
    stored_tasks = []
    stored_team = []
    agent_turns = []

    async def _append_event(**kwargs):
        appended.append(kwargs)
        return object()

    async def _store_task(task, *, ttl_seconds, timeout=None):
        stored_tasks.append((task, ttl_seconds, timeout))

    async def _store_team(team, *, ttl_seconds, timeout=None):
        stored_team.append((team, ttl_seconds, timeout))

    async def _run_agent_turn(**kwargs):
        agent_turns.append(kwargs)
        return object()

    monkeypatch.setattr(runner_module.event_log, "append_event", _append_event)
    monkeypatch.setattr(runner_module.task_pool, "store_task", _store_task)
    monkeypatch.setattr(runner_module.team_store, "store_team", _store_team)
    monkeypatch.setattr(runner_module.agent_loop, "run_agent_turn", _run_agent_turn)

    runner = ShadowCloneV2Runner(
        thread_id="thread-1",
        project_id="project-1",
        agent_run_id="run-1",
        model_key="deepseek-v4-pro-max",
        db_client=object(),
        mode=ShadowCloneMode.V2,
        main_agent_synthesizer=_FixedMainAgentSynthesizer(),
    )

    events = [
        event
        async for event in runner.run(
            user_message="Summarize and QA this office memo.",
            thread_run_id="thread-run-1",
            dynamic_subtasks=[
                {
                    "id": "memo",
                    "role": "document analyst",
                    "task_description": "Summarize the memo and extract action items.",
                },
                {
                    "id": "qa",
                    "role": "quality reviewer",
                    "task_description": "Review the memo summary for completeness.",
                },
            ],
            execute_dynamic_plan=True,
        )
    ]

    assert [member.agent_name for member in stored_team[0][0].members] == [
        "document-analyst",
        "quality-reviewer",
    ]
    assert [task.id for task, _ttl, _timeout in stored_tasks] == ["memo", "qa"]
    assert [turn["task"].id for turn in agent_turns] == ["memo", "qa"]
    assert events[-1]["type"] == "shadow_clone_v2_execution_completed"
    assert EventType.TASK_CREATED in [call["event_type"] for call in appended]
    assert appended[-1]["event_type"] == EventType.RUN_COMPLETED


@pytest.mark.asyncio
async def test_runner_sweeps_expired_task_leases_before_scheduling_turns(
    monkeypatch,
) -> None:
    appended = []
    stored_tasks = []
    stored_team = []
    agent_turns = []
    sweep_calls = []

    async def _append_event(**kwargs):
        appended.append(kwargs)
        return object()

    async def _store_task(task, *, ttl_seconds, timeout=None):
        stored_tasks.append((task, ttl_seconds, timeout))

    async def _store_team(team, *, ttl_seconds, timeout=None):
        stored_team.append((team, ttl_seconds, timeout))

    async def _project_expired_task_lease_recovery_events(**kwargs):
        sweep_calls.append(kwargs)
        return kwargs["sequence_start"] + 1

    async def _list_tasks(**kwargs):
        assert kwargs["run_id"] == "run-1"
        original_task = stored_tasks[0][0]
        return [
            original_task.model_copy(
                update={
                    "description": "Recovered living task state.",
                    "metadata": {
                        **original_task.metadata,
                        "recovered": True,
                    },
                }
            )
        ]

    async def _run_agent_turn(**kwargs):
        agent_turns.append(kwargs)
        return AgentTurnResult(
            agent=kwargs["agent"].model_copy(
                update={"status": AgentLifecycleStatus.IDLE}
            ),
            task=kwargs["task"],
            output="done",
            next_sequence=kwargs["sequence_start"] + 3,
        )

    monkeypatch.setattr(runner_module.event_log, "append_event", _append_event)
    monkeypatch.setattr(runner_module.task_pool, "store_task", _store_task)
    monkeypatch.setattr(runner_module.team_store, "store_team", _store_team)
    monkeypatch.setattr(
        runner_module.agent_loop,
        "project_expired_task_lease_recovery_events",
        _project_expired_task_lease_recovery_events,
    )
    monkeypatch.setattr(runner_module.task_pool, "list_tasks", _list_tasks)
    monkeypatch.setattr(runner_module.agent_loop, "run_agent_turn", _run_agent_turn)

    runner = ShadowCloneV2Runner(
        thread_id="thread-1",
        project_id="project-1",
        agent_run_id="run-1",
        model_key="deepseek-v4-pro-max",
        db_client=object(),
        mode=ShadowCloneMode.V2,
    )

    _events = [
        event
        async for event in runner.run(
            user_message="Summarize this memo.",
            thread_run_id="thread-run-1",
            dynamic_subtasks=[
                {
                    "id": "memo",
                    "role": "document analyst",
                    "task_description": "Summarize memo.",
                },
            ],
            execute_dynamic_plan=True,
        )
    ]

    assert len(sweep_calls) == 1
    assert sweep_calls[0]["run_id"] == "run-1"
    assert sweep_calls[0]["thread_id"] == "thread-1"
    assert sweep_calls[0]["project_id"] == "project-1"
    assert (
        sweep_calls[0]["ttl_seconds"] == runner_module.SHADOW_CLONE_V2_STATE_TTL_SECONDS
    )
    assert agent_turns[0]["sequence_start"] == sweep_calls[0]["sequence_start"] + 1
    assert agent_turns[0]["task"].description == "Recovered living task state."
    assert agent_turns[0]["task"].metadata["recovered"] is True


@pytest.mark.asyncio
async def test_runner_does_not_schedule_failed_or_blocked_tasks_after_recovery(
    monkeypatch,
) -> None:
    stored_tasks = []
    agent_turns = []
    sweep_calls = []

    async def _append_event(**_kwargs):
        return object()

    async def _store_task(task, *, ttl_seconds, timeout=None):
        stored_tasks.append(task)

    async def _noop_store_team(*_args, **_kwargs):
        return None

    async def _project_expired_task_lease_recovery_events(**kwargs):
        sweep_calls.append(kwargs)
        return kwargs["sequence_start"] + 2

    async def _list_tasks(**_kwargs):
        return [
            stored_tasks[0].model_copy(update={"status": TaskStatus.FAILED}),
            stored_tasks[1].model_copy(update={"status": TaskStatus.BLOCKED}),
        ]

    async def _run_agent_turn(**kwargs):
        agent_turns.append(kwargs)
        return object()

    monkeypatch.setattr(runner_module.event_log, "append_event", _append_event)
    monkeypatch.setattr(runner_module.task_pool, "store_task", _store_task)
    monkeypatch.setattr(runner_module.team_store, "store_team", _noop_store_team)
    monkeypatch.setattr(
        runner_module.agent_loop,
        "project_expired_task_lease_recovery_events",
        _project_expired_task_lease_recovery_events,
    )
    monkeypatch.setattr(runner_module.task_pool, "list_tasks", _list_tasks)
    monkeypatch.setattr(runner_module.agent_loop, "run_agent_turn", _run_agent_turn)

    runner = ShadowCloneV2Runner(
        thread_id="thread-1",
        project_id="project-1",
        agent_run_id="run-1",
        model_key="deepseek-v4-pro-max",
        db_client=object(),
        mode=ShadowCloneMode.V2,
    )

    events = [
        event
        async for event in runner.run(
            user_message="Summarize and QA this memo.",
            thread_run_id="thread-run-1",
            dynamic_subtasks=[
                {
                    "id": "memo",
                    "role": "document analyst",
                    "task_description": "Summarize memo.",
                },
                {
                    "id": "qa",
                    "role": "quality reviewer",
                    "task_description": "Review memo.",
                },
            ],
            execute_dynamic_plan=True,
        )
    ]

    assert len(sweep_calls) == 1
    assert agent_turns == []
    assert events[-1]["type"] == "shadow_clone_v2_execution_failed"
    assert events[-1]["status"] == "failed"
    assert events[-1]["failed_task_id"] == "memo"
    assert events[-1]["executed_task_count"] == 0


@pytest.mark.asyncio
async def test_runner_reports_blocked_when_recovery_leaves_only_blocked_tasks(
    monkeypatch,
) -> None:
    stored_tasks = []
    agent_turns = []

    async def _append_event(**_kwargs):
        return object()

    async def _store_task(task, *, ttl_seconds, timeout=None):
        stored_tasks.append(task)

    async def _noop_store_team(*_args, **_kwargs):
        return None

    async def _project_expired_task_lease_recovery_events(**kwargs):
        return kwargs["sequence_start"] + 1

    async def _list_tasks(**_kwargs):
        return [stored_tasks[0].model_copy(update={"status": TaskStatus.BLOCKED})]

    async def _run_agent_turn(**kwargs):
        agent_turns.append(kwargs)
        return object()

    monkeypatch.setattr(runner_module.event_log, "append_event", _append_event)
    monkeypatch.setattr(runner_module.task_pool, "store_task", _store_task)
    monkeypatch.setattr(runner_module.team_store, "store_team", _noop_store_team)
    monkeypatch.setattr(
        runner_module.agent_loop,
        "project_expired_task_lease_recovery_events",
        _project_expired_task_lease_recovery_events,
    )
    monkeypatch.setattr(runner_module.task_pool, "list_tasks", _list_tasks)
    monkeypatch.setattr(runner_module.agent_loop, "run_agent_turn", _run_agent_turn)

    runner = ShadowCloneV2Runner(
        thread_id="thread-1",
        project_id="project-1",
        agent_run_id="run-1",
        model_key="deepseek-v4-pro-max",
        db_client=object(),
        mode=ShadowCloneMode.V2,
    )

    events = [
        event
        async for event in runner.run(
            user_message="Summarize this memo.",
            thread_run_id="thread-run-1",
            dynamic_subtasks=[
                {
                    "id": "memo",
                    "role": "document analyst",
                    "task_description": "Summarize memo.",
                },
            ],
            execute_dynamic_plan=True,
        )
    ]

    assert agent_turns == []
    assert events[-1]["type"] == "shadow_clone_v2_execution_blocked"
    assert events[-1]["status"] == "blocked"
    assert events[-1]["blocked_task_ids"] == ["memo"]


@pytest.mark.asyncio
async def test_runner_persists_idle_team_member_after_agent_turn(monkeypatch) -> None:
    stored_teams = []

    async def _append_event(**_kwargs):
        return object()

    async def _noop_store_task(*_args, **_kwargs):
        return None

    async def _store_team(team, *, ttl_seconds, timeout=None):
        stored_teams.append(team)

    async def _run_agent_turn(**kwargs):
        idle_agent = kwargs["agent"].model_copy(
            update={
                "status": AgentLifecycleStatus.IDLE,
                "idle_since": "2026-05-31T12:05:00+00:00",
                "idle_expires_at": "2026-05-31T12:25:00+00:00",
            }
        )
        return AgentTurnResult(
            agent=idle_agent,
            task=kwargs["task"],
            output="done",
            next_sequence=kwargs["sequence_start"] + 3,
        )

    monkeypatch.setattr(runner_module.event_log, "append_event", _append_event)
    monkeypatch.setattr(runner_module.task_pool, "store_task", _noop_store_task)
    monkeypatch.setattr(runner_module.team_store, "store_team", _store_team)
    monkeypatch.setattr(runner_module.agent_loop, "run_agent_turn", _run_agent_turn)

    runner = ShadowCloneV2Runner(
        thread_id="thread-1",
        project_id="project-1",
        agent_run_id="run-1",
        model_key="deepseek-v4-pro-max",
        db_client=object(),
        mode=ShadowCloneMode.V2,
    )

    _events = [
        event
        async for event in runner.run(
            user_message="Summarize this memo.",
            thread_run_id="thread-run-1",
            dynamic_subtasks=[
                {
                    "id": "memo",
                    "role": "document analyst",
                    "task_description": "Summarize memo.",
                },
            ],
            execute_dynamic_plan=True,
        )
    ]

    assert len(stored_teams) >= 2
    assert stored_teams[0].members[0].status == AgentLifecycleStatus.STARTING
    assert stored_teams[-1].members[0].status == AgentLifecycleStatus.IDLE
    assert stored_teams[-1].members[0].idle_expires_at == "2026-05-31T12:25:00+00:00"


@pytest.mark.asyncio
async def test_runner_reuses_existing_idle_agent_in_same_thread(monkeypatch) -> None:
    appended = []
    agent_turns = []
    existing_idle_agent = AgentIdentity(
        agent_id="persisted-document-analyst@thread-1",
        agent_name="document-analyst",
        thread_id="thread-1",
        project_id="project-1",
        role="document analyst",
        status=AgentLifecycleStatus.IDLE,
        idle_since="2026-05-31T12:00:00+00:00",
        idle_expires_at="2099-05-31T12:20:00+00:00",
    )
    existing_team = TeamConfig(
        team_id="shadow-clone-v2:project-1:thread-1",
        thread_id="thread-1",
        project_id="project-1",
        facilitator_id="facilitator@thread-1",
        members=[existing_idle_agent],
    )

    async def _append_event(**kwargs):
        appended.append(kwargs)
        return object()

    async def _noop_store_task(*_args, **_kwargs):
        return None

    async def _noop_store_team(*_args, **_kwargs):
        return None

    async def _read_team(**_kwargs):
        return existing_team

    async def _run_agent_turn(**kwargs):
        agent_turns.append(kwargs["agent"])
        return AgentTurnResult(
            agent=kwargs["agent"].model_copy(
                update={"status": AgentLifecycleStatus.IDLE}
            ),
            task=kwargs["task"],
            output="done",
            next_sequence=kwargs["sequence_start"] + 3,
        )

    monkeypatch.setattr(runner_module.event_log, "append_event", _append_event)
    monkeypatch.setattr(runner_module.task_pool, "store_task", _noop_store_task)
    monkeypatch.setattr(runner_module.team_store, "store_team", _noop_store_team)
    monkeypatch.setattr(runner_module.team_store, "read_team", _read_team)
    monkeypatch.setattr(runner_module.agent_loop, "run_agent_turn", _run_agent_turn)

    runner = ShadowCloneV2Runner(
        thread_id="thread-1",
        project_id="project-1",
        agent_run_id="run-2",
        model_key="deepseek-v4-pro-max",
        db_client=object(),
        mode=ShadowCloneMode.V2,
    )

    _events = [
        event
        async for event in runner.run(
            user_message="Summarize this memo.",
            thread_run_id="thread-run-2",
            dynamic_subtasks=[
                {
                    "id": "memo",
                    "role": "document analyst",
                    "task_description": "Summarize memo.",
                },
            ],
            execute_dynamic_plan=True,
        )
    ]

    assert [agent.agent_id for agent in agent_turns] == [
        "persisted-document-analyst@thread-1"
    ]
    assert [event["event_type"] for event in appended][1] == EventType.AGENT_WAKE


@pytest.mark.asyncio
async def test_runner_does_not_reuse_expired_idle_agent_and_emits_shutdown(
    monkeypatch,
) -> None:
    appended = []
    agent_turns = []
    expired_idle_agent = AgentIdentity(
        agent_id="expired-document-analyst@thread-1",
        agent_name="document-analyst",
        thread_id="thread-1",
        project_id="project-1",
        role="document analyst",
        status=AgentLifecycleStatus.IDLE,
        idle_since="2000-05-31T12:00:00+00:00",
        idle_expires_at="2000-05-31T12:20:00+00:00",
    )
    existing_team = TeamConfig(
        team_id="shadow-clone-v2:project-1:thread-1",
        thread_id="thread-1",
        project_id="project-1",
        facilitator_id="facilitator@thread-1",
        members=[expired_idle_agent],
    )

    async def _append_event(**kwargs):
        appended.append(kwargs)
        return object()

    async def _noop_store_task(*_args, **_kwargs):
        return None

    async def _noop_store_team(*_args, **_kwargs):
        return None

    async def _read_team(**_kwargs):
        return existing_team

    async def _run_agent_turn(**kwargs):
        agent_turns.append(kwargs["agent"])
        return AgentTurnResult(
            agent=kwargs["agent"].model_copy(
                update={"status": AgentLifecycleStatus.IDLE}
            ),
            task=kwargs["task"],
            output="done",
            next_sequence=kwargs["sequence_start"] + 2,
        )

    monkeypatch.setattr(runner_module.event_log, "append_event", _append_event)
    monkeypatch.setattr(runner_module.task_pool, "store_task", _noop_store_task)
    monkeypatch.setattr(runner_module.team_store, "store_team", _noop_store_team)
    monkeypatch.setattr(runner_module.team_store, "read_team", _read_team)
    monkeypatch.setattr(runner_module.agent_loop, "run_agent_turn", _run_agent_turn)

    runner = ShadowCloneV2Runner(
        thread_id="thread-1",
        project_id="project-1",
        agent_run_id="run-3",
        model_key="deepseek-v4-pro-max",
        db_client=object(),
        mode=ShadowCloneMode.V2,
    )

    _events = [
        event
        async for event in runner.run(
            user_message="Summarize this memo.",
            thread_run_id="thread-run-3",
            dynamic_subtasks=[
                {
                    "id": "memo",
                    "role": "document analyst",
                    "task_description": "Summarize memo.",
                },
            ],
            execute_dynamic_plan=True,
        )
    ]

    assert [agent.agent_id for agent in agent_turns] == ["document-analyst@thread-1"]
    event_types = [event["event_type"] for event in appended]
    assert EventType.AGENT_SHUTDOWN in event_types
    assert EventType.AGENT_SPAWNED in event_types
    assert EventType.AGENT_WAKE not in event_types


@pytest.mark.asyncio
async def test_runner_appends_run_failed_and_yields_terminal_failure_when_agent_turn_raises(
    monkeypatch,
) -> None:
    appended = []

    async def _append_event(**kwargs):
        appended.append(kwargs)
        return object()

    async def _noop_store_task(*_args, **_kwargs):
        return None

    async def _noop_store_team(*_args, **_kwargs):
        return None

    async def _run_agent_turn(**_kwargs):
        error = RuntimeError("worker failed")
        error.next_sequence = 42
        raise error

    monkeypatch.setattr(runner_module.event_log, "append_event", _append_event)
    monkeypatch.setattr(runner_module.task_pool, "store_task", _noop_store_task)
    monkeypatch.setattr(runner_module.team_store, "store_team", _noop_store_team)
    monkeypatch.setattr(runner_module.agent_loop, "run_agent_turn", _run_agent_turn)

    runner = ShadowCloneV2Runner(
        thread_id="thread-1",
        project_id="project-1",
        agent_run_id="run-1",
        model_key="deepseek-v4-pro-max",
        db_client=object(),
        mode=ShadowCloneMode.V2,
    )

    events = [
        event
        async for event in runner.run(
            user_message="Summarize this memo.",
            thread_run_id="thread-run-1",
            dynamic_subtasks=[
                {
                    "id": "memo",
                    "role": "document analyst",
                    "task_description": "Summarize memo.",
                },
            ],
            execute_dynamic_plan=True,
        )
    ]

    assert events[-1]["type"] == "shadow_clone_v2_execution_failed"
    assert events[-1]["status"] == "failed"
    assert events[-1]["ui_phase"] == "failed"
    assert events[-1]["activity_owner"] == "none"
    assert events[-1]["subtasks"]
    assert events[-1]["subtasks"][0]["status"] == "failed"
    assert events[-1]["subtasks"][0]["id"] == events[-1]["failed_task_id"]
    assert appended[-1]["event_type"] == EventType.RUN_FAILED
    assert appended[-1]["sequence"] == 42
    assert appended[-1]["payload"]["error_type"] == "RuntimeError"


@pytest.mark.asyncio
async def test_runner_synthesis_failure_appends_run_failed_not_completed(
    monkeypatch,
) -> None:
    appended = []

    async def _append_event(**kwargs):
        appended.append(kwargs)
        return object()

    async def _noop_store_task(*_args, **_kwargs):
        return None

    async def _noop_store_team(*_args, **_kwargs):
        return None

    async def _run_agent_turn(**kwargs):
        return AgentTurnResult(
            agent=kwargs["agent"].model_copy(
                update={"status": AgentLifecycleStatus.IDLE}
            ),
            task=kwargs["task"],
            output="worker summary",
            next_sequence=kwargs["sequence_start"] + 3,
        )

    class _FailingSynthesizer:
        async def synthesize(self, **_kwargs):
            raise RuntimeError("synthesis model failed")

    monkeypatch.setattr(runner_module.event_log, "append_event", _append_event)
    monkeypatch.setattr(runner_module.task_pool, "store_task", _noop_store_task)
    monkeypatch.setattr(runner_module.team_store, "store_team", _noop_store_team)
    monkeypatch.setattr(runner_module.agent_loop, "run_agent_turn", _run_agent_turn)

    runner = ShadowCloneV2Runner(
        thread_id="thread-1",
        project_id="project-1",
        agent_run_id="run-1",
        model_key="deepseek-v4-pro-max",
        db_client=object(),
        mode=ShadowCloneMode.V2,
        main_agent_synthesizer=_FailingSynthesizer(),
    )

    events = [
        event
        async for event in runner.run(
            user_message="Summarize this memo.",
            thread_run_id="thread-run-1",
            dynamic_subtasks=[
                {
                    "id": "memo",
                    "role": "document analyst",
                    "task_description": "Summarize memo.",
                },
            ],
            execute_dynamic_plan=True,
        )
    ]

    assert events[-1]["type"] == "shadow_clone_v2_synthesis_failed"
    assert events[-1]["status"] == "failed"
    assert events[-1]["failure_stage"] == "final_synthesis"
    assert [event["event_type"] for event in appended][-1] == EventType.RUN_FAILED
    assert appended[-1]["payload"]["failure_stage"] == "final_synthesis"
    assert appended[-1]["payload"]["error_type"] == "RuntimeError"
    assert EventType.RUN_COMPLETED not in [event["event_type"] for event in appended]


@pytest.mark.asyncio
async def test_runner_empty_synthesis_output_appends_run_failed_not_completed(
    monkeypatch,
) -> None:
    appended = []

    async def _append_event(**kwargs):
        appended.append(kwargs)
        return object()

    async def _noop_store_task(*_args, **_kwargs):
        return None

    async def _noop_store_team(*_args, **_kwargs):
        return None

    async def _run_agent_turn(**kwargs):
        return AgentTurnResult(
            agent=kwargs["agent"].model_copy(
                update={"status": AgentLifecycleStatus.IDLE}
            ),
            task=kwargs["task"],
            output="worker summary",
            next_sequence=kwargs["sequence_start"] + 3,
        )

    class _EmptySynthesizer:
        async def synthesize(self, **kwargs):
            return runner_module.MainAgentSynthesisResult(
                output="   ",
                next_sequence=kwargs["sequence_start"],
            )

    monkeypatch.setattr(runner_module.event_log, "append_event", _append_event)
    monkeypatch.setattr(runner_module.task_pool, "store_task", _noop_store_task)
    monkeypatch.setattr(runner_module.team_store, "store_team", _noop_store_team)
    monkeypatch.setattr(runner_module.agent_loop, "run_agent_turn", _run_agent_turn)

    runner = ShadowCloneV2Runner(
        thread_id="thread-1",
        project_id="project-1",
        agent_run_id="run-1",
        model_key="deepseek-v4-pro-max",
        db_client=object(),
        mode=ShadowCloneMode.V2,
        main_agent_synthesizer=_EmptySynthesizer(),
    )

    events = [
        event
        async for event in runner.run(
            user_message="Summarize this memo.",
            thread_run_id="thread-run-1",
            dynamic_subtasks=[
                {
                    "id": "memo",
                    "role": "document analyst",
                    "task_description": "Summarize memo.",
                },
            ],
            execute_dynamic_plan=True,
        )
    ]

    assert events[-1]["type"] == "shadow_clone_v2_synthesis_failed"
    assert events[-1]["failure_stage"] == "final_synthesis"
    assert appended[-1]["event_type"] == EventType.RUN_FAILED
    assert appended[-1]["payload"]["error_type"] == "EmptyFinalSynthesisOutput"
    assert EventType.RUN_COMPLETED not in [event["event_type"] for event in appended]


@pytest.mark.asyncio
async def test_runner_executes_dynamic_plan_tasks_through_agent_loop(
    monkeypatch,
) -> None:
    appended = []
    stored_tasks = []
    stored_team = []
    agent_turns = []

    async def _append_event(**kwargs):
        appended.append(kwargs)
        return object()

    async def _store_task(task, *, ttl_seconds, timeout=None):
        stored_tasks.append((task, ttl_seconds, timeout))

    async def _store_team(team, *, ttl_seconds, timeout=None):
        stored_team.append((team, ttl_seconds, timeout))

    async def _run_agent_turn(**kwargs):
        agent_turns.append(kwargs)
        return object()

    monkeypatch.setattr(runner_module.event_log, "append_event", _append_event)
    monkeypatch.setattr(runner_module.task_pool, "store_task", _store_task)
    monkeypatch.setattr(runner_module.team_store, "store_team", _store_team)
    monkeypatch.setattr(runner_module.agent_loop, "run_agent_turn", _run_agent_turn)

    runner = ShadowCloneV2Runner(
        thread_id="thread-1",
        project_id="project-1",
        agent_run_id="run-1",
        model_key="model-1",
        db_client=object(),
        mode=ShadowCloneMode.V2,
        main_agent_synthesizer=_FixedMainAgentSynthesizer(),
    )

    events = [
        event
        async for event in runner.run(
            user_message="Summarize and QA this office memo.",
            thread_run_id="thread-run-1",
            dynamic_subtasks=[
                {
                    "id": "summarize",
                    "role": "summarizer",
                    "task_description": "Summarize memo.",
                },
                {
                    "id": "qa",
                    "role": "reviewer",
                    "task_description": "Check summary quality.",
                },
            ],
            execute_dynamic_plan=True,
        )
    ]

    assert [turn["agent"].agent_name for turn in agent_turns] == [
        "summarizer",
        "reviewer",
    ]
    assert [turn["task"].id for turn in agent_turns] == ["summarize", "qa"]
    assert all(turn["run_id"] == "run-1" for turn in agent_turns)
    assert all(turn["project_id"] == "project-1" for turn in agent_turns)
    sequence_starts = [turn["sequence_start"] for turn in agent_turns]
    assert sequence_starts[0] == 6
    assert sequence_starts == sorted(sequence_starts)
    assert len(set(sequence_starts)) == len(sequence_starts)
    assert all(callable(turn["executor"]) for turn in agent_turns)
    assert all(callable(turn["clock"]) for turn in agent_turns)
    assert all(
        datetime.fromisoformat(turn["lease_expires_at"]) > turn["now"]
        for turn in agent_turns
    )
    subagent_events = [
        event["type"] for event in events if event["type"].startswith("subagent_")
    ]
    assert subagent_events.count("subagent_started") == 2
    assert subagent_events.count("subagent_completed") == 2
    assert subagent_events.index("subagent_started") < subagent_events.index(
        "subagent_completed"
    )
    subagent_payloads = [
        event for event in events if event["type"].startswith("subagent_")
    ]
    assert subagent_payloads[0]["source"] == "shadow_clone_v2"
    assert {event["subtask_id"] for event in subagent_payloads} == {
        "summarize",
        "qa",
    }
    assert events[-1]["type"] == "shadow_clone_v2_execution_completed"
    assert events[-1]["status"] == "completed"
    assert events[-1]["executed_task_count"] == 2


@pytest.mark.asyncio
async def test_runner_starts_independent_tasks_concurrently(monkeypatch) -> None:
    started_agents: list[str] = []
    completed_agents: list[str] = []
    all_started = asyncio.Event()
    release_workers = asyncio.Event()

    async def _append_event(**_kwargs):
        return object()

    async def _noop_store_task(*_args, **_kwargs):
        return None

    async def _noop_store_team(*_args, **_kwargs):
        return None

    async def _run_agent_turn(**kwargs):
        started_agents.append(kwargs["agent"].agent_name)
        if len(started_agents) == 5:
            all_started.set()
        await release_workers.wait()
        completed_agents.append(kwargs["agent"].agent_name)
        return AgentTurnResult(
            agent=kwargs["agent"].model_copy(
                update={"status": AgentLifecycleStatus.IDLE}
            ),
            task=kwargs["task"],
            output=f"{kwargs['agent'].agent_name} done",
            next_sequence=kwargs["sequence_start"] + 3,
        )

    monkeypatch.setattr(runner_module, "SHADOW_CLONE_V2_MAX_PARALLEL_SUBAGENTS", 10)
    monkeypatch.setattr(runner_module.event_log, "append_event", _append_event)
    monkeypatch.setattr(runner_module.task_pool, "store_task", _noop_store_task)
    monkeypatch.setattr(runner_module.team_store, "store_team", _noop_store_team)
    monkeypatch.setattr(runner_module.agent_loop, "run_agent_turn", _run_agent_turn)

    runner = ShadowCloneV2Runner(
        thread_id="thread-1",
        project_id="project-1",
        agent_run_id="run-1",
        model_key="model-1",
        db_client=object(),
        mode=ShadowCloneMode.V2,
        main_agent_synthesizer=_FixedMainAgentSynthesizer(),
    )

    collect_task = asyncio.create_task(
        _collect_runner_events(
            runner,
            dynamic_subtasks=[
                {
                    "id": f"task-{index}",
                    "role": f"specialist {index}",
                    "task_description": f"Do task {index}.",
                }
                for index in range(1, 6)
            ],
        )
    )
    try:
        await asyncio.wait_for(all_started.wait(), timeout=0.25)
    except asyncio.TimeoutError:
        release_workers.set()
        await collect_task
        pytest.fail(
            "expected all five independent Shadow Clone V2 tasks to start before any completed"
        )

    assert completed_agents == []
    release_workers.set()
    events = await collect_task

    assert len(started_agents) == 5
    assert len(completed_agents) == 5
    assert events[-1]["executed_task_count"] == 5


@pytest.mark.asyncio
async def test_runner_batches_independent_tasks_at_parallel_cap(monkeypatch) -> None:
    active_agents: set[str] = set()
    started_agents: list[str] = []
    max_active = 0
    first_batch_started = asyncio.Event()
    release_first_batch = asyncio.Event()

    async def _append_event(**_kwargs):
        return object()

    async def _noop_store_task(*_args, **_kwargs):
        return None

    async def _noop_store_team(*_args, **_kwargs):
        return None

    async def _run_agent_turn(**kwargs):
        nonlocal max_active
        agent_name = kwargs["agent"].agent_name
        started_agents.append(agent_name)
        active_agents.add(agent_name)
        max_active = max(max_active, len(active_agents))
        if len(started_agents) == 10:
            first_batch_started.set()
        if len(started_agents) <= 10:
            await release_first_batch.wait()
        active_agents.remove(agent_name)
        return AgentTurnResult(
            agent=kwargs["agent"].model_copy(
                update={"status": AgentLifecycleStatus.IDLE}
            ),
            task=kwargs["task"],
            output=f"{agent_name} done",
            next_sequence=kwargs["sequence_start"] + 3,
        )

    monkeypatch.setattr(runner_module, "SHADOW_CLONE_V2_MAX_PARALLEL_SUBAGENTS", 10)
    monkeypatch.setattr(runner_module.event_log, "append_event", _append_event)
    monkeypatch.setattr(runner_module.task_pool, "store_task", _noop_store_task)
    monkeypatch.setattr(runner_module.team_store, "store_team", _noop_store_team)
    monkeypatch.setattr(runner_module.agent_loop, "run_agent_turn", _run_agent_turn)

    runner = ShadowCloneV2Runner(
        thread_id="thread-1",
        project_id="project-1",
        agent_run_id="run-1",
        model_key="model-1",
        db_client=object(),
        mode=ShadowCloneMode.V2,
        main_agent_synthesizer=_FixedMainAgentSynthesizer(),
    )

    collect_task = asyncio.create_task(
        _collect_runner_events(
            runner,
            dynamic_subtasks=[
                {
                    "id": f"task-{index}",
                    "role": f"specialist {index}",
                    "task_description": f"Do task {index}.",
                }
                for index in range(1, 13)
            ],
        )
    )
    try:
        await asyncio.wait_for(first_batch_started.wait(), timeout=0.25)
    except asyncio.TimeoutError:
        release_first_batch.set()
        await collect_task
        pytest.fail("expected first ten independent tasks to start as one batch")

    await asyncio.sleep(0)
    assert len(started_agents) == 10
    release_first_batch.set()
    events = await collect_task

    assert len(started_agents) == 12
    assert max_active <= 10
    assert events[-1]["executed_task_count"] == 12


@pytest.mark.asyncio
async def test_runner_never_runs_two_tasks_for_same_agent_in_same_batch(
    monkeypatch,
) -> None:
    active_agents: set[str] = set()
    concurrent_same_agent_detected = False
    first_task_started = asyncio.Event()
    release_first_task = asyncio.Event()
    started_tasks: list[str] = []

    async def _append_event(**_kwargs):
        return object()

    async def _noop_store_task(*_args, **_kwargs):
        return None

    async def _noop_store_team(*_args, **_kwargs):
        return None

    async def _run_agent_turn(**kwargs):
        nonlocal concurrent_same_agent_detected
        agent_name = kwargs["agent"].agent_name
        task_id = kwargs["task"].id
        if agent_name in active_agents:
            concurrent_same_agent_detected = True
        active_agents.add(agent_name)
        started_tasks.append(task_id)
        if task_id == "task-a":
            first_task_started.set()
            await release_first_task.wait()
        active_agents.remove(agent_name)
        return AgentTurnResult(
            agent=kwargs["agent"].model_copy(
                update={"status": AgentLifecycleStatus.IDLE}
            ),
            task=kwargs["task"],
            output=f"{task_id} done",
            next_sequence=kwargs["sequence_start"] + 3,
        )

    monkeypatch.setattr(runner_module, "SHADOW_CLONE_V2_MAX_PARALLEL_SUBAGENTS", 10)
    monkeypatch.setattr(runner_module.event_log, "append_event", _append_event)
    monkeypatch.setattr(runner_module.task_pool, "store_task", _noop_store_task)
    monkeypatch.setattr(runner_module.team_store, "store_team", _noop_store_team)
    monkeypatch.setattr(runner_module.agent_loop, "run_agent_turn", _run_agent_turn)

    runner = ShadowCloneV2Runner(
        thread_id="thread-1",
        project_id="project-1",
        agent_run_id="run-1",
        model_key="model-1",
        db_client=object(),
        mode=ShadowCloneMode.V2,
        main_agent_synthesizer=_FixedMainAgentSynthesizer(),
    )

    collect_task = asyncio.create_task(
        _collect_runner_events(
            runner,
            dynamic_subtasks=[
                {
                    "id": "task-a",
                    "role": "shared specialist",
                    "task_description": "Do first task.",
                },
                {
                    "id": "task-b",
                    "role": "shared specialist",
                    "task_description": "Do second task.",
                },
            ],
        )
    )
    await asyncio.wait_for(first_task_started.wait(), timeout=0.25)
    await asyncio.sleep(0)
    assert started_tasks == ["task-a"]
    release_first_task.set()
    events = await collect_task

    assert concurrent_same_agent_detected is False
    assert started_tasks == ["task-a", "task-b"]
    assert events[-1]["executed_task_count"] == 2


@pytest.mark.asyncio
async def test_runner_executes_dependency_blocked_tasks_in_later_layers(
    monkeypatch,
) -> None:
    started_tasks: list[str] = []
    completed_tasks: set[str] = set()

    async def _append_event(**_kwargs):
        return object()

    async def _noop_store_task(*_args, **_kwargs):
        return None

    async def _noop_store_team(*_args, **_kwargs):
        return None

    async def _run_agent_turn(**kwargs):
        task_id = kwargs["task"].id
        started_tasks.append(task_id)
        if task_id == "dependent":
            assert "base" in completed_tasks
        completed_tasks.add(task_id)
        return AgentTurnResult(
            agent=kwargs["agent"].model_copy(
                update={"status": AgentLifecycleStatus.IDLE}
            ),
            task=kwargs["task"],
            output=f"{task_id} done",
            next_sequence=kwargs["sequence_start"] + 3,
        )

    monkeypatch.setattr(runner_module, "SHADOW_CLONE_V2_MAX_PARALLEL_SUBAGENTS", 10)
    monkeypatch.setattr(runner_module.event_log, "append_event", _append_event)
    monkeypatch.setattr(runner_module.task_pool, "store_task", _noop_store_task)
    monkeypatch.setattr(runner_module.team_store, "store_team", _noop_store_team)
    monkeypatch.setattr(runner_module.agent_loop, "run_agent_turn", _run_agent_turn)

    runner = ShadowCloneV2Runner(
        thread_id="thread-1",
        project_id="project-1",
        agent_run_id="run-1",
        model_key="model-1",
        db_client=object(),
        mode=ShadowCloneMode.V2,
        main_agent_synthesizer=_FixedMainAgentSynthesizer(),
    )

    events = await _collect_runner_events(
        runner,
        dynamic_subtasks=[
            {
                "id": "base",
                "role": "base specialist",
                "task_description": "Create base output.",
            },
            {
                "id": "dependent",
                "role": "dependent specialist",
                "task_description": "Use base output.",
                "blocked_by": ["base"],
            },
        ],
    )

    assert started_tasks == ["base", "dependent"]
    assert events[-1]["executed_task_count"] == 2


@pytest.mark.asyncio
async def test_runner_respects_multi_layer_blocked_dependencies_without_serializing_ready_tasks(
    monkeypatch,
) -> None:
    started_tasks: list[str] = []
    completed_tasks: set[str] = set()
    active_tasks: set[str] = set()
    max_active_tasks = 0

    async def _append_event(**_kwargs):
        return object()

    async def _noop_store_task(*_args, **_kwargs):
        return None

    async def _noop_store_team(*_args, **_kwargs):
        return None

    async def _run_agent_turn(**kwargs):
        nonlocal max_active_tasks
        task_id = kwargs["task"].id
        blockers = list(kwargs["task"].blocked_by or [])
        started_tasks.append(task_id)

        for blocker in blockers:
            assert blocker in completed_tasks, (
                f"{task_id} started before blocker {blocker} completed; "
                f"completed={sorted(completed_tasks)}"
            )

        active_tasks.add(task_id)
        max_active_tasks = max(max_active_tasks, len(active_tasks))
        await asyncio.sleep(0)
        active_tasks.remove(task_id)
        completed_tasks.add(task_id)
        return AgentTurnResult(
            agent=kwargs["agent"].model_copy(
                update={"status": AgentLifecycleStatus.IDLE}
            ),
            task=kwargs["task"],
            output=f"{task_id} done",
            next_sequence=kwargs["sequence_start"] + 3,
        )

    monkeypatch.setattr(runner_module, "SHADOW_CLONE_V2_MAX_PARALLEL_SUBAGENTS", 10)
    monkeypatch.setattr(runner_module.event_log, "append_event", _append_event)
    monkeypatch.setattr(runner_module.task_pool, "store_task", _noop_store_task)
    monkeypatch.setattr(runner_module.team_store, "store_team", _noop_store_team)
    monkeypatch.setattr(runner_module.agent_loop, "run_agent_turn", _run_agent_turn)

    runner = ShadowCloneV2Runner(
        thread_id="thread-1",
        project_id="project-1",
        agent_run_id="run-1",
        model_key="model-1",
        db_client=object(),
        mode=ShadowCloneMode.V2,
        main_agent_synthesizer=_FixedMainAgentSynthesizer(),
    )

    events = await _collect_runner_events(
        runner,
        dynamic_subtasks=[
            {
                "id": "task-a",
                "role": "layer A specialist",
                "task_description": "Create layer A output.",
            },
            {
                "id": "task-b",
                "role": "layer B specialist",
                "task_description": "Use layer A output.",
                "blocked_by": ["task-a"],
            },
            {
                "id": "task-c",
                "role": "layer C specialist",
                "task_description": "Use layer B output.",
                "blocked_by": ["task-b"],
            },
            {
                "id": "task-independent",
                "role": "independent specialist",
                "task_description": "Run independently while layer A is ready.",
            },
        ],
    )

    assert started_tasks.index("task-a") < started_tasks.index("task-b")
    assert started_tasks.index("task-b") < started_tasks.index("task-c")
    assert max_active_tasks >= 2
    assert set(started_tasks) == {
        "task-a",
        "task-b",
        "task-c",
        "task-independent",
    }
    assert events[-1]["executed_task_count"] == 4


@pytest.mark.asyncio
async def test_runner_executes_blocked_status_task_after_blocker_completes(
    monkeypatch,
) -> None:
    stored_tasks = []
    started_tasks: list[str] = []

    async def _append_event(**_kwargs):
        return object()

    async def _store_task(task, *, ttl_seconds, timeout=None):
        stored_tasks.append(task)

    async def _noop_store_team(*_args, **_kwargs):
        return None

    async def _list_tasks(**_kwargs):
        if not stored_tasks:
            return []
        base = stored_tasks[0]
        dependent = stored_tasks[1].model_copy(
            update={
                "status": TaskStatus.BLOCKED,
                "blocked_by": ["base"],
            }
        )
        if "base" in started_tasks:
            base = base.model_copy(update={"status": TaskStatus.COMPLETED})
        if "dependent" in started_tasks:
            dependent = dependent.model_copy(update={"status": TaskStatus.COMPLETED})
        return [base, dependent]

    async def _run_agent_turn(**kwargs):
        task_id = kwargs["task"].id
        started_tasks.append(task_id)
        if task_id == "dependent":
            assert "base" in started_tasks
        return AgentTurnResult(
            agent=kwargs["agent"].model_copy(
                update={"status": AgentLifecycleStatus.IDLE}
            ),
            task=kwargs["task"],
            output=f"{task_id} done",
            next_sequence=kwargs["sequence_start"] + 3,
        )

    monkeypatch.setattr(runner_module, "SHADOW_CLONE_V2_MAX_PARALLEL_SUBAGENTS", 10)
    monkeypatch.setattr(runner_module.event_log, "append_event", _append_event)
    monkeypatch.setattr(runner_module.task_pool, "store_task", _store_task)
    monkeypatch.setattr(runner_module.task_pool, "list_tasks", _list_tasks)
    monkeypatch.setattr(runner_module.team_store, "store_team", _noop_store_team)
    monkeypatch.setattr(runner_module.agent_loop, "run_agent_turn", _run_agent_turn)

    runner = ShadowCloneV2Runner(
        thread_id="thread-1",
        project_id="project-1",
        agent_run_id="run-1",
        model_key="model-1",
        db_client=object(),
        mode=ShadowCloneMode.V2,
        main_agent_synthesizer=_FixedMainAgentSynthesizer(),
    )

    events = await _collect_runner_events(
        runner,
        dynamic_subtasks=[
            {
                "id": "base",
                "role": "base specialist",
                "task_description": "Create base output.",
            },
            {
                "id": "dependent",
                "role": "dependent specialist",
                "task_description": "Use base output.",
                "blocked_by": ["base"],
            },
        ],
    )

    assert started_tasks == ["base", "dependent"]
    assert events[-1]["executed_task_count"] == 2


@pytest.mark.asyncio
async def test_runner_emits_subagent_activity_and_final_assistant_output_for_v2_ui(
    monkeypatch,
) -> None:
    appended = []
    synthesis_calls = []

    async def _append_event(**kwargs):
        appended.append(kwargs)
        return object()

    async def _noop_store_task(*_args, **_kwargs):
        return None

    async def _noop_store_team(*_args, **_kwargs):
        return None

    async def _run_agent_turn(**kwargs):
        return AgentTurnResult(
            agent=kwargs["agent"].model_copy(
                update={"status": AgentLifecycleStatus.IDLE}
            ),
            task=kwargs["task"],
            output="memo summary complete",
            next_sequence=kwargs["sequence_start"] + 3,
        )

    class _InjectedMainAgentSynthesizer:
        async def synthesize(self, **kwargs):
            synthesis_calls.append(kwargs)
            return runner_module.MainAgentSynthesisResult(
                output="main synthesized answer",
                next_sequence=kwargs["sequence_start"],
            )

    monkeypatch.setattr(runner_module.event_log, "append_event", _append_event)
    monkeypatch.setattr(runner_module.task_pool, "store_task", _noop_store_task)
    monkeypatch.setattr(runner_module.team_store, "store_team", _noop_store_team)
    monkeypatch.setattr(runner_module.agent_loop, "run_agent_turn", _run_agent_turn)

    runner = ShadowCloneV2Runner(
        thread_id="thread-1",
        project_id="project-1",
        agent_run_id="run-1",
        model_key="deepseek-v4-pro-max",
        db_client=object(),
        mode=ShadowCloneMode.V2,
        main_agent_synthesizer=_InjectedMainAgentSynthesizer(),
    )

    events = [
        event
        async for event in runner.run(
            user_message="Summarize this memo.",
            thread_run_id="thread-run-1",
            dynamic_subtasks=[
                {
                    "id": "memo",
                    "role": "summarizer",
                    "task_description": "Summarize memo.",
                },
            ],
            execute_dynamic_plan=True,
        )
    ]

    activity_events = [
        event for event in events if event["type"] == "subagent_activity"
    ]
    assert activity_events
    activity_payload = activity_events[-1]
    assert activity_payload["subtask_id"] == "memo"
    assert activity_payload["source"] == "shadow_clone_v2"
    assert activity_payload["message_type"] == "assistant"
    assert activity_payload["content"]["content"] == "memo summary complete"
    assert activity_payload["metadata"]["stream_status"] == "complete"

    assistant_events = [event for event in events if event["type"] == "assistant"]
    assert assistant_events
    final_assistant = assistant_events[-1]
    assert final_assistant["message_id"]
    assert final_assistant["project_id"] == "project-1"
    assert (
        json.loads(final_assistant["content"])["content"] == "main synthesized answer"
    )
    metadata = json.loads(final_assistant["metadata"])
    assert metadata["stream_status"] == "complete"
    assert metadata["activity_owner"] == "main_agent"
    assert metadata["ui_phase"] == "main_agent_continuation"
    assert len(synthesis_calls) == 1
    assert synthesis_calls[0]["model_key"] == "deepseek-v4-pro-max"
    assert synthesis_calls[0]["worker_outputs"] == [
        {
            "agent_name": "summarizer",
            "task_id": "memo",
            "task_subject": "summarizer",
            "task_description": "Summarize memo.",
            "output": "memo summary complete",
        }
    ]

    assert events[-1]["type"] == "shadow_clone_v2_execution_completed"
    run_completed_events = [
        event for event in appended if event["event_type"] == EventType.RUN_COMPLETED
    ]
    assert (
        run_completed_events[-1]["payload"]["final_output"] == "main synthesized answer"
    )


@pytest.mark.asyncio
async def test_runner_yields_fast_subagent_activity_before_slow_batch_member_completes(
    monkeypatch,
) -> None:
    appended = []

    async def _append_event(**kwargs):
        appended.append(kwargs)
        return object()

    async def _noop_store_task(*_args, **_kwargs):
        return None

    async def _noop_store_team(*_args, **_kwargs):
        return None

    async def _run_agent_turn(**kwargs):
        task = kwargs["task"]
        if task.id == "slow":
            await asyncio.sleep(0.25)
            output = "slow subagent finished"
        else:
            await asyncio.sleep(0.01)
            output = "SUB_STREAM_MARKER_fast_subagent_finished_while_slow_running"
        return AgentTurnResult(
            agent=kwargs["agent"].model_copy(
                update={"status": AgentLifecycleStatus.IDLE}
            ),
            task=task,
            output=output,
            next_sequence=kwargs["sequence_start"] + 3,
        )

    monkeypatch.setattr(runner_module, "SHADOW_CLONE_V2_MAX_PARALLEL_SUBAGENTS", 10)
    monkeypatch.setattr(runner_module.event_log, "append_event", _append_event)
    monkeypatch.setattr(runner_module.task_pool, "store_task", _noop_store_task)
    monkeypatch.setattr(runner_module.team_store, "store_team", _noop_store_team)
    monkeypatch.setattr(runner_module.agent_loop, "run_agent_turn", _run_agent_turn)

    runner = ShadowCloneV2Runner(
        thread_id="thread-1",
        project_id="project-1",
        agent_run_id="run-1",
        model_key="model-1",
        db_client=object(),
        mode=ShadowCloneMode.V2,
        main_agent_synthesizer=_FixedMainAgentSynthesizer(),
    )

    stream = runner.run(
        user_message="Run a fast and slow task.",
        thread_run_id="thread-run-1",
        dynamic_subtasks=[
            {
                "id": "fast",
                "role": "fast writer",
                "task_description": "Return immediately.",
            },
            {
                "id": "slow",
                "role": "slow writer",
                "task_description": "Wait until released.",
            },
        ],
        execute_dynamic_plan=True,
    )
    try:
        seen = []
        while True:
            try:
                event = await anext(stream)
            except StopAsyncIteration:
                pytest.fail(f"runner ended before starting subagent batch; seen={seen!r}")
            metadata = {}
            if isinstance(event.get("metadata"), str):
                metadata = json.loads(event.get("metadata") or "{}")
            elif isinstance(event.get("metadata"), dict):
                metadata = event.get("metadata") or {}
            seen.append(
                (
                    event.get("type"),
                    event.get("phase_reason") or metadata.get("phase_reason"),
                    event.get("status"),
                    event.get("subtask_id"),
                    event.get("executed_task_count"),
                )
            )
            if (
                event.get("phase_reason") or metadata.get("phase_reason")
            ) == "shadow_clone_v2_waiting_for_subagents":
                break

        started_waiting_at = asyncio.get_running_loop().time()
        next_event = await anext(stream)
        elapsed = asyncio.get_running_loop().time() - started_waiting_at
        assert elapsed < 0.10, (
            "fast subagent natural-language activity was buffered behind the "
            "slow subagent in the same batch"
        )
        assert next_event["type"] == "subagent_activity"
        assert next_event["subtask_id"] == "fast"
        assert next_event["content"]["content"] == (
            "SUB_STREAM_MARKER_fast_subagent_finished_while_slow_running"
        )
    finally:
        await stream.aclose()


@pytest.mark.asyncio
async def test_runner_yields_executor_streamed_subagent_text_chunk_before_turn_completion(
    monkeypatch,
) -> None:
    appended = []

    async def _append_event(**kwargs):
        appended.append(kwargs)
        return object()

    async def _noop_store_task(*_args, **_kwargs):
        return None

    async def _noop_store_team(*_args, **_kwargs):
        return None

    async def _run_agent_turn(**kwargs):
        on_stream_activity = kwargs.get("on_stream_activity")
        task = kwargs["task"]
        agent = kwargs["agent"]
        if on_stream_activity is not None:
            await on_stream_activity(
                {
                    "type": "subagent_activity",
                    "status": "running",
                    "source": "shadow_clone_v2",
                    "thread_run_id": "thread-run-1",
                    "agent_run_id": "run-1",
                    "ui_phase": "subagents_running",
                    "activity_owner": "shadow_clone",
                    "phase_reason": "shadow_clone_v2_subagent_activity",
                    "subtask_id": task.id,
                    "sequence": kwargs["sequence_start"],
                    "role": task.subject,
                    "agent_name": agent.agent_name,
                    "message_type": "assistant",
                    "content": {
                        "role": "assistant",
                        "content": "SUB_STREAM_MARKER_runner_chunk_before_turn_done",
                    },
                    "metadata": {
                        "stream_status": "chunk",
                        "source": "shadow_clone_v2",
                        "subtask_id": task.id,
                        "agent_name": agent.agent_name,
                    },
                }
            )
        await asyncio.sleep(0.05)
        return AgentTurnResult(
            agent=agent.model_copy(update={"status": AgentLifecycleStatus.IDLE}),
            task=task,
            output="worker final output",
            next_sequence=kwargs["sequence_start"] + 3,
        )

    monkeypatch.setattr(runner_module.event_log, "append_event", _append_event)
    monkeypatch.setattr(runner_module.task_pool, "store_task", _noop_store_task)
    monkeypatch.setattr(runner_module.team_store, "store_team", _noop_store_team)
    monkeypatch.setattr(runner_module.agent_loop, "run_agent_turn", _run_agent_turn)

    runner = ShadowCloneV2Runner(
        thread_id="thread-1",
        project_id="project-1",
        agent_run_id="run-1",
        model_key="model-1",
        db_client=object(),
        mode=ShadowCloneMode.V2,
        main_agent_synthesizer=_FixedMainAgentSynthesizer(),
    )

    stream = runner.run(
        user_message="Run one streaming task.",
        thread_run_id="thread-run-1",
        dynamic_subtasks=[
            {
                "id": "streaming",
                "role": "streaming writer",
                "task_description": "Stream while working.",
            },
        ],
        execute_dynamic_plan=True,
    )
    try:
        while True:
            event = await anext(stream)
            metadata = {}
            if isinstance(event.get("metadata"), str):
                metadata = json.loads(event.get("metadata") or "{}")
            elif isinstance(event.get("metadata"), dict):
                metadata = event.get("metadata") or {}
            if (
                event.get("phase_reason") or metadata.get("phase_reason")
            ) == "shadow_clone_v2_waiting_for_subagents":
                break

        next_event = await anext(stream)
        assert next_event["type"] == "subagent_activity", next_event
        assert next_event["subtask_id"] == "streaming"
        assert next_event["metadata"]["stream_status"] == "chunk"
        assert next_event["content"]["content"] == (
            "SUB_STREAM_MARKER_runner_chunk_before_turn_done"
        )
    finally:
        await stream.aclose()


@pytest.mark.asyncio
async def test_runner_does_not_duplicate_subagent_complete_activity_after_streamed_complete(
    monkeypatch,
) -> None:
    async def _append_event(**_kwargs):
        return object()

    async def _noop_store_task(*_args, **_kwargs):
        return None

    async def _noop_store_team(*_args, **_kwargs):
        return None

    async def _run_agent_turn(**kwargs):
        on_stream_activity = kwargs.get("on_stream_activity")
        task = kwargs["task"]
        agent = kwargs["agent"]
        if on_stream_activity is not None:
            await on_stream_activity(
                {
                    "type": "subagent_activity",
                    "status": "running",
                    "source": "shadow_clone_v2",
                    "thread_run_id": "thread-run-1",
                    "agent_run_id": "run-1",
                    "ui_phase": "subagents_running",
                    "activity_owner": "shadow_clone",
                    "phase_reason": "shadow_clone_v2_subagent_activity",
                    "subtask_id": task.id,
                    "sequence": kwargs["sequence_start"],
                    "role": task.subject,
                    "agent_name": agent.agent_name,
                    "message_type": "assistant",
                    "content": {
                        "role": "assistant",
                        "content": "SUB_STREAM_MARKER_runner_streamed_complete",
                    },
                    "metadata": {
                        "stream_status": "complete",
                        "source": "shadow_clone_v2",
                        "subtask_id": task.id,
                        "agent_name": agent.agent_name,
                    },
                }
            )
        return AgentTurnResult(
            agent=agent.model_copy(update={"status": AgentLifecycleStatus.IDLE}),
            task=task,
            output="SUB_STREAM_MARKER_runner_streamed_complete",
            next_sequence=kwargs["sequence_start"] + 3,
        )

    monkeypatch.setattr(runner_module.event_log, "append_event", _append_event)
    monkeypatch.setattr(runner_module.task_pool, "store_task", _noop_store_task)
    monkeypatch.setattr(runner_module.team_store, "store_team", _noop_store_team)
    monkeypatch.setattr(runner_module.agent_loop, "run_agent_turn", _run_agent_turn)

    runner = ShadowCloneV2Runner(
        thread_id="thread-1",
        project_id="project-1",
        agent_run_id="run-1",
        model_key="model-1",
        db_client=object(),
        mode=ShadowCloneMode.V2,
        main_agent_synthesizer=_FixedMainAgentSynthesizer(),
    )

    events = [
        event
        async for event in runner.run(
            user_message="Run one streaming task.",
            thread_run_id="thread-run-1",
            dynamic_subtasks=[
                {
                    "id": "streaming",
                    "role": "streaming writer",
                    "task_description": "Stream while working.",
                },
            ],
            execute_dynamic_plan=True,
        )
    ]

    complete_activities = [
        event
        for event in events
        if event.get("type") == "subagent_activity"
        and event.get("subtask_id") == "streaming"
        and (event.get("metadata") or {}).get("stream_status") == "complete"
        and "SUB_STREAM_MARKER_runner_streamed_complete" in str(event.get("content"))
    ]
    assert len(complete_activities) == 1


@pytest.mark.asyncio
async def test_runner_refreshes_living_task_state_before_final_synthesis(
    monkeypatch,
) -> None:
    appended = []
    stored_tasks = []
    synthesis_calls = []

    async def _append_event(**kwargs):
        appended.append(kwargs)
        return object()

    async def _store_task(task, *, ttl_seconds, timeout=None):
        stored_tasks.append(task)

    async def _noop_store_team(*_args, **_kwargs):
        return None

    async def _list_tasks(*, run_id, timeout=None):
        assert run_id == "run-1"
        return [
            stored_tasks[0].model_copy(
                update={
                    "status": TaskStatus.COMPLETED,
                    "result_ref": "artifact://completed-memo",
                    "plan_revision": stored_tasks[0].plan_revision + 3,
                }
            )
        ]

    async def _run_agent_turn(**kwargs):
        return AgentTurnResult(
            agent=kwargs["agent"].model_copy(
                update={"status": AgentLifecycleStatus.IDLE}
            ),
            task=kwargs["task"],
            output="worker summary",
            next_sequence=kwargs["sequence_start"] + 3,
        )

    class _AssertingSynthesizer:
        async def synthesize(self, **kwargs):
            synthesis_calls.append(kwargs)
            task_state = kwargs["tasks"][0]
            assert task_state.status == TaskStatus.COMPLETED
            assert task_state.result_ref == "artifact://completed-memo"
            return runner_module.MainAgentSynthesisResult(
                output="main synthesized answer",
                next_sequence=kwargs["sequence_start"],
            )

    monkeypatch.setattr(runner_module.event_log, "append_event", _append_event)
    monkeypatch.setattr(runner_module.task_pool, "store_task", _store_task)
    monkeypatch.setattr(runner_module.task_pool, "list_tasks", _list_tasks)
    monkeypatch.setattr(runner_module.team_store, "store_team", _noop_store_team)
    monkeypatch.setattr(runner_module.agent_loop, "run_agent_turn", _run_agent_turn)

    runner = ShadowCloneV2Runner(
        thread_id="thread-1",
        project_id="project-1",
        agent_run_id="run-1",
        model_key="deepseek-v4-pro-max",
        db_client=object(),
        mode=ShadowCloneMode.V2,
        main_agent_synthesizer=_AssertingSynthesizer(),
    )

    events = [
        event
        async for event in runner.run(
            user_message="Summarize this memo.",
            thread_run_id="thread-run-1",
            dynamic_subtasks=[
                {
                    "id": "memo",
                    "role": "summarizer",
                    "task_description": "Summarize memo.",
                },
            ],
            execute_dynamic_plan=True,
        )
    ]

    assert len(synthesis_calls) == 1
    assert events[-1]["type"] == "shadow_clone_v2_execution_completed"
    assert appended[-1]["event_type"] == EventType.RUN_COMPLETED


@pytest.mark.asyncio
async def test_runner_defaults_to_agentscope_task_executor_and_carries_subagent_model(
    monkeypatch,
) -> None:
    appended = []
    contexts = []

    async def _append_event(**kwargs):
        appended.append(kwargs)
        return object()

    async def _noop_store_task(*_args, **_kwargs):
        return None

    async def _noop_store_team(*_args, **_kwargs):
        return None

    async def _execute(self, *, task, agent, run_context, inbox_messages=None):
        contexts.append(
            (
                task.id,
                agent.agent_name,
                run_context.model_key,
                run_context.subagent_model,
                inbox_messages,
            )
        )
        return "real executor seam called"

    async def _run_agent_turn(**kwargs):
        await kwargs["executor"](kwargs["task"], kwargs["agent"])
        return object()

    monkeypatch.setattr(runner_module.event_log, "append_event", _append_event)
    monkeypatch.setattr(runner_module.task_pool, "store_task", _noop_store_task)
    monkeypatch.setattr(runner_module.team_store, "store_team", _noop_store_team)
    monkeypatch.setattr(runner_module.agent_loop, "run_agent_turn", _run_agent_turn)
    monkeypatch.setattr(AgentScopeShadowCloneV2TaskExecutor, "execute", _execute)

    runner = ShadowCloneV2Runner(
        thread_id="thread-1",
        project_id="project-1",
        agent_run_id="run-1",
        model_key="deepseek-v4-pro-high",
        subagent_model="deepseek-v4-pro-max",
        db_client=object(),
        mode=ShadowCloneMode.V2,
    )

    assert isinstance(runner.task_executor, AgentScopeShadowCloneV2TaskExecutor)

    _events = [
        event
        async for event in runner.run(
            user_message="Summarize and QA this office memo.",
            thread_run_id="thread-run-1",
            dynamic_subtasks=[
                {
                    "id": "summarize",
                    "role": "summarizer",
                    "task_description": "Summarize memo.",
                },
            ],
            execute_dynamic_plan=True,
        )
    ]

    assert contexts == [
        (
            "summarize",
            "summarizer",
            "deepseek-v4-pro-high",
            "deepseek-v4-pro-max",
            None,
        )
    ]


class _OpeningProgressFakeWorkerResponse:
    def __init__(self, text: str) -> None:
        self._text = text

    def get_text_content(self) -> str:
        return self._text


class _OpeningProgressFakeToolChunk:
    def __init__(self, content: str) -> None:
        self.content = content


class _OpeningProgressFakeToolkit:
    async def call_tool_function(self, _tool_call):
        async def _chunks():
            yield _OpeningProgressFakeToolChunk("tool done")

        return _chunks()


class _OpeningProgressFakeWorker:
    def __init__(self) -> None:
        self.toolkit = _OpeningProgressFakeToolkit()
        self.agent = self

    def get_agent(self):
        return self.agent

    async def __call__(self, _msg):
        tool_result = await self.toolkit.call_tool_function(
            {
                "id": "tool-1",
                "name": "write_file",
                "input": {"path": "/workspace/realtime.md"},
            }
        )
        async for _chunk in tool_result:
            pass
        return _OpeningProgressFakeWorkerResponse("worker finished after tool")


@pytest.mark.asyncio
async def test_runner_yields_executor_opening_progress_before_worker_tool_without_model_chunk(
    monkeypatch,
) -> None:
    appended = []
    stored_tasks: dict[str, Task] = {}

    async def _append_event(**kwargs):
        appended.append(kwargs)
        return object()

    async def _store_task(task, *, ttl_seconds, timeout=None):
        stored_tasks[task.id] = task

    async def _noop_store_team(*_args, **_kwargs):
        return None

    async def _claim_task(**kwargs):
        task = stored_tasks[kwargs["task_id"]]
        stored_tasks[task.id] = task.model_copy(
            update={
                "status": TaskStatus.IN_PROGRESS,
                "owner_agent": kwargs["agent_name"],
                "lease_expires_at": kwargs["lease_expires_at"],
                "attempt": task.attempt + 1,
                "plan_revision": task.plan_revision + 1,
            }
        )
        return True

    async def _read_task(*, run_id, task_id, timeout=None):
        return stored_tasks.get(task_id)

    async def _complete_task(**kwargs):
        task = stored_tasks[kwargs["task_id"]]
        stored_tasks[task.id] = task.model_copy(
            update={
                "status": TaskStatus.COMPLETED,
                "result_ref": kwargs["result_ref"],
                "plan_revision": task.plan_revision + 1,
            }
        )
        return True

    async def _fail_task(**_kwargs):
        return True

    async def _read_next_messages_with_dead_letters(**_kwargs):
        return runner_module.agent_loop.mailbox.MailboxReadResult(
            messages=[],
            dead_lettered=[],
        )

    async def _worker_factory(**_kwargs):
        return _OpeningProgressFakeWorker()

    async def _fake_stream_printing_messages(*, agents, coroutine_task):
        await coroutine_task
        if False:
            yield None

    monkeypatch.setattr(runner_module.event_log, "append_event", _append_event)
    monkeypatch.setattr(runner_module.task_pool, "store_task", _store_task)
    monkeypatch.setattr(runner_module.team_store, "store_team", _noop_store_team)
    monkeypatch.setattr(runner_module.task_pool, "claim_task", _claim_task)
    monkeypatch.setattr(runner_module.task_pool, "read_task", _read_task)
    monkeypatch.setattr(runner_module.task_pool, "complete_task", _complete_task)
    monkeypatch.setattr(runner_module.task_pool, "fail_task", _fail_task)
    monkeypatch.setattr(
        runner_module.agent_loop.mailbox,
        "read_next_messages_with_dead_letters",
        _read_next_messages_with_dead_letters,
    )
    monkeypatch.setattr(
        task_executor_module,
        "stream_printing_messages",
        _fake_stream_printing_messages,
        raising=False,
    )

    runner = ShadowCloneV2Runner(
        thread_id="thread-1",
        project_id="project-1",
        agent_run_id="run-1",
        model_key="model-1",
        db_client=object(),
        mode=ShadowCloneMode.V2,
        task_executor=AgentScopeShadowCloneV2TaskExecutor(
            worker_factory=_worker_factory
        ),
        main_agent_synthesizer=_FixedMainAgentSynthesizer(),
    )

    stream = runner.run(
        user_message="Run realtime task.",
        thread_run_id="thread-run-1",
        dynamic_subtasks=[
            {
                "id": "realtime-task-1",
                "role": "writer",
                "task_description": (
                    "Start by outputting marker parts "
                    '["SUB_STREAM_MARKER", "realtime_agent_1", "1780613951313"] '
                    "before using tools. Then write a file."
                ),
            },
        ],
        execute_dynamic_plan=True,
    )
    try:
        opening_event = None
        async for event in stream:
            if event.get("type") == "subagent_activity":
                opening_event = event
                break
        assert opening_event is not None
        assert opening_event["subtask_id"] == "realtime-task-1"
        assert opening_event["message_type"] == "assistant"
        assert opening_event["metadata"]["stream_status"] == "chunk"
        assert opening_event["metadata"]["opening_progress"] is True
        assert "SUB_STREAM_MARKER_realtime_agent_1_1780613951313" in opening_event["content"]["content"]
        tool_started_events = [
            item for item in appended if item.get("event_type") == EventType.TOOL_CALL_STARTED
        ]
        assert tool_started_events
        assert opening_event["sequence"] < tool_started_events[0]["sequence"]
    finally:
        await stream.aclose()


@pytest.mark.asyncio
async def test_runner_peer_message_changes_recipient_subagent_output(monkeypatch) -> None:
    appended = []
    stored_tasks: dict[str, Task] = {}
    stored_team = []
    mailboxes: dict[str, list[MailboxMessage]] = {"comm-agent-b": []}
    executor_inputs = []

    async def _append_event(**kwargs):
        appended.append(kwargs)
        return SimpleNamespace(id=f"event-{len(appended)}", **kwargs)

    async def _store_task(task, *, ttl_seconds, timeout=None):
        stored_tasks[task.id] = task

    async def _store_team(team, *, ttl_seconds, timeout=None):
        stored_team.append(team)

    async def _claim_task(**kwargs):
        task = stored_tasks[kwargs["task_id"]]
        stored_tasks[task.id] = task.model_copy(
            update={
                "status": TaskStatus.IN_PROGRESS,
                "owner_agent": kwargs["agent_name"],
                "lease_expires_at": kwargs["lease_expires_at"],
                "attempt": task.attempt + 1,
                "plan_revision": task.plan_revision + 1,
            }
        )
        return True

    async def _read_task(*, run_id, task_id, timeout=None):
        return stored_tasks.get(task_id)

    async def _complete_task(**kwargs):
        task = stored_tasks[kwargs["task_id"]]
        stored_tasks[task.id] = task.model_copy(
            update={
                "status": TaskStatus.COMPLETED,
                "result_ref": kwargs["result_ref"],
                "plan_revision": task.plan_revision + 1,
            }
        )
        return True

    async def _fail_task(**_kwargs):
        return True

    async def _read_next_messages_with_dead_letters(**kwargs):
        messages = list(mailboxes.get(kwargs["agent_name"], []))
        mailboxes[kwargs["agent_name"]] = []
        return runner_module.agent_loop.mailbox.MailboxReadResult(
            messages=messages,
            dead_lettered=[],
        )

    async def _ack_message(**kwargs):
        return runner_module.agent_loop.mailbox.MailboxAckResult(
            project_id=kwargs["project_id"],
            thread_id=kwargs["thread_id"],
            agent_name=kwargs["agent_name"],
            message_id=kwargs["message_id"],
            acked_by=kwargs["acked_by"],
            acked_at=kwargs["acked_at"],
            read_at=kwargs["acked_at"],
            already_acked=False,
        )

    class _PeerExecutor:
        async def execute(self, **kwargs):
            task = kwargs["task"]
            agent = kwargs["agent"]
            inbox_messages = list(kwargs.get("inbox_messages") or [])
            executor_inputs.append((task.id, agent.agent_name, task.description, [message.text for message in inbox_messages]))
            if agent.agent_name == "comm-agent-a":
                mailboxes.setdefault("comm-agent-b", []).append(
                    MailboxMessage(
                        id="a-to-b-1",
                        run_id="run-1",
                        thread_id="thread-1",
                        project_id="project-1",
                        account_id="account-1",
                        sender="comm-agent-a",
                        recipient="comm-agent-b",
                        kind=MessageKind.TEXT,
                        type=MessageType.TEXT,
                        idempotency_key="a-to-b-marker",
                        summary="marker",
                        text="P2P_FROM_A_TO_B_E2E_MARKER",
                        payload={},
                        created_at="2026-06-03T00:00:00+00:00",
                    )
                )
                return "A_SENT_P2P_FROM_A_TO_B_E2E_MARKER"
            if agent.agent_name == "comm-agent-b" and any(
                "P2P_FROM_A_TO_B_E2E_MARKER" in message.text
                for message in inbox_messages
            ):
                return "P2P_B_CONSUMED_A_MESSAGE_E2E_MARKER"
            return f"{task.id} did not consume peer message"

    class _Synthesis:
        async def synthesize(self, **kwargs):
            outputs = "\n".join(item["output"] for item in kwargs["worker_outputs"])
            return runner_module.MainAgentSynthesisResult(
                output=f"MAIN_SEES_OUTPUTS\n{outputs}",
                next_sequence=kwargs["sequence_start"],
            )

    monkeypatch.setattr(runner_module.event_log, "append_event", _append_event)
    monkeypatch.setattr(runner_module.task_pool, "store_task", _store_task)
    monkeypatch.setattr(runner_module.team_store, "store_team", _store_team)
    monkeypatch.setattr(runner_module.task_pool, "claim_task", _claim_task)
    monkeypatch.setattr(runner_module.task_pool, "read_task", _read_task)
    monkeypatch.setattr(runner_module.task_pool, "complete_task", _complete_task)
    monkeypatch.setattr(runner_module.task_pool, "fail_task", _fail_task)
    monkeypatch.setattr(
        runner_module.agent_loop.mailbox,
        "read_next_messages_with_dead_letters",
        _read_next_messages_with_dead_letters,
    )
    monkeypatch.setattr(runner_module.agent_loop.mailbox, "ack_message", _ack_message)

    runner = ShadowCloneV2Runner(
        thread_id="thread-1",
        project_id="project-1",
        account_id="account-1",
        agent_run_id="run-1",
        model_key="deepseek-v4-pro-max",
        db_client=object(),
        mode=ShadowCloneMode.V2,
        task_executor=_PeerExecutor(),
        main_agent_synthesizer=_Synthesis(),
    )

    events = [
        event
        async for event in runner.run(
            user_message="A sends a private token, then B must consume the inbox-only token without seeing it in the task description.",
            thread_run_id="thread-run-1",
            dynamic_subtasks=[
                {
                    "id": "task-a",
                    "role": "comm agent a",
                    "task_description": "Send the private token to B via mailbox only; do not place it in B's task description.",
                },
                {
                    "id": "task-b",
                    "role": "comm agent b",
                    "task_description": "Read inbox and output the consumed-token marker only if the private mailbox token is present.",
                    "blocked_by": ["task-a"],
                },
            ],
            execute_dynamic_plan=True,
        )
    ]

    task_b_inputs = [item for item in executor_inputs if item[0] == "task-b"]
    assert task_b_inputs == [
        (
            "task-b",
            "comm-agent-b",
            "Read inbox and output the consumed-token marker only if the private mailbox token is present.",
            ["P2P_FROM_A_TO_B_E2E_MARKER"],
        )
    ]
    assert "P2P_FROM_A_TO_B_E2E_MARKER" not in task_b_inputs[0][2]
    assert stored_tasks["task-b"].result_ref == "P2P_B_CONSUMED_A_MESSAGE_E2E_MARKER"
    assert any(
        event.get("type") == "subagent_activity"
        and event.get("subtask_id") == "task-b"
        and "P2P_B_CONSUMED_A_MESSAGE_E2E_MARKER"
        in str(event.get("content"))
        for event in events
    )
    final_assistant = [event for event in events if event.get("type") == "assistant"][-1]
    assert "P2P_B_CONSUMED_A_MESSAGE_E2E_MARKER" in str(final_assistant)
    assert any(
        call["event_type"] == EventType.MAILBOX_ACKED
        and call["payload"]["message_id"] == "a-to-b-1"
        for call in appended
    )


@pytest.mark.asyncio
async def test_runner_task_executor_seam_forwards_inbox_messages() -> None:
    captured = {}

    class _CapturingExecutor:
        async def execute(self, **kwargs):
            captured.update(kwargs)
            return "worker consumed inbox"

    runner = ShadowCloneV2Runner(
        thread_id="thread-1",
        project_id="project-1",
        agent_run_id="run-1",
        model_key="deepseek-v4-pro-high",
        db_client=object(),
        mode=ShadowCloneMode.V2,
        task_executor=_CapturingExecutor(),
    )

    result = await runner._execute_v2_task(
        Task(
            id="task-1",
            run_id="run-1",
            project_id="project-1",
            thread_id="thread-1",
            subject="Consume peer marker",
            description="Use peer message.",
            metadata={"agent_name": "comm-agent-b"},
            created_at="2026-06-03T00:00:00+00:00",
            updated_at="2026-06-03T00:00:00+00:00",
        ),
        AgentIdentity(
            agent_id="comm-agent-b@thread-1",
            agent_name="comm-agent-b",
            thread_id="thread-1",
            project_id="project-1",
            role="recipient",
        ),
        sequence_start=20,
        inbox_messages=[
            SimpleNamespace(
                id="msg-1",
                sender="comm-agent-a",
                recipient="comm-agent-b",
                text="P2P_FROM_A_TO_B_TEST_MARKER",
            )
        ],
    )

    assert result == "worker consumed inbox"
    assert captured["inbox_messages"][0].text == "P2P_FROM_A_TO_B_TEST_MARKER"


@pytest.mark.asyncio
async def test_runner_task_executor_seam_forwards_stream_activity_callback_and_thread_run_id() -> None:
    captured = {}

    async def _on_stream_activity(_activity):
        return None

    class _CapturingExecutor:
        async def execute(self, **kwargs):
            captured.update(kwargs)
            return "worker streamed output"

    runner = ShadowCloneV2Runner(
        thread_id="thread-1",
        project_id="project-1",
        agent_run_id="run-1",
        model_key="deepseek-v4-pro-high",
        db_client=object(),
        mode=ShadowCloneMode.V2,
        task_executor=_CapturingExecutor(),
    )

    result = await runner._execute_v2_task(
        Task(
            id="task-1",
            run_id="run-1",
            project_id="project-1",
            thread_id="thread-1",
            subject="Stream peer marker",
            description="Stream while executing.",
            metadata={"agent_name": "stream-agent"},
            created_at="2026-06-03T00:00:00+00:00",
            updated_at="2026-06-03T00:00:00+00:00",
        ),
        AgentIdentity(
            agent_id="stream-agent@thread-1",
            agent_name="stream-agent",
            thread_id="thread-1",
            project_id="project-1",
            role="streaming worker",
        ),
        sequence_start=20,
        thread_run_id="thread-run-1",
        on_stream_activity=_on_stream_activity,
    )

    assert result == "worker streamed output"
    assert captured["on_stream_activity"] is _on_stream_activity
    assert captured["run_context"].thread_run_id == "thread-run-1"
    assert captured["run_context"].tool_sequence_start == 20


@pytest.mark.asyncio
async def test_runner_uses_injected_task_executor_for_dynamic_plan(monkeypatch) -> None:
    appended = []
    executor_calls = []
    agent_turn_executors = []

    async def _append_event(**kwargs):
        appended.append(kwargs)
        return object()

    async def _noop_store_task(*_args, **_kwargs):
        return None

    async def _noop_store_team(*_args, **_kwargs):
        return None

    async def _run_agent_turn(**kwargs):
        agent_turn_executors.append(kwargs["executor"])
        output = await kwargs["executor"](kwargs["task"], kwargs["agent"])
        return object()

    class _InjectedExecutor:
        async def execute(self, *, task, agent, run_context, inbox_messages=None):
            executor_calls.append(
                (
                    task.id,
                    agent.agent_name,
                    run_context.agent_run_id,
                    run_context.model_key,
                )
            )
            return f"executed:{task.id}:{agent.agent_name}"

    monkeypatch.setattr(runner_module.event_log, "append_event", _append_event)
    monkeypatch.setattr(runner_module.task_pool, "store_task", _noop_store_task)
    monkeypatch.setattr(runner_module.team_store, "store_team", _noop_store_team)
    monkeypatch.setattr(runner_module.agent_loop, "run_agent_turn", _run_agent_turn)

    runner = ShadowCloneV2Runner(
        thread_id="thread-1",
        project_id="project-1",
        agent_run_id="run-1",
        model_key="deepseek-v4-pro-max",
        db_client=object(),
        mode=ShadowCloneMode.V2,
        task_executor=_InjectedExecutor(),
    )

    _events = [
        event
        async for event in runner.run(
            user_message="Summarize and QA this office memo.",
            thread_run_id="thread-run-1",
            dynamic_subtasks=[
                {
                    "id": "summarize",
                    "role": "summarizer",
                    "task_description": "Summarize memo.",
                },
            ],
            execute_dynamic_plan=True,
        )
    ]

    assert len(agent_turn_executors) == 1
    assert executor_calls == [
        ("summarize", "summarizer", "run-1", "deepseek-v4-pro-max")
    ]


@pytest.mark.asyncio
async def test_runner_can_delegate_planning_to_main_agent_tool_planner(
    monkeypatch,
) -> None:
    appended = []
    stored_teams = []
    created_tasks = []
    planner_calls = []

    async def _append_event(**kwargs):
        appended.append(kwargs)
        return object()

    async def _read_team(**_kwargs):
        if not stored_teams:
            return None
        return stored_teams[-1][0]

    async def _store_team(team, *, ttl_seconds, timeout=None):
        stored_teams.append((team, ttl_seconds, timeout))

    async def _create_task(task, *, ttl_seconds, timeout=None):
        created_tasks.append((task, ttl_seconds, timeout))
        return True

    async def _list_tasks(*, run_id, timeout=None):
        return [task for task, _ttl, _timeout in created_tasks]

    def _forbidden_planner(**_kwargs):
        raise AssertionError("runner planner fallback should not be called")

    monkeypatch.setattr(runner_module.event_log, "append_event", _append_event)
    monkeypatch.setattr(runner_module.team_store, "read_team", _read_team)
    monkeypatch.setattr(runner_module.team_store, "store_team", _store_team)
    monkeypatch.setattr(
        runner_module.task_pool, "create_task", _create_task, raising=False
    )
    monkeypatch.setattr(runner_module.task_pool, "list_tasks", _list_tasks)
    monkeypatch.setattr(
        runner_module, "normalize_dynamic_team_plan", _forbidden_planner
    )

    class _ToolDrivenPlanner:
        async def plan(self, **kwargs):
            planner_calls.append(kwargs)
            team_result = await kwargs["tools"].team_create(
                members=[{"agent_name": "researcher", "role": "research analyst"}]
            )
            task_result = await kwargs["tools"].task_create(
                task_id="research",
                subject="Research",
                description="Research the user's request.",
                metadata={"agent_name": "researcher"},
            )
            return runner_module.MainAgentToolPlanResult(
                team=team_result.team,
                tasks=[task_result.task],
                next_sequence=task_result.next_sequence,
                execute_dynamic_plan=False,
            )

    runner = ShadowCloneV2Runner(
        thread_id="thread-1",
        project_id="project-1",
        account_id="account-1",
        agent_run_id="run-1",
        model_key="frontend-selected-model",
        db_client=object(),
        mode=ShadowCloneMode.V2,
        main_agent_planner=_ToolDrivenPlanner(),
    )

    events = [
        event
        async for event in runner.run(
            user_message="Research competitors.",
            thread_run_id="thread-run-1",
        )
    ]

    assert len(planner_calls) == 1
    assert planner_calls[0]["user_message"] == "Research competitors."
    assert planner_calls[0]["model_key"] == "frontend-selected-model"
    assert stored_teams[0][0].members[0].agent_name == "researcher"
    assert created_tasks[0][0].metadata == {"agent_name": "researcher"}
    assert [call["event_type"] for call in appended] == [
        EventType.RUN_STARTED,
        EventType.TEAM_CREATED,
        EventType.AGENT_SPAWNED,
        EventType.TASK_CREATED,
    ]
    assert appended[1]["activity_owner"] == "agent:facilitator@thread-1"
    assert events[-1]["type"] == "shadow_clone_v2_plan_created"
    assert events[-1]["subagent_count"] == 1
    assert events[-1]["task_count"] == 1


@pytest.mark.asyncio
async def test_runner_does_not_duplicate_existing_agent_idle_finalization(
    monkeypatch,
) -> None:
    appended = []
    stored_team = TeamConfig(
        team_id="shadow-clone-v2:project-1:thread-1",
        thread_id="thread-1",
        project_id="project-1",
        facilitator_id="facilitator@thread-1",
        members=[
            AgentIdentity(
                agent_id="researcher@thread-1",
                agent_name="researcher",
                thread_id="thread-1",
                project_id="project-1",
                role="research analyst",
                status=AgentLifecycleStatus.WORKING,
            )
        ],
    )
    completed_task = runner_module.Task(
        id="research",
        run_id="run-1",
        thread_id="thread-1",
        subject="Research",
        description="Research the user's request.",
        status=TaskStatus.COMPLETED,
        owner_agent="researcher",
        result_ref="research complete",
        metadata={"agent_name": "researcher"},
        created_at="2026-05-31T12:00:00+00:00",
        updated_at="2026-05-31T12:01:00+00:00",
    )

    async def _append_event(**kwargs):
        appended.append(kwargs)
        return object()

    async def _read_events(run_id, start="-", limit=None):
        assert run_id == "run-1"
        return [
            EventRecord(
                id="event-idle-1",
                run_id="run-1",
                thread_id="thread-1",
                project_id="project-1",
                sequence=12,
                type=EventType.AGENT_IDLE,
                activity_owner="agent:researcher",
                payload={
                    "agent_name": "researcher",
                    "idle_since": "2026-05-31T12:02:00+00:00",
                    "idle_expires_at": "2026-05-31T12:22:00+00:00",
                },
                created_at="2026-05-31T12:02:00+00:00",
            )
        ]

    async def _store_team(team, *, ttl_seconds, timeout=None):
        nonlocal stored_team
        stored_team = team

    monkeypatch.setattr(runner_module.event_log, "append_event", _append_event)
    monkeypatch.setattr(runner_module.event_log, "read_events", _read_events)
    monkeypatch.setattr(runner_module.team_store, "store_team", _store_team)

    runner = ShadowCloneV2Runner(
        thread_id="thread-1",
        project_id="project-1",
        account_id="account-1",
        agent_run_id="run-1",
        model_key="frontend-selected-model",
        db_client=object(),
        mode=ShadowCloneMode.V2,
    )

    updated_team, next_sequence = await runner._mark_completed_task_owners_idle(
        team=stored_team,
        tasks=[completed_task],
        sequence_start=30,
    )

    assert appended == []
    assert updated_team is stored_team
    assert next_sequence == 30
    assert stored_team.members[0].status == AgentLifecycleStatus.WORKING


@pytest.mark.asyncio
async def test_runner_marks_completed_owner_idle_after_prior_idle_then_wake(
    monkeypatch,
) -> None:
    appended = []
    stored_team = TeamConfig(
        team_id="shadow-clone-v2:project-1:thread-1",
        thread_id="thread-1",
        project_id="project-1",
        facilitator_id="facilitator@thread-1",
        members=[
            AgentIdentity(
                agent_id="researcher@thread-1",
                agent_name="researcher",
                thread_id="thread-1",
                project_id="project-1",
                role="research analyst",
                status=AgentLifecycleStatus.WORKING,
            )
        ],
    )
    completed_task = runner_module.Task(
        id="followup-research",
        run_id="run-1",
        thread_id="thread-1",
        subject="Follow-up research",
        description="Research the follow-up request.",
        status=TaskStatus.COMPLETED,
        owner_agent="researcher",
        result_ref="follow-up research complete",
        metadata={"agent_name": "researcher"},
        created_at="2026-05-31T12:10:00+00:00",
        updated_at="2026-05-31T12:11:00+00:00",
    )

    async def _append_event(**kwargs):
        appended.append(kwargs)
        return object()

    async def _read_events(run_id, start="-", limit=None):
        assert run_id == "run-1"
        return [
            EventRecord(
                id="event-idle-1",
                run_id="run-1",
                thread_id="thread-1",
                project_id="project-1",
                sequence=12,
                type=EventType.AGENT_IDLE,
                activity_owner="agent:researcher",
                payload={
                    "agent_name": "researcher",
                    "idle_since": "2026-05-31T12:02:00+00:00",
                    "idle_expires_at": "2026-05-31T12:22:00+00:00",
                },
                created_at="2026-05-31T12:02:00+00:00",
            ),
            EventRecord(
                id="event-wake-1",
                run_id="run-1",
                thread_id="thread-1",
                project_id="project-1",
                sequence=13,
                type=EventType.AGENT_WAKE,
                activity_owner="facilitator",
                payload={"agent_name": "researcher"},
                created_at="2026-05-31T12:10:00+00:00",
            ),
        ]

    async def _store_team(team, *, ttl_seconds, timeout=None):
        nonlocal stored_team
        stored_team = team

    monkeypatch.setattr(runner_module.event_log, "append_event", _append_event)
    monkeypatch.setattr(runner_module.event_log, "read_events", _read_events)
    monkeypatch.setattr(runner_module.team_store, "store_team", _store_team)

    runner = ShadowCloneV2Runner(
        thread_id="thread-1",
        project_id="project-1",
        account_id="account-1",
        agent_run_id="run-1",
        model_key="frontend-selected-model",
        db_client=object(),
        mode=ShadowCloneMode.V2,
    )

    updated_team, next_sequence = await runner._mark_completed_task_owners_idle(
        team=stored_team,
        tasks=[completed_task],
        sequence_start=30,
    )

    assert len(appended) == 1
    assert appended[0]["event_type"] == EventType.AGENT_IDLE
    assert appended[0]["payload"]["agent_name"] == "researcher"
    assert appended[0]["payload"]["task_id"] == "followup-research"
    assert next_sequence == 31
    assert updated_team.members[0].status == AgentLifecycleStatus.IDLE
    assert stored_team.members[0].idle_expires_at


@pytest.mark.asyncio
async def test_runner_marks_same_task_owner_idle_after_prior_idle_then_wake(
    monkeypatch,
) -> None:
    appended = []
    stored_team = TeamConfig(
        team_id="shadow-clone-v2:project-1:thread-1",
        thread_id="thread-1",
        project_id="project-1",
        facilitator_id="facilitator@thread-1",
        members=[
            AgentIdentity(
                agent_id="researcher@thread-1",
                agent_name="researcher",
                thread_id="thread-1",
                project_id="project-1",
                role="research analyst",
                status=AgentLifecycleStatus.WORKING,
            )
        ],
    )
    completed_task = runner_module.Task(
        id="research",
        run_id="run-1",
        thread_id="thread-1",
        subject="Research",
        description="Research the reworked request.",
        status=TaskStatus.COMPLETED,
        owner_agent="researcher",
        result_ref="research rework complete",
        metadata={"agent_name": "researcher"},
        created_at="2026-05-31T12:10:00+00:00",
        updated_at="2026-05-31T12:11:00+00:00",
    )

    async def _append_event(**kwargs):
        appended.append(kwargs)
        return object()

    async def _read_events(run_id, start="-", limit=None):
        assert run_id == "run-1"
        return [
            EventRecord(
                id="event-idle-1",
                run_id="run-1",
                thread_id="thread-1",
                project_id="project-1",
                sequence=12,
                type=EventType.AGENT_IDLE,
                activity_owner="agent:researcher",
                payload={
                    "agent_name": "researcher",
                    "task_id": "research",
                    "idle_since": "2026-05-31T12:02:00+00:00",
                    "idle_expires_at": "2026-05-31T12:22:00+00:00",
                },
                created_at="2026-05-31T12:02:00+00:00",
            ),
            EventRecord(
                id="event-wake-1",
                run_id="run-1",
                thread_id="thread-1",
                project_id="project-1",
                sequence=13,
                type=EventType.AGENT_WAKE,
                activity_owner="facilitator",
                payload={"agent_name": "researcher"},
                created_at="2026-05-31T12:10:00+00:00",
            ),
        ]

    async def _store_team(team, *, ttl_seconds, timeout=None):
        nonlocal stored_team
        stored_team = team

    monkeypatch.setattr(runner_module.event_log, "append_event", _append_event)
    monkeypatch.setattr(runner_module.event_log, "read_events", _read_events)
    monkeypatch.setattr(runner_module.team_store, "store_team", _store_team)

    runner = ShadowCloneV2Runner(
        thread_id="thread-1",
        project_id="project-1",
        account_id="account-1",
        agent_run_id="run-1",
        model_key="frontend-selected-model",
        db_client=object(),
        mode=ShadowCloneMode.V2,
    )

    updated_team, next_sequence = await runner._mark_completed_task_owners_idle(
        team=stored_team,
        tasks=[completed_task],
        sequence_start=30,
    )

    assert len(appended) == 1
    assert appended[0]["event_type"] == EventType.AGENT_IDLE
    assert appended[0]["payload"]["agent_name"] == "researcher"
    assert appended[0]["payload"]["task_id"] == "research"
    assert next_sequence == 31
    assert updated_team.members[0].status == AgentLifecycleStatus.IDLE
    assert stored_team.members[0].idle_expires_at


@pytest.mark.asyncio
async def test_runner_does_not_mark_shutdown_completed_owner_idle(
    monkeypatch,
) -> None:
    appended = []
    stored_team = TeamConfig(
        team_id="shadow-clone-v2:project-1:thread-1",
        thread_id="thread-1",
        project_id="project-1",
        facilitator_id="facilitator@thread-1",
        members=[
            AgentIdentity(
                agent_id="researcher@thread-1",
                agent_name="researcher",
                thread_id="thread-1",
                project_id="project-1",
                role="research analyst",
                status=AgentLifecycleStatus.WORKING,
            )
        ],
    )
    completed_task = runner_module.Task(
        id="research",
        run_id="run-1",
        thread_id="thread-1",
        subject="Research",
        description="Research already completed before shutdown.",
        status=TaskStatus.COMPLETED,
        owner_agent="researcher",
        result_ref="research complete",
        metadata={"agent_name": "researcher"},
        created_at="2026-05-31T12:00:00+00:00",
        updated_at="2026-05-31T12:01:00+00:00",
    )

    async def _append_event(**kwargs):
        appended.append(kwargs)
        return object()

    async def _read_events(run_id, start="-", limit=None):
        assert run_id == "run-1"
        return [
            EventRecord(
                id="event-shutdown-1",
                run_id="run-1",
                thread_id="thread-1",
                project_id="project-1",
                sequence=20,
                type=EventType.AGENT_SHUTDOWN,
                activity_owner="facilitator",
                payload={"agent_name": "researcher"},
                created_at="2026-05-31T12:05:00+00:00",
            )
        ]

    async def _store_team(team, *, ttl_seconds, timeout=None):
        nonlocal stored_team
        stored_team = team

    monkeypatch.setattr(runner_module.event_log, "append_event", _append_event)
    monkeypatch.setattr(runner_module.event_log, "read_events", _read_events)
    monkeypatch.setattr(runner_module.team_store, "store_team", _store_team)

    runner = ShadowCloneV2Runner(
        thread_id="thread-1",
        project_id="project-1",
        account_id="account-1",
        agent_run_id="run-1",
        model_key="frontend-selected-model",
        db_client=object(),
        mode=ShadowCloneMode.V2,
    )

    updated_team, next_sequence = await runner._mark_completed_task_owners_idle(
        team=stored_team,
        tasks=[completed_task],
        sequence_start=30,
    )

    assert appended == []
    assert updated_team is stored_team
    assert next_sequence == 30
    assert stored_team.members[0].status == AgentLifecycleStatus.WORKING


@pytest.mark.asyncio
async def test_runner_marks_tool_completed_task_owner_idle_before_run_completed(
    monkeypatch,
) -> None:
    appended = []
    stored_team = None
    stored_tasks = []

    async def _append_event(**kwargs):
        appended.append(kwargs)
        return object()

    async def _read_team(**_kwargs):
        return stored_team

    async def _store_team(team, *, ttl_seconds, timeout=None):
        nonlocal stored_team
        stored_team = team

    async def _create_task(task, *, ttl_seconds, timeout=None):
        stored_tasks.append(task)
        return True

    async def _read_task(*, run_id, task_id, timeout=None):
        for task in stored_tasks:
            if task.run_id == run_id and task.id == task_id:
                return task
        return None

    async def _update_task(task, *, expected_plan_revision, ttl_seconds, timeout=None):
        for index, stored_task in enumerate(stored_tasks):
            if stored_task.id == task.id:
                assert stored_task.plan_revision == expected_plan_revision
                stored_tasks[index] = task
                return True
        return False

    async def _list_tasks(*, run_id, timeout=None):
        return [task for task in stored_tasks if task.run_id == run_id]

    class _ToolCompletingPlanner:
        async def plan(self, **kwargs):
            team_result = await kwargs["tools"].team_create(
                members=[{"agent_name": "researcher", "role": "research analyst"}]
            )
            created_task = await kwargs["tools"].task_create(
                task_id="research",
                subject="Research",
                description="Research the user's request.",
                metadata={"agent_name": "researcher"},
            )
            await kwargs["tools"].task_update(
                task_id="research",
                expected_version=created_task.task.plan_revision,
                status=TaskStatus.IN_PROGRESS,
                owner_agent="researcher",
            )
            completed_task = await kwargs["tools"].task_update(
                task_id="research",
                expected_version=created_task.task.plan_revision + 1,
                status=TaskStatus.COMPLETED,
                result_ref="research complete",
            )
            return runner_module.MainAgentToolPlanResult(
                team=team_result.team,
                tasks=[completed_task.task],
                next_sequence=kwargs["tools"].next_sequence,
                execute_dynamic_plan=True,
            )

    monkeypatch.setattr(runner_module.event_log, "append_event", _append_event)
    monkeypatch.setattr(runner_module.team_store, "read_team", _read_team)
    monkeypatch.setattr(runner_module.team_store, "store_team", _store_team)
    monkeypatch.setattr(runner_module.task_pool, "create_task", _create_task)
    monkeypatch.setattr(runner_module.task_pool, "read_task", _read_task)
    monkeypatch.setattr(runner_module.task_pool, "update_task", _update_task)
    monkeypatch.setattr(runner_module.task_pool, "list_tasks", _list_tasks)

    runner = ShadowCloneV2Runner(
        thread_id="thread-1",
        project_id="project-1",
        account_id="account-1",
        agent_run_id="run-1",
        model_key="frontend-selected-model",
        db_client=object(),
        mode=ShadowCloneMode.V2,
        main_agent_planner=_ToolCompletingPlanner(),
        main_agent_synthesizer=_FixedMainAgentSynthesizer(),
    )

    events = [
        event
        async for event in runner.run(
            user_message="Research competitors.",
            thread_run_id="thread-run-1",
        )
    ]

    event_types = [call["event_type"] for call in appended]
    assert event_types[-2:] == [EventType.AGENT_IDLE, EventType.RUN_COMPLETED]
    idle_event = appended[-2]
    assert idle_event["activity_owner"] == "agent:researcher"
    assert idle_event["payload"]["agent_name"] == "researcher"
    assert idle_event["payload"]["idle_since"]
    assert idle_event["payload"]["idle_expires_at"]
    assert stored_team is not None
    assert stored_team.members[0].status == AgentLifecycleStatus.IDLE
    assert stored_team.members[0].idle_expires_at
    assert events[-1]["type"] == "shadow_clone_v2_execution_completed"


@pytest.mark.asyncio
async def test_runner_default_planning_path_uses_agent_scope_tool_planner(
    monkeypatch,
) -> None:
    appended = []
    stored_teams = []
    created_tasks = []
    planner_calls = []

    async def _append_event(**kwargs):
        appended.append(kwargs)
        return object()

    async def _read_team(**_kwargs):
        if not stored_teams:
            return None
        return stored_teams[-1][0]

    async def _store_team(team, *, ttl_seconds, timeout=None):
        stored_teams.append((team, ttl_seconds, timeout))

    async def _create_task(task, *, ttl_seconds, timeout=None):
        created_tasks.append((task, ttl_seconds, timeout))
        return True

    async def _list_tasks(*, run_id, timeout=None):
        return [task for task, _ttl, _timeout in created_tasks]

    def _forbidden_runner_planner(**_kwargs):
        raise AssertionError("default V2 planning must not use runner heuristics")

    class _DefaultToolPlanner:
        async def plan(self, **kwargs):
            planner_calls.append(kwargs)
            team_result = await kwargs["tools"].team_create(
                members=[
                    {"agent_name": "researcher", "role": "research analyst"},
                    {"agent_name": "reviewer", "role": "quality reviewer"},
                ]
            )
            first_task = await kwargs["tools"].task_create(
                task_id="research",
                subject="Research",
                description="Research the user's request.",
                metadata={"agent_name": "researcher"},
            )
            second_task = await kwargs["tools"].task_create(
                task_id="review",
                subject="Review",
                description="Review the research findings.",
                metadata={"agent_name": "reviewer"},
            )
            return runner_module.MainAgentToolPlanResult(
                team=team_result.team,
                tasks=[first_task.task, second_task.task],
                next_sequence=second_task.next_sequence,
                execute_dynamic_plan=False,
            )

    monkeypatch.setattr(runner_module.event_log, "append_event", _append_event)
    monkeypatch.setattr(runner_module.team_store, "read_team", _read_team)
    monkeypatch.setattr(runner_module.team_store, "store_team", _store_team)
    monkeypatch.setattr(
        runner_module.task_pool, "create_task", _create_task, raising=False
    )
    monkeypatch.setattr(runner_module.task_pool, "list_tasks", _list_tasks)
    monkeypatch.setattr(
        runner_module, "normalize_dynamic_team_plan", _forbidden_runner_planner
    )
    monkeypatch.setattr(
        runner_module,
        "AgentScopeShadowCloneV2MainAgentPlanner",
        lambda: _DefaultToolPlanner(),
        raising=False,
    )

    runner = ShadowCloneV2Runner(
        thread_id="thread-1",
        project_id="project-1",
        account_id="account-1",
        agent_run_id="run-1",
        model_key="frontend-selected-model",
        db_client=object(),
        mode=ShadowCloneMode.V2,
    )

    events = [
        event
        async for event in runner.run(
            user_message="Research competitors.",
            thread_run_id="thread-run-1",
        )
    ]

    assert len(planner_calls) == 1
    assert planner_calls[0]["model_key"] == "frontend-selected-model"
    assert planner_calls[0]["tools"].context.sequence_start == 2
    assert planner_calls[0]["tools"].context.account_id == "account-1"
    assert [member.agent_name for member in stored_teams[0][0].members] == [
        "researcher",
        "reviewer",
    ]
    assert [task.id for task, _ttl, _timeout in created_tasks] == [
        "research",
        "review",
    ]
    assert [call["event_type"] for call in appended] == [
        EventType.RUN_STARTED,
        EventType.TEAM_CREATED,
        EventType.AGENT_SPAWNED,
        EventType.AGENT_SPAWNED,
        EventType.TASK_CREATED,
        EventType.TASK_CREATED,
    ]
    assert events[-1]["type"] == "shadow_clone_v2_plan_created"
    assert events[-1]["subagent_count"] == 2
    assert events[-1]["task_count"] == 2


@pytest.mark.asyncio
async def test_runner_emits_user_visible_main_progress_messages_during_v2_planning(
    monkeypatch,
) -> None:
    async def _append_event(**_kwargs):
        return object()

    async def _noop_store_task(*_args, **_kwargs):
        return None

    async def _noop_store_team(*_args, **_kwargs):
        return None

    monkeypatch.setattr(runner_module.event_log, "append_event", _append_event)
    monkeypatch.setattr(runner_module.task_pool, "store_task", _noop_store_task)
    monkeypatch.setattr(runner_module.team_store, "store_team", _noop_store_team)

    runner = ShadowCloneV2Runner(
        thread_id="thread-1",
        project_id="project-1",
        agent_run_id="run-1",
        model_key="model-1",
        db_client=object(),
        mode=ShadowCloneMode.V2,
    )

    events = [
        event
        async for event in runner.run(
            user_message="Let five teammates write files.",
            thread_run_id="thread-run-1",
            dynamic_subtasks=[
                {
                    "id": "writer-1",
                    "role": "markdown writer one",
                    "task_description": "Write one markdown file.",
                },
                {
                    "id": "writer-2",
                    "role": "markdown writer two",
                    "task_description": "Write another markdown file.",
                },
            ],
            execute_dynamic_plan=False,
        )
    ]

    progress_events = [
        event
        for event in events
        if event.get("type") == "assistant"
        and json.loads(event.get("metadata") or "{}").get("message_kind")
        == "orchestration_progress"
    ]

    assert len(progress_events) >= 2
    assert progress_events[0]["message_id"] == "shadow-clone-v2-progress-run-1-started"
    assert progress_events[0]["is_llm_message"] is False
    first_content = json.loads(progress_events[0]["content"])
    assert "[系统进度]" in first_content["content"]
    first_metadata = json.loads(progress_events[0]["metadata"])
    assert first_metadata["shadow_clone_system_progress"] is True
    assert first_metadata["agent_run_id"] == "run-1"
    assert first_metadata["stream_status"] == "complete"
    assert first_metadata["ui_phase"] == "planning"

    plan_progress_metadata = json.loads(progress_events[-1]["metadata"])
    plan_progress_content = json.loads(progress_events[-1]["content"])["content"]
    assert plan_progress_metadata["phase_reason"] == "shadow_clone_v2_plan_progress"
    assert plan_progress_metadata["subagent_count"] == 2
    assert plan_progress_metadata["task_count"] == 2
    assert "2" in plan_progress_content


@pytest.mark.asyncio
async def test_runner_emits_main_agent_planning_natural_language_when_planner_returns_output(
    monkeypatch,
) -> None:
    stored_team = None
    created_tasks = []

    async def _append_event(**_kwargs):
        return object()

    async def _read_team(**_kwargs):
        return stored_team

    async def _store_team(team, *, ttl_seconds, timeout=None):
        nonlocal stored_team
        stored_team = team

    async def _create_task(task, *, ttl_seconds, timeout=None):
        created_tasks.append(task)
        return True

    async def _list_tasks(*, run_id, timeout=None):
        return [task for task in created_tasks if task.run_id == run_id]

    class _NaturalLanguagePlanner:
        async def plan(self, **kwargs):
            team_result = await kwargs["tools"].team_create(
                members=[{"agent_name": "writer-agent-1", "role": "markdown writer"}]
            )
            task_result = await kwargs["tools"].task_create(
                task_id="writer-task-1",
                subject="Write markdown",
                description="Write one markdown file.",
                metadata={"agent_name": "writer-agent-1"},
            )
            return runner_module.MainAgentToolPlanResult(
                team=team_result.team,
                tasks=[task_result.task],
                next_sequence=task_result.next_sequence,
                execute_dynamic_plan=False,
                output=(
                    "MAIN_PLANNING_NATURAL_LANGUAGE_OK I will ask writer-agent-1 "
                    "to create the requested markdown file."
                ),
            )

    monkeypatch.setattr(runner_module.event_log, "append_event", _append_event)
    monkeypatch.setattr(runner_module.team_store, "read_team", _read_team)
    monkeypatch.setattr(runner_module.team_store, "store_team", _store_team)
    monkeypatch.setattr(runner_module.task_pool, "create_task", _create_task)
    monkeypatch.setattr(runner_module.task_pool, "list_tasks", _list_tasks)

    runner = ShadowCloneV2Runner(
        thread_id="thread-1",
        project_id="project-1",
        agent_run_id="run-1",
        model_key="model-1",
        db_client=object(),
        mode=ShadowCloneMode.V2,
        main_agent_planner=_NaturalLanguagePlanner(),
    )

    events = [
        event
        async for event in runner.run(
            user_message="Ask one teammate to write a markdown file.",
            thread_run_id="thread-run-1",
        )
    ]

    planning_events = [
        event
        for event in events
        if event.get("type") == "assistant"
        and event.get("is_llm_message") is True
        and "MAIN_PLANNING_NATURAL_LANGUAGE_OK"
        in json.loads(event.get("content") or "{}").get("content", "")
    ]

    assert len(planning_events) == 1
    assert planning_events[0]["project_id"] == "project-1"
    metadata = json.loads(planning_events[0]["metadata"])
    assert metadata["stream_status"] == "complete"
    assert metadata["activity_owner"] == "main_agent"
    assert metadata["ui_phase"] == "planning"
    assert metadata["phase_reason"] == "shadow_clone_v2_main_agent_planning_output"


@pytest.mark.asyncio
async def test_runner_emits_structured_failure_when_main_agent_planning_fails(
    monkeypatch,
) -> None:
    appended = []
    stored_team = None

    async def _append_event(**kwargs):
        appended.append(kwargs)
        return object()

    async def _read_team(**_kwargs):
        return stored_team

    async def _store_team(team, *, ttl_seconds, timeout=None):
        nonlocal stored_team
        stored_team = team

    class _FailingPlanner:
        async def plan(self, **kwargs):
            await kwargs["tools"].team_create(
                members=[{"agent_name": "researcher", "role": "research analyst"}]
            )
            raise RuntimeError("planner did not create tasks")

    monkeypatch.setattr(runner_module.event_log, "append_event", _append_event)
    monkeypatch.setattr(runner_module.team_store, "read_team", _read_team)
    monkeypatch.setattr(runner_module.team_store, "store_team", _store_team)

    runner = ShadowCloneV2Runner(
        thread_id="thread-1",
        project_id="project-1",
        agent_run_id="run-1",
        model_key="frontend-selected-model",
        db_client=object(),
        mode=ShadowCloneMode.V2,
        main_agent_planner=_FailingPlanner(),
    )

    events = [
        event
        async for event in runner.run(
            user_message="Research competitors.",
            thread_run_id="thread-run-1",
        )
    ]

    assert events[-1]["type"] == "shadow_clone_v2_planning_failed"
    assert events[-1]["status"] == "failed"
    assert events[-1]["error"] == "planner did not create tasks"
    assert [call["event_type"] for call in appended] == [
        EventType.RUN_STARTED,
        EventType.TEAM_CREATED,
        EventType.AGENT_SPAWNED,
        EventType.RUN_FAILED,
    ]
    assert appended[-1]["sequence"] == 4
    assert appended[-1]["payload"]["failure_stage"] == "planning"
    assert appended[-1]["payload"]["error_type"] == "RuntimeError"


@pytest.mark.asyncio
async def test_runner_rejects_planner_result_not_backed_by_tool_state(
    monkeypatch,
) -> None:
    appended = []
    stored_team = None
    stored_tasks = []

    async def _append_event(**kwargs):
        appended.append(kwargs)
        return object()

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

    class _MalformedPlanner:
        async def plan(self, **kwargs):
            team_result = await kwargs["tools"].team_create(
                members=[{"agent_name": "researcher", "role": "research analyst"}]
            )
            created_task = await kwargs["tools"].task_create(
                task_id="research",
                subject="Research",
                description="Research the user's request.",
                metadata={"agent_name": "researcher"},
            )
            unpersisted_task = created_task.task.model_copy(
                update={"id": "unpersisted"}
            )
            return runner_module.MainAgentToolPlanResult(
                team=team_result.team,
                tasks=[unpersisted_task],
                next_sequence=2,
                execute_dynamic_plan=False,
            )

    monkeypatch.setattr(runner_module.event_log, "append_event", _append_event)
    monkeypatch.setattr(runner_module.team_store, "read_team", _read_team)
    monkeypatch.setattr(runner_module.team_store, "store_team", _store_team)
    monkeypatch.setattr(runner_module.task_pool, "create_task", _create_task)
    monkeypatch.setattr(runner_module.task_pool, "list_tasks", _list_tasks)

    runner = ShadowCloneV2Runner(
        thread_id="thread-1",
        project_id="project-1",
        agent_run_id="run-1",
        model_key="frontend-selected-model",
        db_client=object(),
        mode=ShadowCloneMode.V2,
        main_agent_planner=_MalformedPlanner(),
    )

    events = [
        event
        async for event in runner.run(
            user_message="Research competitors.",
            thread_run_id="thread-run-1",
        )
    ]

    assert events[-1]["type"] == "shadow_clone_v2_planning_failed"
    assert "sequence" in events[-1]["error"] or "durable" in events[-1]["error"]
    assert appended[-1]["event_type"] == EventType.RUN_FAILED
    assert appended[-1]["sequence"] == 5


@pytest.mark.asyncio
async def test_runner_rejects_planner_returned_task_not_in_durable_state(
    monkeypatch,
) -> None:
    appended = []
    stored_team = None
    stored_tasks = []

    async def _append_event(**kwargs):
        appended.append(kwargs)
        return object()

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

    class _MalformedPlanner:
        async def plan(self, **kwargs):
            team_result = await kwargs["tools"].team_create(
                members=[{"agent_name": "researcher", "role": "research analyst"}]
            )
            created_task = await kwargs["tools"].task_create(
                task_id="research",
                subject="Research",
                description="Research the user's request.",
                metadata={"agent_name": "researcher"},
            )
            unpersisted_task = created_task.task.model_copy(
                update={"id": "unpersisted"}
            )
            return runner_module.MainAgentToolPlanResult(
                team=team_result.team,
                tasks=[unpersisted_task],
                next_sequence=kwargs["tools"].next_sequence,
                execute_dynamic_plan=False,
            )

    monkeypatch.setattr(runner_module.event_log, "append_event", _append_event)
    monkeypatch.setattr(runner_module.team_store, "read_team", _read_team)
    monkeypatch.setattr(runner_module.team_store, "store_team", _store_team)
    monkeypatch.setattr(runner_module.task_pool, "create_task", _create_task)
    monkeypatch.setattr(runner_module.task_pool, "list_tasks", _list_tasks)

    runner = ShadowCloneV2Runner(
        thread_id="thread-1",
        project_id="project-1",
        agent_run_id="run-1",
        model_key="frontend-selected-model",
        db_client=object(),
        mode=ShadowCloneMode.V2,
        main_agent_planner=_MalformedPlanner(),
    )

    events = [
        event
        async for event in runner.run(
            user_message="Research competitors.",
            thread_run_id="thread-run-1",
        )
    ]

    assert events[-1]["type"] == "shadow_clone_v2_planning_failed"
    assert "durable task set mismatch" in events[-1]["error"]
    assert appended[-1]["event_type"] == EventType.RUN_FAILED
    assert appended[-1]["sequence"] == 5
