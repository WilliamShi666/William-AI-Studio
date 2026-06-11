"""Minimal Shadow Clone V2 agent execution loop primitives."""

from __future__ import annotations

from dataclasses import dataclass
import inspect
from datetime import datetime
from typing import Any, Awaitable, Callable

from . import agent_registry, event_log, mailbox, task_pool
from .models import AgentIdentity, EventType, Task, TaskStatus

AgentTurnExecutor = Callable[..., Awaitable[Any]]


@dataclass(frozen=True)
class AgentTurnResult:
    """Result of one V2 agent task turn."""

    agent: AgentIdentity
    task: Task
    output: str
    next_sequence: int


async def _invoke_executor(
    executor: AgentTurnExecutor,
    task: Task,
    agent: AgentIdentity,
    *,
    sequence_start: int,
    inbox_messages: list[Any] | None = None,
    renew_task_lease: Callable[..., Awaitable[bool]] | None = None,
    on_stream_activity: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
) -> Any:
    """Invoke old two-arg executors or new sequence-aware executors."""
    try:
        signature = inspect.signature(executor)
    except (TypeError, ValueError):
        signature = None
    if signature is not None:
        kwargs: dict[str, Any] = {}
        if "sequence_start" in signature.parameters:
            kwargs["sequence_start"] = sequence_start
        if "inbox_messages" in signature.parameters:
            kwargs["inbox_messages"] = list(inbox_messages or [])
        if "renew_task_lease" in signature.parameters and renew_task_lease is not None:
            kwargs["renew_task_lease"] = renew_task_lease
        if "on_stream_activity" in signature.parameters and on_stream_activity is not None:
            kwargs["on_stream_activity"] = on_stream_activity
        if kwargs:
            return await executor(task, agent, **kwargs)
    return await executor(task, agent)


def _executor_accepts_inbox(executor: AgentTurnExecutor) -> bool:
    """Return whether executor can receive staged mailbox messages."""
    try:
        signature = inspect.signature(executor)
    except (TypeError, ValueError):
        return False
    return "inbox_messages" in signature.parameters


def _coerce_executor_result(result: Any, *, current_sequence: int) -> tuple[str, int]:
    """Extract output text and next event sequence from executor results."""
    output = getattr(result, "output", result)
    next_sequence = getattr(result, "next_sequence", current_sequence)
    normalized_output = str(output or "")
    if isinstance(next_sequence, int) and next_sequence > current_sequence:
        return normalized_output, next_sequence
    return normalized_output, current_sequence


async def run_agent_turn(
    *,
    run_id: str,
    project_id: str,
    agent: AgentIdentity,
    task: Task,
    lease_token: str,
    lease_expires_at: str,
    now: datetime,
    executor: AgentTurnExecutor,
    sequence_start: int,
    clock: Callable[[], datetime] | None = None,
    on_stream_activity: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
) -> AgentTurnResult:
    """Claim, execute, complete, and idle one agent task turn.

    This is intentionally small: it gives V2 a testable lifecycle seam before
    wiring the real AgentScope ReActAgent executor into the loop.
    """
    current_time = clock or (lambda: now)
    updated_at = now.isoformat()
    claimed = await task_pool.claim_task(
        run_id=run_id,
        task_id=task.id,
        agent_name=agent.agent_name,
        lease_token=lease_token,
        lease_expires_at=lease_expires_at,
        updated_at=updated_at,
    )
    if not claimed:
        raise RuntimeError(
            f"Shadow Clone V2 agent '{agent.agent_name}' could not claim task '{task.id}'."
        )
    claimed_task = await task_pool.read_task(run_id=run_id, task_id=task.id)
    claim_previous_version = task.plan_revision
    claim_new_version = (
        claimed_task.plan_revision
        if claimed_task is not None
        else claim_previous_version + 1
    )
    claim_attempt = (
        claimed_task.attempt if claimed_task is not None else task.attempt + 1
    )

    await event_log.append_event(
        run_id=run_id,
        thread_id=task.thread_id,
        project_id=project_id,
        sequence=sequence_start,
        event_type=EventType.TASK_CLAIMED,
        activity_owner=f"agent:{agent.agent_name}",
        payload={
            "task_id": task.id,
            "agent_name": agent.agent_name,
            "lease_expires_at": lease_expires_at,
            "attempt": claim_attempt,
            "previous_version": claim_previous_version,
            "new_version": claim_new_version,
        },
        created_at=updated_at,
    )
    sequence = sequence_start + 1
    current_lease_expires_at = lease_expires_at
    current_task_version = claim_new_version

    async def renew_current_task_lease(*, lease_expires_at: str) -> bool:
        nonlocal current_lease_expires_at, current_task_version, sequence
        renewed_at = current_time()
        renewed_at_text = renewed_at.isoformat()
        previous_expiry = current_lease_expires_at
        previous_version = current_task_version
        renewed = await task_pool.renew_task_lease(
            run_id=run_id,
            task_id=task.id,
            owner_agent=agent.agent_name,
            lease_token=lease_token,
            lease_expires_at=lease_expires_at,
            updated_at=renewed_at_text,
            now=renewed_at_text,
        )
        if not renewed:
            return False
        renewed_task = await task_pool.read_task(
            run_id=run_id,
            task_id=task.id,
        )
        if renewed_task is not None:
            current_task_version = renewed_task.plan_revision
            current_lease_expires_at = renewed_task.lease_expires_at or lease_expires_at
        else:
            current_task_version = previous_version + 1
            current_lease_expires_at = lease_expires_at
        await event_log.append_event(
            run_id=run_id,
            thread_id=task.thread_id,
            project_id=project_id,
            sequence=sequence,
            event_type=EventType.TASK_UPDATED,
            activity_owner=f"agent:{agent.agent_name}",
            payload={
                "task_id": task.id,
                "agent_name": agent.agent_name,
                "change_reason": "lease_renewed",
                "previous_lease_expires_at": previous_expiry,
                "lease_expires_at": current_lease_expires_at,
                "previous_version": previous_version,
                "new_version": current_task_version,
            },
            created_at=renewed_at_text,
        )
        sequence += 1
        return True

    try:
        inbox_messages: list[Any] = []
        if _executor_accepts_inbox(executor):
            inbox_result = await mailbox.read_next_messages_with_dead_letters(
                project_id=project_id,
                thread_id=task.thread_id,
                account_id=agent.account_id,
                agent_name=agent.agent_name,
            )
            for dead_lettered in inbox_result.dead_lettered:
                await event_log.append_event(
                    run_id=run_id,
                    thread_id=task.thread_id,
                    project_id=project_id,
                    sequence=sequence,
                    event_type=EventType.MAILBOX_DEAD_LETTERED,
                    activity_owner=f"agent:{agent.agent_name}",
                    payload={
                        "message_id": dead_lettered.id,
                        "agent_name": dead_lettered.recipient,
                        "reason": dead_lettered.dead_letter_reason,
                    },
                    created_at=updated_at,
                )
                sequence += 1
            inbox_messages = list(inbox_result.messages)
        executor_result = await _invoke_executor(
            executor,
            task,
            agent,
            sequence_start=sequence,
            inbox_messages=inbox_messages,
            renew_task_lease=renew_current_task_lease,
            on_stream_activity=on_stream_activity,
        )
        output, sequence = _coerce_executor_result(
            executor_result,
            current_sequence=sequence,
        )
        for message in inbox_messages:
            ack = await mailbox.ack_message(
                project_id=project_id,
                thread_id=task.thread_id,
                account_id=agent.account_id,
                agent_name=agent.agent_name,
                message_id=message.id,
                acked_by=agent.agent_name,
                acked_at=updated_at,
            )
            if ack.already_acked:
                continue
            await event_log.append_event(
                run_id=run_id,
                thread_id=task.thread_id,
                project_id=project_id,
                sequence=sequence,
                event_type=EventType.MAILBOX_ACKED,
                activity_owner=f"agent:{agent.agent_name}",
                payload={
                    "message_id": ack.message_id,
                    "agent_name": ack.agent_name,
                    "acked_by": ack.acked_by,
                    "already_acked": ack.already_acked,
                },
                created_at=updated_at,
            )
            sequence += 1
    except Exception as exc:
        exception_next_sequence = getattr(exc, "next_sequence", None)
        if (
            isinstance(exception_next_sequence, int)
            and exception_next_sequence > sequence
        ):
            sequence = exception_next_sequence
        failed_at = current_time()
        failed_at_text = failed_at.isoformat()
        task_marked_failed = await task_pool.fail_task(
            run_id=run_id,
            task_id=task.id,
            owner_agent=agent.agent_name,
            lease_token=lease_token,
            error_type=type(exc).__name__,
            error_message=str(exc),
            updated_at=failed_at_text,
            now=failed_at_text,
        )
        if task_marked_failed:
            await event_log.append_event(
                run_id=run_id,
                thread_id=task.thread_id,
                project_id=project_id,
                sequence=sequence,
                event_type=EventType.TASK_FAILED,
                activity_owner=f"agent:{agent.agent_name}",
                payload={
                    "task_id": task.id,
                    "agent_name": agent.agent_name,
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "task_marked_failed": task_marked_failed,
                },
                created_at=failed_at_text,
            )
        else:
            await event_log.append_event(
                run_id=run_id,
                thread_id=task.thread_id,
                project_id=project_id,
                sequence=sequence,
                event_type=EventType.TASK_UPDATED,
                activity_owner=f"agent:{agent.agent_name}",
                payload={
                    "task_id": task.id,
                    "agent_name": agent.agent_name,
                    "change_reason": "lease_lost_failure",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "task_marked_failed": task_marked_failed,
                },
                created_at=failed_at_text,
            )
        idle_agent = agent_registry.mark_agent_idle(agent, now=failed_at)
        await event_log.append_event(
            run_id=run_id,
            thread_id=task.thread_id,
            project_id=project_id,
            sequence=sequence + 1,
            event_type=EventType.AGENT_IDLE,
            activity_owner=f"agent:{agent.agent_name}",
            payload={
                "agent_name": idle_agent.agent_name,
                "idle_since": idle_agent.idle_since,
                "idle_expires_at": idle_agent.idle_expires_at,
            },
            created_at=failed_at_text,
        )
        try:
            setattr(exc, "next_sequence", sequence + 2)
        except Exception:
            pass
        raise

    finished_at = current_time()
    finished_at_text = finished_at.isoformat()
    completed = await task_pool.complete_task(
        run_id=run_id,
        task_id=task.id,
        owner_agent=agent.agent_name,
        lease_token=lease_token,
        result_ref=output,
        updated_at=finished_at_text,
        now=finished_at_text,
    )
    if not completed:
        current_task = await task_pool.read_task(run_id=run_id, task_id=task.id)
        if (
            current_task is not None
            and current_task.status == TaskStatus.COMPLETED
            and current_task.owner_agent == agent.agent_name
        ):
            current_result_ref = str(current_task.result_ref or output or "")
            completed_payload = {
                "task_id": task.id,
                "agent_name": agent.agent_name,
                "result_ref": current_result_ref,
            }
            if output:
                completed_payload["result_summary"] = str(output)
                completed_payload["output"] = str(output)
            await event_log.append_event(
                run_id=run_id,
                thread_id=task.thread_id,
                project_id=project_id,
                sequence=sequence,
                event_type=EventType.TASK_COMPLETED,
                activity_owner=f"agent:{agent.agent_name}",
                payload=completed_payload,
                created_at=finished_at_text,
            )
            sequence += 1
            idle_agent = agent_registry.mark_agent_idle(agent, now=finished_at)
            await event_log.append_event(
                run_id=run_id,
                thread_id=task.thread_id,
                project_id=project_id,
                sequence=sequence,
                event_type=EventType.AGENT_IDLE,
                activity_owner=f"agent:{agent.agent_name}",
                payload={
                    "agent_name": idle_agent.agent_name,
                    "idle_since": idle_agent.idle_since,
                    "idle_expires_at": idle_agent.idle_expires_at,
                },
                created_at=finished_at_text,
            )
            return AgentTurnResult(
                agent=idle_agent,
                task=current_task,
                output=str(output or current_task.result_ref or ""),
                next_sequence=sequence + 1,
            )
        error = RuntimeError(
            f"Shadow Clone V2 agent '{agent.agent_name}' lost lease for task '{task.id}'."
        )
        try:
            setattr(error, "next_sequence", sequence)
        except Exception:
            pass
        raise error

    await event_log.append_event(
        run_id=run_id,
        thread_id=task.thread_id,
        project_id=project_id,
        sequence=sequence,
        event_type=EventType.TASK_COMPLETED,
        activity_owner=f"agent:{agent.agent_name}",
        payload={
            "task_id": task.id,
            "agent_name": agent.agent_name,
            "result_ref": output,
        },
        created_at=finished_at_text,
    )
    sequence += 1

    idle_agent = agent_registry.mark_agent_idle(agent, now=finished_at)
    await event_log.append_event(
        run_id=run_id,
        thread_id=task.thread_id,
        project_id=project_id,
        sequence=sequence,
        event_type=EventType.AGENT_IDLE,
        activity_owner=f"agent:{agent.agent_name}",
        payload={
            "agent_name": idle_agent.agent_name,
            "idle_since": idle_agent.idle_since,
            "idle_expires_at": idle_agent.idle_expires_at,
        },
        created_at=finished_at_text,
    )
    sequence += 1

    return AgentTurnResult(
        agent=idle_agent,
        task=task,
        output=output,
        next_sequence=sequence,
    )


async def project_expired_task_lease_recovery_events(
    *,
    run_id: str,
    thread_id: str,
    project_id: str,
    now: str,
    ttl_seconds: int,
    sequence_start: int,
) -> int:
    """Sweep expired task leases and project replayable recovery events."""
    sequence = sequence_start
    sweep_results = await task_pool.sweep_expired_task_leases(
        run_id=run_id,
        now=now,
        ttl_seconds=ttl_seconds,
    )
    for result in sweep_results:
        task = result.task
        previous_version = max(0, task.plan_revision - 1)
        payload = {
            "task_id": task.id,
            "change_reason": (
                "lease_expired_failed"
                if result.action == "failed"
                else "lease_expired_recovered"
            ),
            "previous_status": result.previous_status,
            "status": (
                task.status.value if hasattr(task.status, "value") else task.status
            ),
            "expired_owner_agent": result.previous_owner_agent,
            "expired_lease_expires_at": result.previous_lease_expires_at,
            "attempt": task.attempt,
            "max_attempts": task.max_attempts,
            "previous_version": previous_version,
            "new_version": task.plan_revision,
            "error_type": str(task.error.get("error_type") or "LeaseExpired"),
        }
        await event_log.append_event(
            run_id=run_id,
            thread_id=thread_id,
            project_id=project_id,
            sequence=sequence,
            event_type=(
                EventType.TASK_FAILED
                if result.action == "failed"
                else EventType.TASK_UPDATED
            ),
            activity_owner="runtime",
            payload=payload,
            created_at=now,
        )
        sequence += 1
    return sequence
