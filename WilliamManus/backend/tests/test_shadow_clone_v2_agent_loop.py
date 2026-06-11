from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from agentscope_integration.shadow_clone_v2 import task_pool
from agentscope_integration.shadow_clone_v2.models import (
    AgentIdentity,
    AgentLifecycleStatus,
    EventType,
    MailboxMessage,
    MessageKind,
    MessageType,
    Task,
    TaskStatus,
)


@pytest.fixture(autouse=True)
def _default_empty_mailbox(monkeypatch):
    import agentscope_integration.shadow_clone_v2.agent_loop as agent_loop_module

    async def fake_read_next_messages(**_kwargs):
        return []

    monkeypatch.setattr(
        agent_loop_module.mailbox, "read_next_messages", fake_read_next_messages
    )


@pytest.mark.asyncio
async def test_agent_loop_claims_executes_completes_and_enters_idle(
    monkeypatch,
) -> None:
    from agentscope_integration.shadow_clone_v2.agent_loop import run_agent_turn
    import agentscope_integration.shadow_clone_v2.agent_loop as agent_loop_module

    calls: list[tuple[str, object]] = []
    appended_events: list[dict] = []
    now = datetime(2026, 5, 31, 12, 0, 0, tzinfo=timezone.utc)
    finished_at = datetime(2026, 5, 31, 12, 10, 0, tzinfo=timezone.utc)
    agent = AgentIdentity(
        agent_id="reviewer@thread-1",
        agent_name="reviewer",
        thread_id="thread-1",
        project_id="project-1",
        role="reviewer",
        status=AgentLifecycleStatus.STARTING,
    )
    task = Task(
        id="task-1",
        run_id="run-1",
        thread_id="thread-1",
        subject="Review",
        description="Review the result.",
        created_at=now.isoformat(),
        updated_at=now.isoformat(),
    )

    claimed_task = task.model_copy(
        update={
            "attempt": 1,
            "plan_revision": 2,
            "lease_expires_at": "2026-05-31T12:05:00+00:00",
        }
    )

    async def fake_claim_task(**kwargs):
        calls.append(("claim_task", kwargs))
        return True

    async def fake_read_task(**kwargs):
        assert kwargs["task_id"] == "task-1"
        return claimed_task

    async def fake_complete_task(**kwargs):
        calls.append(("complete_task", kwargs))
        return True

    async def fake_append_event(**kwargs):
        appended_events.append(kwargs)
        return SimpleNamespace(id=f"event-{len(appended_events)}", **kwargs)

    def fake_mark_agent_idle(next_agent, *, now):
        calls.append(("mark_agent_idle", {"agent": next_agent, "now": now}))
        return next_agent.model_copy(
            update={
                "status": AgentLifecycleStatus.IDLE,
                "idle_since": now.isoformat(),
                "idle_expires_at": "2026-05-31T12:30:00+00:00",
            }
        )

    async def fake_executor(next_task, next_agent):
        calls.append(("executor", {"task": next_task, "agent": next_agent}))
        return "review complete"

    monkeypatch.setattr(agent_loop_module.task_pool, "claim_task", fake_claim_task)
    monkeypatch.setattr(agent_loop_module.task_pool, "read_task", fake_read_task)
    monkeypatch.setattr(
        agent_loop_module.task_pool, "complete_task", fake_complete_task
    )
    monkeypatch.setattr(agent_loop_module.event_log, "append_event", fake_append_event)
    monkeypatch.setattr(
        agent_loop_module.agent_registry, "mark_agent_idle", fake_mark_agent_idle
    )

    result = await run_agent_turn(
        run_id="run-1",
        project_id="project-1",
        agent=agent,
        task=task,
        lease_token="lease-1",
        lease_expires_at="2026-05-31T12:05:00+00:00",
        now=now,
        executor=fake_executor,
        sequence_start=7,
        clock=lambda: finished_at,
    )

    call_names = [name for name, _payload in calls]
    assert call_names == ["claim_task", "executor", "complete_task", "mark_agent_idle"]
    assert result.agent.status == AgentLifecycleStatus.IDLE
    assert result.agent.idle_expires_at == "2026-05-31T12:30:00+00:00"
    assert result.output == "review complete"
    assert result.next_sequence == 10
    complete_call = calls[2][1]
    idle_call = calls[3][1]
    assert complete_call["updated_at"] == "2026-05-31T12:10:00+00:00"
    assert idle_call["now"] == finished_at
    assert [event["event_type"] for event in appended_events] == [
        EventType.TASK_CLAIMED,
        EventType.TASK_COMPLETED,
        EventType.AGENT_IDLE,
    ]
    assert appended_events[0]["payload"]["task_id"] == "task-1"
    assert appended_events[0]["payload"]["attempt"] == 1
    assert appended_events[0]["payload"]["previous_version"] == 1
    assert appended_events[0]["payload"]["new_version"] == 2
    assert appended_events[0]["created_at"] == "2026-05-31T12:00:00+00:00"
    assert appended_events[1]["payload"]["task_id"] == "task-1"
    assert appended_events[1]["created_at"] == "2026-05-31T12:10:00+00:00"
    assert appended_events[2]["payload"]["agent_name"] == "reviewer"
    assert appended_events[2]["created_at"] == "2026-05-31T12:10:00+00:00"


@pytest.mark.asyncio
async def test_agent_loop_treats_tool_completed_task_as_success_and_enters_idle(
    monkeypatch,
) -> None:
    from agentscope_integration.shadow_clone_v2.agent_loop import run_agent_turn
    import agentscope_integration.shadow_clone_v2.agent_loop as agent_loop_module

    calls: list[tuple[str, object]] = []
    appended_events: list[dict] = []
    now = datetime(2026, 6, 3, 12, 0, 0, tzinfo=timezone.utc)
    finished_at = datetime(2026, 6, 3, 12, 3, 0, tzinfo=timezone.utc)
    agent = AgentIdentity(
        agent_id="teammate-1@thread-1",
        agent_name="teammate-1",
        thread_id="thread-1",
        project_id="project-1",
        role="worker",
        status=AgentLifecycleStatus.STARTING,
    )
    task = Task(
        id="live-click-subtask",
        run_id="run-1",
        thread_id="thread-1",
        subject="Live click",
        description="Return LIVE_CLICK_SUBAGENT_OK.",
        created_at=now.isoformat(),
        updated_at=now.isoformat(),
    )
    claimed_task = task.model_copy(
        update={
            "status": TaskStatus.IN_PROGRESS,
            "owner_agent": "teammate-1",
            "attempt": 1,
            "plan_revision": 2,
        }
    )
    tool_completed_task = claimed_task.model_copy(
        update={
            "status": TaskStatus.COMPLETED,
            "result_ref": "LIVE_CLICK_SUBAGENT_OK",
            "plan_revision": 3,
        }
    )
    read_count = 0

    async def fake_claim_task(**kwargs):
        calls.append(("claim_task", kwargs))
        return True

    async def fake_read_task(**kwargs):
        nonlocal read_count
        read_count += 1
        calls.append(("read_task", kwargs))
        return claimed_task if read_count == 1 else tool_completed_task

    async def fake_complete_task(**kwargs):
        calls.append(("complete_task", kwargs))
        return False

    async def fake_append_event(**kwargs):
        appended_events.append(kwargs)
        return SimpleNamespace(id=f"event-{len(appended_events)}", **kwargs)

    def fake_mark_agent_idle(next_agent, *, now):
        calls.append(("mark_agent_idle", {"agent": next_agent, "now": now}))
        return next_agent.model_copy(
            update={
                "status": AgentLifecycleStatus.IDLE,
                "idle_since": now.isoformat(),
                "idle_expires_at": "2026-06-03T12:33:00+00:00",
            }
        )

    async def fake_executor(next_task, next_agent):
        calls.append(("executor", {"task": next_task, "agent": next_agent}))
        return ""

    monkeypatch.setattr(agent_loop_module.task_pool, "claim_task", fake_claim_task)
    monkeypatch.setattr(agent_loop_module.task_pool, "read_task", fake_read_task)
    monkeypatch.setattr(
        agent_loop_module.task_pool, "complete_task", fake_complete_task
    )
    monkeypatch.setattr(agent_loop_module.event_log, "append_event", fake_append_event)
    monkeypatch.setattr(
        agent_loop_module.agent_registry, "mark_agent_idle", fake_mark_agent_idle
    )

    result = await run_agent_turn(
        run_id="run-1",
        project_id="project-1",
        agent=agent,
        task=task,
        lease_token="lease-1",
        lease_expires_at="2026-06-03T12:20:00+00:00",
        now=now,
        executor=fake_executor,
        sequence_start=7,
        clock=lambda: finished_at,
    )

    assert result.agent.status == AgentLifecycleStatus.IDLE
    assert result.output == "LIVE_CLICK_SUBAGENT_OK"
    assert result.task.status == TaskStatus.COMPLETED
    assert result.next_sequence == 10
    assert [event["event_type"] for event in appended_events] == [
        EventType.TASK_CLAIMED,
        EventType.TASK_COMPLETED,
        EventType.AGENT_IDLE,
    ]
    assert appended_events[1]["payload"]["result_ref"] == "LIVE_CLICK_SUBAGENT_OK"
    assert appended_events[-1]["payload"]["agent_name"] == "teammate-1"
    assert [name for name, _payload in calls] == [
        "claim_task",
        "read_task",
        "executor",
        "complete_task",
        "read_task",
        "mark_agent_idle",
    ]


@pytest.mark.asyncio
async def test_agent_loop_preserves_executor_text_when_tool_completed_task_has_artifact_ref(
    monkeypatch,
) -> None:
    from agentscope_integration.shadow_clone_v2.agent_loop import run_agent_turn
    import agentscope_integration.shadow_clone_v2.agent_loop as agent_loop_module

    appended_events: list[dict] = []
    now = datetime(2026, 6, 4, 12, 0, 0, tzinfo=timezone.utc)
    finished_at = datetime(2026, 6, 4, 12, 3, 0, tzinfo=timezone.utc)
    agent = AgentIdentity(
        agent_id="teammate-1@thread-1",
        agent_name="teammate-1",
        thread_id="thread-1",
        project_id="project-1",
        role="worker",
        status=AgentLifecycleStatus.STARTING,
    )
    task = Task(
        id="idle-feedback-apply",
        run_id="run-1",
        thread_id="thread-1",
        subject="Apply feedback",
        description="Reuse prior draft and return visible markers.",
        created_at=now.isoformat(),
        updated_at=now.isoformat(),
    )
    claimed_task = task.model_copy(
        update={
            "status": TaskStatus.IN_PROGRESS,
            "owner_agent": "teammate-1",
            "attempt": 1,
            "plan_revision": 2,
        }
    )
    tool_completed_task = claimed_task.model_copy(
        update={
            "status": TaskStatus.COMPLETED,
            "result_ref": "/workspace/live-click-subtask-1780527898453.md",
            "plan_revision": 3,
        }
    )
    read_count = 0

    async def fake_claim_task(**_kwargs):
        return True

    async def fake_read_task(**_kwargs):
        nonlocal read_count
        read_count += 1
        return claimed_task if read_count == 1 else tool_completed_task

    async def fake_complete_task(**_kwargs):
        return False

    async def fake_append_event(**kwargs):
        appended_events.append(kwargs)
        return SimpleNamespace(id=f"event-{len(appended_events)}", **kwargs)

    def fake_mark_agent_idle(next_agent, *, now):
        return next_agent.model_copy(
            update={
                "status": AgentLifecycleStatus.IDLE,
                "idle_since": now.isoformat(),
                "idle_expires_at": "2026-06-04T12:33:00+00:00",
            }
        )

    async def fake_executor(next_task, next_agent):
        return (
            "WAKE_REUSED_TEAMMATE_OK\n"
            "IDLE_FEEDBACK_REQUEST_1780527898453\n"
            "IDLE_FEEDBACK_APPLIED_1780527898453"
        )

    monkeypatch.setattr(agent_loop_module.task_pool, "claim_task", fake_claim_task)
    monkeypatch.setattr(agent_loop_module.task_pool, "read_task", fake_read_task)
    monkeypatch.setattr(
        agent_loop_module.task_pool, "complete_task", fake_complete_task
    )
    monkeypatch.setattr(agent_loop_module.event_log, "append_event", fake_append_event)
    monkeypatch.setattr(
        agent_loop_module.agent_registry, "mark_agent_idle", fake_mark_agent_idle
    )

    result = await run_agent_turn(
        run_id="run-1",
        project_id="project-1",
        agent=agent,
        task=task,
        lease_token="lease-1",
        lease_expires_at="2026-06-04T12:20:00+00:00",
        now=now,
        executor=fake_executor,
        sequence_start=7,
        clock=lambda: finished_at,
    )

    assert result.task.result_ref == "/workspace/live-click-subtask-1780527898453.md"
    assert "WAKE_REUSED_TEAMMATE_OK" in result.output
    assert "IDLE_FEEDBACK_APPLIED_1780527898453" in result.output
    assert "/workspace/live-click-subtask-1780527898453.md" not in result.output
    assert [event["event_type"] for event in appended_events] == [
        EventType.TASK_CLAIMED,
        EventType.TASK_COMPLETED,
        EventType.AGENT_IDLE,
    ]
    assert appended_events[1]["payload"]["result_ref"] == (
        "/workspace/live-click-subtask-1780527898453.md"
    )
    assert "WAKE_REUSED_TEAMMATE_OK" in appended_events[1]["payload"]["result_summary"]


@pytest.mark.asyncio
async def test_agent_loop_does_not_execute_when_claim_fails(monkeypatch) -> None:
    from agentscope_integration.shadow_clone_v2.agent_loop import run_agent_turn
    import agentscope_integration.shadow_clone_v2.agent_loop as agent_loop_module

    calls: list[str] = []
    now = datetime(2026, 5, 31, 12, 0, 0, tzinfo=timezone.utc)
    agent = AgentIdentity(
        agent_id="reviewer@thread-1",
        agent_name="reviewer",
        thread_id="thread-1",
        project_id="project-1",
        account_id="account-1",
        role="reviewer",
    )
    task = Task(
        id="task-1",
        run_id="run-1",
        thread_id="thread-1",
        subject="Review",
        description="Review the result.",
        created_at=now.isoformat(),
        updated_at=now.isoformat(),
    )

    async def fake_claim_task(**_kwargs):
        calls.append("claim_task")
        return False

    async def fake_executor(_task, _agent):
        calls.append("executor")
        return "should not run"

    monkeypatch.setattr(agent_loop_module.task_pool, "claim_task", fake_claim_task)

    with pytest.raises(RuntimeError, match="could not claim task"):
        await run_agent_turn(
            run_id="run-1",
            project_id="project-1",
            agent=agent,
            task=task,
            lease_token="lease-1",
            lease_expires_at="2026-05-31T12:05:00+00:00",
            now=now,
            executor=fake_executor,
            sequence_start=7,
        )

    assert calls == ["claim_task"]


@pytest.mark.asyncio
async def test_agent_loop_appends_task_failed_when_executor_raises(monkeypatch) -> None:
    from agentscope_integration.shadow_clone_v2.agent_loop import run_agent_turn
    import agentscope_integration.shadow_clone_v2.agent_loop as agent_loop_module

    appended_events: list[dict] = []
    calls: list[str] = []
    now = datetime(2026, 5, 31, 12, 0, 0, tzinfo=timezone.utc)
    agent = AgentIdentity(
        agent_id="reviewer@thread-1",
        agent_name="reviewer",
        thread_id="thread-1",
        project_id="project-1",
        role="reviewer",
    )
    task = Task(
        id="task-1",
        run_id="run-1",
        thread_id="thread-1",
        subject="Review",
        description="Review the result.",
        created_at=now.isoformat(),
        updated_at=now.isoformat(),
    )

    async def fake_claim_task(**_kwargs):
        return True

    async def fake_executor(_task, _agent):
        raise ValueError("executor exploded")

    async def fake_fail_task(**_kwargs):
        calls.append("fail_task")
        return True

    def fake_mark_agent_idle(next_agent, *, now):
        calls.append("mark_agent_idle")
        return next_agent.model_copy(
            update={
                "status": AgentLifecycleStatus.IDLE,
                "idle_since": now.isoformat(),
                "idle_expires_at": "2026-05-31T12:20:00+00:00",
            }
        )

    async def fake_append_event(**kwargs):
        appended_events.append(kwargs)
        return SimpleNamespace(id=f"event-{len(appended_events)}", **kwargs)

    monkeypatch.setattr(agent_loop_module.task_pool, "claim_task", fake_claim_task)
    monkeypatch.setattr(
        agent_loop_module.task_pool, "fail_task", fake_fail_task, raising=False
    )
    monkeypatch.setattr(
        agent_loop_module.agent_registry, "mark_agent_idle", fake_mark_agent_idle
    )
    monkeypatch.setattr(agent_loop_module.event_log, "append_event", fake_append_event)

    with pytest.raises(ValueError, match="executor exploded"):
        await run_agent_turn(
            run_id="run-1",
            project_id="project-1",
            agent=agent,
            task=task,
            lease_token="lease-1",
            lease_expires_at="2026-05-31T12:05:00+00:00",
            now=now,
            executor=fake_executor,
            sequence_start=7,
        )

    assert calls == ["fail_task", "mark_agent_idle"]
    assert [event["event_type"] for event in appended_events] == [
        EventType.TASK_CLAIMED,
        EventType.TASK_FAILED,
        EventType.AGENT_IDLE,
    ]
    assert appended_events[0]["payload"]["task_id"] == "task-1"
    assert appended_events[1]["payload"]["task_id"] == "task-1"
    assert appended_events[1]["payload"]["error_type"] == "ValueError"
    assert appended_events[1]["payload"]["task_marked_failed"] is True
    assert appended_events[2]["payload"]["agent_name"] == "reviewer"


@pytest.mark.asyncio
async def test_agent_loop_advances_sequence_after_executor_tool_events(
    monkeypatch,
) -> None:
    from agentscope_integration.shadow_clone_v2.agent_loop import run_agent_turn
    import agentscope_integration.shadow_clone_v2.agent_loop as agent_loop_module

    appended_events: list[dict] = []
    now = datetime(2026, 6, 2, 0, 0, 0, tzinfo=timezone.utc)
    finished_at = datetime(2026, 6, 2, 0, 2, 0, tzinfo=timezone.utc)
    agent = AgentIdentity(
        agent_id="reviewer@thread-1",
        agent_name="reviewer",
        thread_id="thread-1",
        project_id="project-1",
        role="reviewer",
    )
    task = Task(
        id="task-1",
        run_id="run-1",
        thread_id="thread-1",
        subject="Review",
        description="Review the result.",
        created_at=now.isoformat(),
        updated_at=now.isoformat(),
    )

    async def fake_claim_task(**_kwargs):
        return True

    async def fake_complete_task(**_kwargs):
        return True

    async def fake_append_event(**kwargs):
        appended_events.append(kwargs)
        return SimpleNamespace(id=f"event-{len(appended_events)}", **kwargs)

    async def fake_executor(next_task, next_agent):
        await agent_loop_module.event_log.append_event(
            run_id="run-1",
            thread_id=next_task.thread_id,
            project_id="project-1",
            sequence=8,
            event_type=EventType.MAILBOX_SENT,
            activity_owner=f"agent:{next_agent.agent_name}",
            payload={"message_id": "1-0"},
            created_at=finished_at.isoformat(),
        )
        return SimpleNamespace(output="review complete", next_sequence=9)

    def fake_mark_agent_idle(next_agent, *, now):
        return next_agent.model_copy(
            update={
                "status": AgentLifecycleStatus.IDLE,
                "idle_since": now.isoformat(),
                "idle_expires_at": "2026-06-02T00:22:00+00:00",
            }
        )

    monkeypatch.setattr(agent_loop_module.task_pool, "claim_task", fake_claim_task)
    monkeypatch.setattr(
        agent_loop_module.task_pool, "complete_task", fake_complete_task
    )
    monkeypatch.setattr(agent_loop_module.event_log, "append_event", fake_append_event)
    monkeypatch.setattr(
        agent_loop_module.agent_registry, "mark_agent_idle", fake_mark_agent_idle
    )

    result = await run_agent_turn(
        run_id="run-1",
        project_id="project-1",
        agent=agent,
        task=task,
        lease_token="lease-1",
        lease_expires_at="2026-06-02T00:05:00+00:00",
        now=now,
        executor=fake_executor,
        sequence_start=7,
        clock=lambda: finished_at,
    )

    assert result.output == "review complete"
    assert [event["event_type"] for event in appended_events] == [
        EventType.TASK_CLAIMED,
        EventType.MAILBOX_SENT,
        EventType.TASK_COMPLETED,
        EventType.AGENT_IDLE,
    ]
    assert [event["sequence"] for event in appended_events] == [7, 8, 9, 10]
    assert result.next_sequence == 11


@pytest.mark.asyncio
async def test_agent_loop_injects_lease_renewal_callback_and_projects_update(
    monkeypatch,
) -> None:
    from agentscope_integration.shadow_clone_v2.agent_loop import run_agent_turn
    import agentscope_integration.shadow_clone_v2.agent_loop as agent_loop_module

    appended_events: list[dict] = []
    renew_calls: list[dict] = []
    now = datetime(2026, 6, 2, 0, 0, 0, tzinfo=timezone.utc)
    renewed_at = datetime(2026, 6, 2, 0, 3, 0, tzinfo=timezone.utc)
    finished_at = datetime(2026, 6, 2, 0, 4, 0, tzinfo=timezone.utc)
    clock_values = iter([renewed_at, finished_at])
    agent = AgentIdentity(
        agent_id="reviewer@thread-1",
        agent_name="reviewer",
        thread_id="thread-1",
        project_id="project-1",
        role="reviewer",
    )
    task = Task(
        id="task-1",
        run_id="run-1",
        thread_id="thread-1",
        subject="Review",
        description="Review the result.",
        plan_revision=1,
        created_at=now.isoformat(),
        updated_at=now.isoformat(),
    )
    claimed_task = task.model_copy(
        update={
            "plan_revision": 2,
            "attempt": 1,
            "lease_expires_at": "2026-06-02T00:05:00+00:00",
            "updated_at": now.isoformat(),
        }
    )
    renewed_task = task.model_copy(
        update={
            "plan_revision": 3,
            "attempt": 1,
            "lease_expires_at": "2026-06-02T00:23:00+00:00",
            "updated_at": renewed_at.isoformat(),
        }
    )

    async def fake_claim_task(**_kwargs):
        return True

    async def fake_complete_task(**_kwargs):
        return True

    async def fake_renew_task_lease(**kwargs):
        renew_calls.append(kwargs)
        return True

    read_results = iter([claimed_task, renewed_task])

    async def fake_read_task(**_kwargs):
        return next(read_results)

    async def fake_append_event(**kwargs):
        appended_events.append(kwargs)
        return SimpleNamespace(id=f"event-{len(appended_events)}", **kwargs)

    def fake_mark_agent_idle(next_agent, *, now):
        return next_agent.model_copy(
            update={
                "status": AgentLifecycleStatus.IDLE,
                "idle_since": now.isoformat(),
                "idle_expires_at": "2026-06-02T00:24:00+00:00",
            }
        )

    async def fake_executor(next_task, next_agent, *, renew_task_lease):
        renewed = await renew_task_lease(lease_expires_at="2026-06-02T00:23:00+00:00")
        assert renewed is True
        return "review complete"

    monkeypatch.setattr(agent_loop_module.task_pool, "claim_task", fake_claim_task)
    monkeypatch.setattr(
        agent_loop_module.task_pool, "complete_task", fake_complete_task
    )
    monkeypatch.setattr(
        agent_loop_module.task_pool, "renew_task_lease", fake_renew_task_lease
    )
    monkeypatch.setattr(agent_loop_module.task_pool, "read_task", fake_read_task)
    monkeypatch.setattr(agent_loop_module.event_log, "append_event", fake_append_event)
    monkeypatch.setattr(
        agent_loop_module.agent_registry, "mark_agent_idle", fake_mark_agent_idle
    )

    result = await run_agent_turn(
        run_id="run-1",
        project_id="project-1",
        agent=agent,
        task=task,
        lease_token="lease-1",
        lease_expires_at="2026-06-02T00:05:00+00:00",
        now=now,
        executor=fake_executor,
        sequence_start=7,
        clock=lambda: next(clock_values),
    )

    assert renew_calls == [
        {
            "run_id": "run-1",
            "task_id": "task-1",
            "owner_agent": "reviewer",
            "lease_token": "lease-1",
            "lease_expires_at": "2026-06-02T00:23:00+00:00",
            "updated_at": "2026-06-02T00:03:00+00:00",
            "now": "2026-06-02T00:03:00+00:00",
        }
    ]
    assert [event["event_type"] for event in appended_events] == [
        EventType.TASK_CLAIMED,
        EventType.TASK_UPDATED,
        EventType.TASK_COMPLETED,
        EventType.AGENT_IDLE,
    ]
    assert appended_events[1]["payload"] == {
        "task_id": "task-1",
        "agent_name": "reviewer",
        "change_reason": "lease_renewed",
        "previous_lease_expires_at": "2026-06-02T00:05:00+00:00",
        "lease_expires_at": "2026-06-02T00:23:00+00:00",
        "previous_version": 2,
        "new_version": 3,
    }
    assert [event["sequence"] for event in appended_events] == [7, 8, 9, 10]
    assert result.next_sequence == 11


@pytest.mark.asyncio
async def test_agent_loop_late_completion_after_expired_lease_does_not_emit_completed(
    monkeypatch,
) -> None:
    from agentscope_integration.shadow_clone_v2.agent_loop import run_agent_turn
    import agentscope_integration.shadow_clone_v2.agent_loop as agent_loop_module

    appended_events: list[dict] = []
    now = datetime(2026, 6, 2, 0, 0, 0, tzinfo=timezone.utc)
    finished_at = datetime(2026, 6, 2, 0, 10, 0, tzinfo=timezone.utc)
    agent = AgentIdentity(
        agent_id="reviewer@thread-1",
        agent_name="reviewer",
        thread_id="thread-1",
        project_id="project-1",
        role="reviewer",
    )
    task = Task(
        id="task-1",
        run_id="run-1",
        thread_id="thread-1",
        subject="Review",
        description="Review the result.",
        created_at=now.isoformat(),
        updated_at=now.isoformat(),
    )

    async def fake_claim_task(**_kwargs):
        return True

    async def fake_complete_task(**kwargs):
        assert kwargs["now"] == "2026-06-02T00:10:00+00:00"
        return False

    async def fake_append_event(**kwargs):
        appended_events.append(kwargs)
        return SimpleNamespace(id=f"event-{len(appended_events)}", **kwargs)

    async def fake_executor(_task, _agent):
        return "late output"

    monkeypatch.setattr(agent_loop_module.task_pool, "claim_task", fake_claim_task)
    monkeypatch.setattr(
        agent_loop_module.task_pool, "complete_task", fake_complete_task
    )
    monkeypatch.setattr(agent_loop_module.event_log, "append_event", fake_append_event)

    with pytest.raises(RuntimeError, match="lost lease"):
        await run_agent_turn(
            run_id="run-1",
            project_id="project-1",
            agent=agent,
            task=task,
            lease_token="lease-1",
            lease_expires_at="2026-06-02T00:05:00+00:00",
            now=now,
            executor=fake_executor,
            sequence_start=7,
            clock=lambda: finished_at,
        )

    assert [event["event_type"] for event in appended_events] == [
        EventType.TASK_CLAIMED
    ]


@pytest.mark.asyncio
async def test_agent_loop_executor_failure_after_lost_lease_does_not_emit_task_failed(
    monkeypatch,
) -> None:
    from agentscope_integration.shadow_clone_v2.agent_loop import run_agent_turn
    import agentscope_integration.shadow_clone_v2.agent_loop as agent_loop_module

    appended_events: list[dict] = []
    now = datetime(2026, 6, 2, 0, 0, 0, tzinfo=timezone.utc)
    failed_at = datetime(2026, 6, 2, 0, 10, 0, tzinfo=timezone.utc)
    agent = AgentIdentity(
        agent_id="reviewer@thread-1",
        agent_name="reviewer",
        thread_id="thread-1",
        project_id="project-1",
        role="reviewer",
    )
    task = Task(
        id="task-1",
        run_id="run-1",
        thread_id="thread-1",
        subject="Review",
        description="Review the result.",
        created_at=now.isoformat(),
        updated_at=now.isoformat(),
    )

    async def fake_claim_task(**_kwargs):
        return True

    async def fake_fail_task(**kwargs):
        assert kwargs["now"] == "2026-06-02T00:10:00+00:00"
        return False

    async def fake_append_event(**kwargs):
        appended_events.append(kwargs)
        return SimpleNamespace(id=f"event-{len(appended_events)}", **kwargs)

    def fake_mark_agent_idle(next_agent, *, now):
        return next_agent.model_copy(
            update={
                "status": AgentLifecycleStatus.IDLE,
                "idle_since": now.isoformat(),
                "idle_expires_at": "2026-06-02T00:30:00+00:00",
            }
        )

    async def fake_executor(_task, _agent):
        raise ValueError("late failure")

    monkeypatch.setattr(agent_loop_module.task_pool, "claim_task", fake_claim_task)
    monkeypatch.setattr(agent_loop_module.task_pool, "fail_task", fake_fail_task)
    monkeypatch.setattr(agent_loop_module.event_log, "append_event", fake_append_event)
    monkeypatch.setattr(
        agent_loop_module.agent_registry, "mark_agent_idle", fake_mark_agent_idle
    )

    with pytest.raises(ValueError, match="late failure"):
        await run_agent_turn(
            run_id="run-1",
            project_id="project-1",
            agent=agent,
            task=task,
            lease_token="lease-1",
            lease_expires_at="2026-06-02T00:05:00+00:00",
            now=now,
            executor=fake_executor,
            sequence_start=7,
            clock=lambda: failed_at,
        )

    assert EventType.TASK_FAILED not in [
        event["event_type"] for event in appended_events
    ]
    assert [event["event_type"] for event in appended_events] == [
        EventType.TASK_CLAIMED,
        EventType.TASK_UPDATED,
        EventType.AGENT_IDLE,
    ]
    assert appended_events[1]["payload"]["change_reason"] == "lease_lost_failure"


@pytest.mark.asyncio
async def test_project_expired_task_lease_recovery_events_maps_retry_and_failure(
    monkeypatch,
) -> None:
    import agentscope_integration.shadow_clone_v2.agent_loop as agent_loop_module

    appended_events: list[dict] = []
    retry_task = Task(
        id="task-retry",
        run_id="run-1",
        thread_id="thread-1",
        subject="Retry",
        description="Retry me.",
        status=TaskStatus.PENDING,
        attempt=1,
        max_attempts=2,
        plan_revision=3,
        error={"error_type": "LeaseExpired"},
        created_at="2026-06-02T00:00:00+00:00",
        updated_at="2026-06-02T00:10:00+00:00",
    )
    failed_task = Task(
        id="task-fail",
        run_id="run-1",
        thread_id="thread-1",
        subject="Fail",
        description="Fail me.",
        status=TaskStatus.FAILED,
        attempt=2,
        max_attempts=2,
        plan_revision=4,
        error={"error_type": "LeaseExpired"},
        created_at="2026-06-02T00:00:00+00:00",
        updated_at="2026-06-02T00:10:00+00:00",
    )
    sweep_results = [
        task_pool.ExpiredTaskLeaseSweepResult(
            action="retried",
            task=retry_task,
            previous_status="in_progress",
            previous_owner_agent="reviewer",
            previous_lease_expires_at="2026-06-02T00:05:00+00:00",
        ),
        task_pool.ExpiredTaskLeaseSweepResult(
            action="failed",
            task=failed_task,
            previous_status="in_progress",
            previous_owner_agent="writer",
            previous_lease_expires_at="2026-06-02T00:05:00+00:00",
        ),
    ]

    async def fake_sweep_expired_task_leases(**kwargs):
        assert kwargs == {
            "run_id": "run-1",
            "now": "2026-06-02T00:10:00+00:00",
            "ttl_seconds": 3600,
        }
        return sweep_results

    async def fake_append_event(**kwargs):
        appended_events.append(kwargs)
        return SimpleNamespace(id=f"event-{len(appended_events)}", **kwargs)

    monkeypatch.setattr(
        agent_loop_module.task_pool,
        "sweep_expired_task_leases",
        fake_sweep_expired_task_leases,
    )
    monkeypatch.setattr(agent_loop_module.event_log, "append_event", fake_append_event)

    next_sequence = await agent_loop_module.project_expired_task_lease_recovery_events(
        run_id="run-1",
        thread_id="thread-1",
        project_id="project-1",
        now="2026-06-02T00:10:00+00:00",
        ttl_seconds=3600,
        sequence_start=17,
    )

    assert next_sequence == 19
    assert [event["event_type"] for event in appended_events] == [
        EventType.TASK_UPDATED,
        EventType.TASK_FAILED,
    ]
    assert appended_events[0]["payload"] == {
        "task_id": "task-retry",
        "change_reason": "lease_expired_recovered",
        "previous_status": "in_progress",
        "status": "pending",
        "expired_owner_agent": "reviewer",
        "expired_lease_expires_at": "2026-06-02T00:05:00+00:00",
        "attempt": 1,
        "max_attempts": 2,
        "previous_version": 2,
        "new_version": 3,
        "error_type": "LeaseExpired",
    }
    assert appended_events[1]["payload"]["task_id"] == "task-fail"
    assert appended_events[1]["payload"]["error_type"] == "LeaseExpired"
    assert [event["sequence"] for event in appended_events] == [17, 18]


@pytest.mark.asyncio
async def test_agent_loop_stages_inbox_messages_and_acks_before_executor(
    monkeypatch,
) -> None:
    from agentscope_integration.shadow_clone_v2.agent_loop import run_agent_turn
    import agentscope_integration.shadow_clone_v2.agent_loop as agent_loop_module

    appended_events: list[dict] = []
    acked_messages: list[dict] = []
    now = datetime(2026, 6, 3, 0, 0, 0, tzinfo=timezone.utc)
    finished_at = datetime(2026, 6, 3, 0, 2, 0, tzinfo=timezone.utc)
    agent = AgentIdentity(
        agent_id="reviewer@thread-1",
        agent_name="reviewer",
        thread_id="thread-1",
        project_id="project-1",
        account_id="account-1",
        role="reviewer",
    )
    task = Task(
        id="task-1",
        run_id="run-1",
        thread_id="thread-1",
        subject="Review",
        description="Review the result.",
        created_at=now.isoformat(),
        updated_at=now.isoformat(),
    )
    staged_message = MailboxMessage(
        id="1-0",
        run_id="run-1",
        thread_id="thread-1",
        project_id="project-1",
        sender="researcher",
        recipient="reviewer",
        kind=MessageKind.TEXT,
        type=MessageType.TEXT,
        idempotency_key="researcher-reviewer-1",
        text="Please review the draft.",
        created_at=now.isoformat(),
    )

    async def fake_claim_task(**_kwargs):
        return True

    async def fake_complete_task(**_kwargs):
        return True

    async def fake_read_next_messages_with_dead_letters(**kwargs):
        assert kwargs["agent_name"] == "reviewer"
        assert kwargs["account_id"] == "account-1"
        return agent_loop_module.mailbox.MailboxReadResult(
            messages=[staged_message],
            dead_lettered=[],
        )

    async def fake_ack_message(**kwargs):
        assert kwargs["account_id"] == "account-1"
        acked_messages.append(kwargs)
        return agent_loop_module.mailbox.MailboxAckResult(
            project_id=kwargs["project_id"],
            thread_id=kwargs["thread_id"],
            agent_name=kwargs["agent_name"],
            message_id=kwargs["message_id"],
            acked_by=kwargs["acked_by"],
            acked_at=kwargs["acked_at"],
            read_at=kwargs["acked_at"],
            already_acked=False,
        )

    async def fake_append_event(**kwargs):
        appended_events.append(kwargs)
        return SimpleNamespace(id=f"event-{len(appended_events)}", **kwargs)

    async def fake_executor(next_task, next_agent, *, inbox_messages, sequence_start):
        assert next_task == task
        assert next_agent == agent
        assert inbox_messages == [staged_message]
        assert sequence_start == 8
        return "review complete"

    def fake_mark_agent_idle(next_agent, *, now):
        return next_agent.model_copy(
            update={
                "status": AgentLifecycleStatus.IDLE,
                "idle_since": now.isoformat(),
                "idle_expires_at": "2026-06-03T00:22:00+00:00",
            }
        )

    monkeypatch.setattr(agent_loop_module.task_pool, "claim_task", fake_claim_task)
    monkeypatch.setattr(
        agent_loop_module.task_pool, "complete_task", fake_complete_task
    )
    monkeypatch.setattr(
        agent_loop_module.mailbox,
        "read_next_messages_with_dead_letters",
        fake_read_next_messages_with_dead_letters,
    )
    monkeypatch.setattr(agent_loop_module.mailbox, "ack_message", fake_ack_message)
    monkeypatch.setattr(agent_loop_module.event_log, "append_event", fake_append_event)
    monkeypatch.setattr(
        agent_loop_module.agent_registry, "mark_agent_idle", fake_mark_agent_idle
    )

    result = await run_agent_turn(
        run_id="run-1",
        project_id="project-1",
        agent=agent,
        task=task,
        lease_token="lease-1",
        lease_expires_at="2026-06-03T00:05:00+00:00",
        now=now,
        executor=fake_executor,
        sequence_start=7,
        clock=lambda: finished_at,
    )

    assert result.output == "review complete"
    assert acked_messages[0]["message_id"] == "1-0"
    assert [event["event_type"] for event in appended_events] == [
        EventType.TASK_CLAIMED,
        EventType.MAILBOX_ACKED,
        EventType.TASK_COMPLETED,
        EventType.AGENT_IDLE,
    ]
    assert [event["sequence"] for event in appended_events] == [7, 8, 9, 10]
    assert appended_events[1]["payload"] == {
        "message_id": "1-0",
        "agent_name": "reviewer",
        "acked_by": "reviewer",
        "already_acked": False,
    }
    assert result.next_sequence == 11


@pytest.mark.asyncio
async def test_agent_loop_does_not_ack_inbox_for_old_executor(monkeypatch) -> None:
    from agentscope_integration.shadow_clone_v2.agent_loop import run_agent_turn
    import agentscope_integration.shadow_clone_v2.agent_loop as agent_loop_module

    appended_events: list[dict] = []
    acked_messages: list[dict] = []
    now = datetime(2026, 6, 3, 0, 0, 0, tzinfo=timezone.utc)
    finished_at = datetime(2026, 6, 3, 0, 2, 0, tzinfo=timezone.utc)
    agent = AgentIdentity(
        agent_id="reviewer@thread-1",
        agent_name="reviewer",
        thread_id="thread-1",
        project_id="project-1",
        role="reviewer",
    )
    task = Task(
        id="task-1",
        run_id="run-1",
        thread_id="thread-1",
        subject="Review",
        description="Review the result.",
        created_at=now.isoformat(),
        updated_at=now.isoformat(),
    )

    async def fake_claim_task(**_kwargs):
        return True

    async def fake_complete_task(**_kwargs):
        return True

    async def fake_read_next_messages_with_dead_letters(**_kwargs):
        raise AssertionError("old executors cannot consume inbox; do not read/ack")

    async def fake_ack_message(**kwargs):
        acked_messages.append(kwargs)
        raise AssertionError("old executors must not ack unread inbox")

    async def fake_append_event(**kwargs):
        appended_events.append(kwargs)
        return SimpleNamespace(id=f"event-{len(appended_events)}", **kwargs)

    async def fake_executor(next_task, next_agent):
        assert next_task == task
        assert next_agent == agent
        return "review complete"

    def fake_mark_agent_idle(next_agent, *, now):
        return next_agent.model_copy(
            update={
                "status": AgentLifecycleStatus.IDLE,
                "idle_since": now.isoformat(),
                "idle_expires_at": "2026-06-03T00:22:00+00:00",
            }
        )

    monkeypatch.setattr(agent_loop_module.task_pool, "claim_task", fake_claim_task)
    monkeypatch.setattr(
        agent_loop_module.task_pool, "complete_task", fake_complete_task
    )
    monkeypatch.setattr(
        agent_loop_module.mailbox,
        "read_next_messages_with_dead_letters",
        fake_read_next_messages_with_dead_letters,
    )
    monkeypatch.setattr(agent_loop_module.mailbox, "ack_message", fake_ack_message)
    monkeypatch.setattr(agent_loop_module.event_log, "append_event", fake_append_event)
    monkeypatch.setattr(
        agent_loop_module.agent_registry, "mark_agent_idle", fake_mark_agent_idle
    )

    result = await run_agent_turn(
        run_id="run-1",
        project_id="project-1",
        agent=agent,
        task=task,
        lease_token="lease-1",
        lease_expires_at="2026-06-03T00:05:00+00:00",
        now=now,
        executor=fake_executor,
        sequence_start=7,
        clock=lambda: finished_at,
    )

    assert result.output == "review complete"
    assert acked_messages == []
    assert [event["event_type"] for event in appended_events] == [
        EventType.TASK_CLAIMED,
        EventType.TASK_COMPLETED,
        EventType.AGENT_IDLE,
    ]


@pytest.mark.asyncio
async def test_agent_loop_projects_auto_dead_lettered_messages(monkeypatch) -> None:
    from agentscope_integration.shadow_clone_v2.agent_loop import run_agent_turn
    import agentscope_integration.shadow_clone_v2.agent_loop as agent_loop_module

    appended_events: list[dict] = []
    now = datetime(2026, 6, 3, 0, 0, 0, tzinfo=timezone.utc)
    finished_at = datetime(2026, 6, 3, 0, 2, 0, tzinfo=timezone.utc)
    agent = AgentIdentity(
        agent_id="reviewer@thread-1",
        agent_name="reviewer",
        thread_id="thread-1",
        project_id="project-1",
        role="reviewer",
    )
    task = Task(
        id="task-1",
        run_id="run-1",
        thread_id="thread-1",
        subject="Review",
        description="Review the result.",
        created_at=now.isoformat(),
        updated_at=now.isoformat(),
    )
    dead_lettered = MailboxMessage(
        id="1-0",
        run_id="run-1",
        thread_id="thread-1",
        project_id="project-1",
        sender="researcher",
        recipient="reviewer",
        kind=MessageKind.TEXT,
        type=MessageType.TEXT,
        idempotency_key="poison",
        text="Poison.",
        created_at=now.isoformat(),
        dead_letter_reason="max_delivery_attempts_exceeded:1",
        dead_lettered_at=now.isoformat(),
    )

    async def fake_claim_task(**_kwargs):
        return True

    async def fake_complete_task(**_kwargs):
        return True

    async def fake_read_next_messages_with_dead_letters(**_kwargs):
        return agent_loop_module.mailbox.MailboxReadResult(
            messages=[],
            dead_lettered=[dead_lettered],
        )

    async def fake_append_event(**kwargs):
        appended_events.append(kwargs)
        return SimpleNamespace(id=f"event-{len(appended_events)}", **kwargs)

    async def fake_executor(_task, _agent, *, inbox_messages, sequence_start):
        assert inbox_messages == []
        assert sequence_start == 9
        return "review complete"

    def fake_mark_agent_idle(next_agent, *, now):
        return next_agent.model_copy(
            update={
                "status": AgentLifecycleStatus.IDLE,
                "idle_since": now.isoformat(),
                "idle_expires_at": "2026-06-03T00:22:00+00:00",
            }
        )

    monkeypatch.setattr(agent_loop_module.task_pool, "claim_task", fake_claim_task)
    monkeypatch.setattr(
        agent_loop_module.task_pool, "complete_task", fake_complete_task
    )
    monkeypatch.setattr(
        agent_loop_module.mailbox,
        "read_next_messages_with_dead_letters",
        fake_read_next_messages_with_dead_letters,
    )
    monkeypatch.setattr(agent_loop_module.event_log, "append_event", fake_append_event)
    monkeypatch.setattr(
        agent_loop_module.agent_registry, "mark_agent_idle", fake_mark_agent_idle
    )

    result = await run_agent_turn(
        run_id="run-1",
        project_id="project-1",
        agent=agent,
        task=task,
        lease_token="lease-1",
        lease_expires_at="2026-06-03T00:05:00+00:00",
        now=now,
        executor=fake_executor,
        sequence_start=7,
        clock=lambda: finished_at,
    )

    assert result.next_sequence == 11
    assert [event["event_type"] for event in appended_events] == [
        EventType.TASK_CLAIMED,
        EventType.MAILBOX_DEAD_LETTERED,
        EventType.TASK_COMPLETED,
        EventType.AGENT_IDLE,
    ]
    assert appended_events[1]["payload"] == {
        "message_id": "1-0",
        "agent_name": "reviewer",
        "reason": "max_delivery_attempts_exceeded:1",
    }


@pytest.mark.asyncio
async def test_agent_loop_failure_uses_executor_error_sequence_cursor(
    monkeypatch,
) -> None:
    from agentscope_integration.shadow_clone_v2.agent_loop import run_agent_turn
    import agentscope_integration.shadow_clone_v2.agent_loop as agent_loop_module

    class SequencedExecutorError(RuntimeError):
        def __init__(self) -> None:
            super().__init__("worker failed after tool use")
            self.next_sequence = 9

    appended_events: list[dict] = []
    now = datetime(2026, 6, 2, 0, 0, 0, tzinfo=timezone.utc)
    failed_at = datetime(2026, 6, 2, 0, 2, 0, tzinfo=timezone.utc)
    agent = AgentIdentity(
        agent_id="reviewer@thread-1",
        agent_name="reviewer",
        thread_id="thread-1",
        project_id="project-1",
        role="reviewer",
    )
    task = Task(
        id="task-1",
        run_id="run-1",
        thread_id="thread-1",
        subject="Review",
        description="Review the result.",
        created_at=now.isoformat(),
        updated_at=now.isoformat(),
    )

    async def fake_claim_task(**_kwargs):
        return True

    async def fake_fail_task(**_kwargs):
        return True

    async def fake_append_event(**kwargs):
        appended_events.append(kwargs)
        return SimpleNamespace(id=f"event-{len(appended_events)}", **kwargs)

    async def fake_executor(next_task, next_agent):
        await agent_loop_module.event_log.append_event(
            run_id="run-1",
            thread_id=next_task.thread_id,
            project_id="project-1",
            sequence=8,
            event_type=EventType.MAILBOX_SENT,
            activity_owner=f"agent:{next_agent.agent_name}",
            payload={"message_id": "1-0"},
            created_at=failed_at.isoformat(),
        )
        raise SequencedExecutorError()

    def fake_mark_agent_idle(next_agent, *, now):
        return next_agent.model_copy(
            update={
                "status": AgentLifecycleStatus.IDLE,
                "idle_since": now.isoformat(),
                "idle_expires_at": "2026-06-02T00:22:00+00:00",
            }
        )

    monkeypatch.setattr(agent_loop_module.task_pool, "claim_task", fake_claim_task)
    monkeypatch.setattr(agent_loop_module.task_pool, "fail_task", fake_fail_task)
    monkeypatch.setattr(agent_loop_module.event_log, "append_event", fake_append_event)
    monkeypatch.setattr(
        agent_loop_module.agent_registry, "mark_agent_idle", fake_mark_agent_idle
    )

    with pytest.raises(SequencedExecutorError) as exc_info:
        await run_agent_turn(
            run_id="run-1",
            project_id="project-1",
            agent=agent,
            task=task,
            lease_token="lease-1",
            lease_expires_at="2026-06-02T00:05:00+00:00",
            now=now,
            executor=fake_executor,
            sequence_start=7,
            clock=lambda: failed_at,
        )

    assert getattr(exc_info.value, "next_sequence") == 11
    assert [event["event_type"] for event in appended_events] == [
        EventType.TASK_CLAIMED,
        EventType.MAILBOX_SENT,
        EventType.TASK_FAILED,
        EventType.AGENT_IDLE,
    ]
    assert [event["sequence"] for event in appended_events] == [7, 8, 9, 10]


@pytest.mark.asyncio
async def test_agent_loop_lost_lease_error_carries_executor_sequence_cursor(
    monkeypatch,
) -> None:
    from agentscope_integration.shadow_clone_v2.agent_loop import run_agent_turn
    import agentscope_integration.shadow_clone_v2.agent_loop as agent_loop_module

    now = datetime(2026, 6, 2, 0, 0, 0, tzinfo=timezone.utc)
    finished_at = datetime(2026, 6, 2, 0, 2, 0, tzinfo=timezone.utc)
    agent = AgentIdentity(
        agent_id="reviewer@thread-1",
        agent_name="reviewer",
        thread_id="thread-1",
        project_id="project-1",
        role="reviewer",
    )
    task = Task(
        id="task-1",
        run_id="run-1",
        thread_id="thread-1",
        subject="Review",
        description="Review the result.",
        created_at=now.isoformat(),
        updated_at=now.isoformat(),
    )

    async def fake_claim_task(**_kwargs):
        return True

    async def fake_complete_task(**_kwargs):
        return False

    async def fake_append_event(**_kwargs):
        return SimpleNamespace(id="event")

    async def fake_executor(_task, _agent):
        return SimpleNamespace(output="review complete", next_sequence=12)

    monkeypatch.setattr(agent_loop_module.task_pool, "claim_task", fake_claim_task)
    monkeypatch.setattr(
        agent_loop_module.task_pool, "complete_task", fake_complete_task
    )
    monkeypatch.setattr(agent_loop_module.event_log, "append_event", fake_append_event)

    with pytest.raises(RuntimeError, match="lost lease") as exc_info:
        await run_agent_turn(
            run_id="run-1",
            project_id="project-1",
            agent=agent,
            task=task,
            lease_token="lease-1",
            lease_expires_at="2026-06-02T00:05:00+00:00",
            now=now,
            executor=fake_executor,
            sequence_start=7,
            clock=lambda: finished_at,
        )

    assert getattr(exc_info.value, "next_sequence") == 12
