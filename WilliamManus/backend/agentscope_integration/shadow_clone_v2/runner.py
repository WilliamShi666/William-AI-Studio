"""AgentScope-runner-compatible facade for Shadow Clone V2."""

from __future__ import annotations

import asyncio
import inspect
import json
from datetime import datetime, timedelta, timezone
from typing import Any, AsyncGenerator, Awaitable, Callable, Dict, List, Optional
from uuid import uuid4

from agentscope_integration.shadow_clone.constants import ShadowCloneMode

from . import agent_loop, agent_registry, event_log, task_pool, team_store
from .facilitator import (
    build_dynamic_team_hint,
    normalize_dynamic_team_plan,
)
from .agent_tools import ShadowCloneV2AgentTools, ShadowCloneV2ToolContext
from .constants import (
    SHADOW_CLONE_V2_MAX_PARALLEL_SUBAGENTS,
    SHADOW_CLONE_V2_STATE_TTL_SECONDS,
)
from .main_agent_planner import (
    AgentScopeShadowCloneV2MainAgentSynthesizer,
    AgentScopeShadowCloneV2MainAgentPlanner,
    MainAgentSynthesisResult,
    MainAgentToolPlanResult,
    ShadowCloneV2MainAgentPlanner,
    ShadowCloneV2MainAgentSynthesizer,
)
from .models import (
    AgentIdentity,
    AgentLifecycleStatus,
    EventType,
    Task,
    TaskStatus,
    TeamConfig,
)
from .projection import project_shadow_clone_v2_public_state
from .task_executor import (
    AgentScopeShadowCloneV2TaskExecutor,
    ShadowCloneV2RunContext,
    ShadowCloneV2TaskExecutor,
)


class EmptyFinalSynthesisOutput(RuntimeError):
    """Final synthesis completed without a user-facing answer."""


class ShadowCloneV2Runner:
    """Thin V2 runner shell.

    The full facilitator/agent-loop implementation is built incrementally behind
    this facade so routing can be tested without touching the V1 runner.
    """

    def __init__(
        self,
        *,
        thread_id: str,
        project_id: str,
        agent_run_id: str,
        model_key: str,
        db_client,
        mode: ShadowCloneMode,
        account_id: str | None = None,
        subagent_model: Optional[str] = None,
        trace=None,
        task_executor: ShadowCloneV2TaskExecutor | None = None,
        main_agent_planner: ShadowCloneV2MainAgentPlanner | None = None,
        main_agent_synthesizer: ShadowCloneV2MainAgentSynthesizer | None = None,
    ) -> None:
        self.thread_id = thread_id
        self.project_id = project_id
        self.account_id = account_id
        self.agent_run_id = agent_run_id
        self.model_key = model_key
        self.db_client = db_client
        self.mode = mode
        self.subagent_model = subagent_model
        self.trace = trace
        self.task_executor = task_executor or AgentScopeShadowCloneV2TaskExecutor()
        self.main_agent_planner = (
            main_agent_planner or AgentScopeShadowCloneV2MainAgentPlanner()
        )
        self.main_agent_synthesizer = (
            main_agent_synthesizer or AgentScopeShadowCloneV2MainAgentSynthesizer()
        )

    @staticmethod
    def _build_ui_subtasks(
        tasks: list[Task],
        members: list[AgentIdentity],
        *,
        status: str = "pending",
        status_by_task_id: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        """Project V2 tasks into the existing Shadow Clone frontend ledger shape."""
        members_by_name = {member.agent_name: member for member in members}
        status_by_task_id = status_by_task_id or {}
        subtasks: list[dict[str, Any]] = []
        for task in tasks:
            metadata = task.metadata if isinstance(task.metadata, dict) else {}
            agent_name = str(metadata.get("agent_name") or "").strip()
            member = members_by_name.get(agent_name)
            subtasks.append(
                {
                    "id": task.id,
                    "role": task.subject or (member.role if member else ""),
                    "task_description": task.description,
                    "status": status_by_task_id.get(task.id, status),
                    "agent_name": agent_name,
                }
            )
        return subtasks

    def _build_subagent_activity_event(
        self,
        *,
        task: Task,
        member: AgentIdentity,
        output: str,
        thread_run_id: str,
        sequence: int,
        timestamp: str,
    ) -> dict[str, Any]:
        """Project a V2 worker result into the existing subagent inspection stream."""
        return {
            "type": "subagent_activity",
            "status": "running",
            "source": "shadow_clone_v2",
            "shadow_clone_mode": self.mode.value,
            "thread_run_id": thread_run_id,
            "agent_run_id": self.agent_run_id,
            "ui_phase": "subagents_running",
            "activity_owner": "shadow_clone",
            "phase_reason": "shadow_clone_v2_subagent_activity",
            "subtask_id": task.id,
            "sequence": sequence,
            "role": task.subject or member.role,
            "message_type": "assistant",
            "content": {
                "role": "assistant",
                "content": output,
            },
            "metadata": {
                "stream_status": "complete",
                "source": "shadow_clone_v2",
                "subtask_id": task.id,
                "agent_name": member.agent_name,
            },
            "created_at": timestamp,
            "updated_at": timestamp,
        }

    def _build_main_agent_final_assistant_event(
        self,
        *,
        output: str,
        thread_run_id: str,
        sequence: int,
        timestamp: str,
    ) -> dict[str, Any]:
        """Emit a normal assistant message so the primary thread shows output."""
        return {
            "sequence": sequence,
            "message_id": f"shadow-clone-v2-final-{self.agent_run_id}-{uuid4().hex[:12]}",
            "thread_id": self.thread_id,
            "project_id": self.project_id,
            "type": "assistant",
            "is_llm_message": True,
            "content": json.dumps(
                {
                    "role": "assistant",
                    "content": output,
                },
                ensure_ascii=False,
            ),
            "metadata": json.dumps(
                {
                    "stream_status": "complete",
                    "thread_run_id": thread_run_id,
                    "agent_run_id": self.agent_run_id,
                    "shadow_clone_mode": self.mode.value,
                    "activity_owner": "main_agent",
                    "ui_phase": "main_agent_continuation",
                    "phase_reason": "shadow_clone_v2_final_output",
                },
                ensure_ascii=False,
            ),
            "created_at": timestamp,
            "updated_at": timestamp,
        }

    def _build_main_agent_planning_assistant_event(
        self,
        *,
        output: str,
        thread_run_id: str,
        sequence: int,
        timestamp: str,
    ) -> dict[str, Any]:
        """Emit real main-agent planning text as a normal assistant message."""
        return {
            "sequence": sequence,
            "message_id": f"shadow-clone-v2-planning-{self.agent_run_id}-{uuid4().hex[:12]}",
            "thread_id": self.thread_id,
            "project_id": self.project_id,
            "type": "assistant",
            "is_llm_message": True,
            "content": json.dumps(
                {
                    "role": "assistant",
                    "content": output,
                },
                ensure_ascii=False,
            ),
            "metadata": json.dumps(
                {
                    "stream_status": "complete",
                    "thread_run_id": thread_run_id,
                    "agent_run_id": self.agent_run_id,
                    "shadow_clone_mode": self.mode.value,
                    "activity_owner": "main_agent",
                    "ui_phase": "planning",
                    "phase_reason": "shadow_clone_v2_main_agent_planning_output",
                },
                ensure_ascii=False,
            ),
            "created_at": timestamp,
            "updated_at": timestamp,
        }

    def _build_main_agent_progress_event(
        self,
        *,
        phase_key: str,
        message: str,
        thread_run_id: str,
        sequence: int,
        timestamp: str,
        ui_phase: str,
        phase_reason: str,
        activity_owner: str = "shadow_clone",
        extra_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Emit a bounded, user-visible Shadow Clone V2 progress message."""
        return {
            "sequence": sequence,
            "message_id": f"shadow-clone-v2-progress-{self.agent_run_id}-{phase_key}",
            "thread_id": self.thread_id,
            "type": "assistant",
            "is_llm_message": False,
            "content": json.dumps(
                {
                    "role": "assistant",
                    "content": f"[系统进度] {message}",
                },
                ensure_ascii=False,
            ),
            "metadata": json.dumps(
                {
                    "stream_status": "complete",
                    "thread_run_id": thread_run_id,
                    "agent_run_id": self.agent_run_id,
                    "shadow_clone_mode": self.mode.value,
                    "source": "shadow_clone_v2",
                    "activity_owner": activity_owner,
                    "ui_phase": ui_phase,
                    "phase_reason": phase_reason,
                    "shadow_clone_system_progress": True,
                    "message_kind": "orchestration_progress",
                    **(extra_metadata or {}),
                },
                ensure_ascii=False,
            ),
            "created_at": timestamp,
            "updated_at": timestamp,
        }

    async def _append_planning_failed_event(
        self,
        *,
        thread_run_id: str,
        error: Exception,
        sequence: int,
    ) -> None:
        """Record a controlled planning failure instead of leaking stream errors."""
        await event_log.append_event(
            run_id=self.agent_run_id,
            thread_id=self.thread_id,
            project_id=self.project_id,
            sequence=sequence,
            event_type=EventType.RUN_FAILED,
            activity_owner="facilitator",
            payload={
                "failure_stage": "planning",
                "thread_run_id": thread_run_id,
                "error_type": type(error).__name__,
                "error": str(error),
            },
            created_at=datetime.now(timezone.utc).isoformat(),
        )

    def _build_planning_failed_status(
        self,
        *,
        thread_run_id: str,
        error: Exception,
    ) -> dict[str, Any]:
        """Build the terminal SSE/status projection for planning failure."""
        return {
            "type": "shadow_clone_v2_planning_failed",
            "status": "failed",
            "shadow_clone_mode": self.mode.value,
            "thread_run_id": thread_run_id,
            "agent_run_id": self.agent_run_id,
            "ui_phase": "failed",
            "activity_owner": "none",
            "phase_reason": "shadow_clone_v2_planning_failed",
            "error": str(error),
            "failure_class": type(error).__name__,
        }

    async def _append_synthesis_failed_event(
        self,
        *,
        thread_run_id: str,
        error: Exception,
        sequence: int,
        completed_task_count: int,
    ) -> None:
        """Record a durable terminal failure for final synthesis errors."""
        await event_log.append_event(
            run_id=self.agent_run_id,
            thread_id=self.thread_id,
            project_id=self.project_id,
            sequence=sequence,
            event_type=EventType.RUN_FAILED,
            activity_owner="main_agent",
            payload={
                "failure_stage": "final_synthesis",
                "thread_run_id": thread_run_id,
                "completed_task_count": completed_task_count,
                "error_type": type(error).__name__,
                "error": str(error),
            },
            created_at=datetime.now(timezone.utc).isoformat(),
        )

    def _build_synthesis_failed_status(
        self,
        *,
        thread_run_id: str,
        error: Exception,
        completed_task_count: int,
        tasks: list[Task],
        team: TeamConfig,
    ) -> dict[str, Any]:
        """Build the terminal status yielded when final synthesis fails."""
        return {
            "type": "shadow_clone_v2_synthesis_failed",
            "status": "failed",
            "shadow_clone_mode": self.mode.value,
            "thread_run_id": thread_run_id,
            "agent_run_id": self.agent_run_id,
            "ui_phase": "failed",
            "activity_owner": "main_agent",
            "phase_reason": "shadow_clone_v2_final_synthesis_failed",
            "failure_stage": "final_synthesis",
            "completed_task_count": completed_task_count,
            "error": str(error),
            "failure_class": type(error).__name__,
            "subtasks": self._build_ui_subtasks(
                tasks,
                team.members,
                status_by_task_id={
                    task.id: str(getattr(task.status, "value", task.status))
                    for task in tasks
                },
            ),
        }

    async def _load_tool_created_plan(
        self,
        *,
        tools: ShadowCloneV2AgentTools,
        plan_result: MainAgentToolPlanResult,
    ) -> tuple[TeamConfig, list[Task], int]:
        """Validate planner output against durable tool-created state."""
        if plan_result.next_sequence != tools.next_sequence:
            raise RuntimeError(
                "main-agent planning sequence mismatch: "
                f"planner={plan_result.next_sequence} tools={tools.next_sequence}"
            )
        team = await team_store.read_team(
            project_id=self.project_id,
            thread_id=self.thread_id,
            account_id=self.account_id,
        )
        if team is None:
            raise RuntimeError("main-agent planning durable team is missing")
        tasks = list(await task_pool.list_tasks(run_id=self.agent_run_id))
        if not tasks:
            raise RuntimeError("main-agent planning durable tasks are missing")
        _validate_tool_plan_consistency(
            returned_team=plan_result.team,
            returned_tasks=list(plan_result.tasks),
            durable_team=team,
            durable_tasks=tasks,
        )
        return team, tasks, tools.next_sequence

    async def _load_event_log_message_context(self) -> list[dict[str, Any]]:
        """Load peer message state through the V2 event-log projection."""
        try:
            events = await event_log.read_events(self.agent_run_id, start="-")
            projection = project_shadow_clone_v2_public_state(
                agent_run_id=self.agent_run_id,
                events=events,
                parent_status=None,
            )
        except Exception:
            return []
        messages = projection.get("messages") if isinstance(projection, dict) else {}
        if isinstance(messages, dict):
            return [
                dict(message)
                for _message_id, message in sorted(
                    messages.items(), key=lambda item: str(item[0])
                )
                if isinstance(message, dict)
            ]
        if isinstance(messages, list):
            return [dict(message) for message in messages if isinstance(message, dict)]
        return []

    async def _mark_completed_task_owners_idle(
        self,
        *,
        team: TeamConfig,
        tasks: list[Task],
        sequence_start: int,
    ) -> tuple[TeamConfig, int]:
        """Ensure tool-completed task owners enter the replayed idle lifecycle."""
        member_by_name = {member.agent_name: member for member in team.members}
        next_sequence = sequence_start
        updated_team = team
        idled_agent_names: set[str] = set()
        latest_lifecycle_by_agent = await self._load_existing_agent_lifecycle_markers()
        for task in tasks:
            if task.status != TaskStatus.COMPLETED:
                continue
            agent_name = str(task.owner_agent or "").strip()
            if not agent_name:
                metadata = task.metadata if isinstance(task.metadata, dict) else {}
                agent_name = str(metadata.get("agent_name") or "").strip()
            if not agent_name or agent_name in idled_agent_names:
                continue
            latest_lifecycle = latest_lifecycle_by_agent.get(agent_name)
            if latest_lifecycle in {"idle", "closed"}:
                continue
            member = member_by_name.get(agent_name)
            if member is None or member.status in {
                AgentLifecycleStatus.IDLE,
                AgentLifecycleStatus.CLOSED,
                AgentLifecycleStatus.SHUTTING_DOWN,
                AgentLifecycleStatus.FAILED,
            }:
                continue
            idled_at = datetime.now(timezone.utc)
            idle_agent = agent_registry.mark_agent_idle(member, now=idled_at)
            updated_team = team_store.replace_team_member(updated_team, idle_agent)
            member_by_name[agent_name] = idle_agent
            idled_agent_names.add(agent_name)
            await event_log.append_event(
                run_id=self.agent_run_id,
                thread_id=self.thread_id,
                project_id=self.project_id,
                sequence=next_sequence,
                event_type=EventType.AGENT_IDLE,
                activity_owner=f"agent:{agent_name}",
                payload={
                    "agent_name": idle_agent.agent_name,
                    "idle_since": idle_agent.idle_since,
                    "idle_expires_at": idle_agent.idle_expires_at,
                    "task_id": task.id,
                    "reason": "completed_task_owner_finalization",
                },
                created_at=idled_at.isoformat(),
            )
            next_sequence += 1
        if updated_team is not team:
            await team_store.store_team(
                updated_team,
                ttl_seconds=SHADOW_CLONE_V2_STATE_TTL_SECONDS,
            )
        return updated_team, next_sequence

    async def _load_existing_agent_lifecycle_markers(self) -> dict[str, str]:
        """Return latest agent lifecycle state by agent name."""
        try:
            events = await event_log.read_events(self.agent_run_id, start="-")
        except Exception:
            return {}
        latest_lifecycle_by_agent: dict[str, str] = {}
        ordered_events = sorted(
            enumerate(events),
            key=lambda item: (
                _event_sequence(item[1]),
                str(getattr(item[1], "id", "") or ""),
                item[0],
            ),
        )
        for _index, event in ordered_events:
            event_type = getattr(event, "type", None)
            event_type_value = getattr(event_type, "value", event_type)
            payload = getattr(event, "payload", None)
            if not isinstance(payload, dict):
                continue
            agent_name = str(payload.get("agent_name") or "").strip()
            if not agent_name:
                continue
            if event_type_value == EventType.AGENT_IDLE.value:
                latest_lifecycle_by_agent[agent_name] = "idle"
            elif event_type_value in {
                EventType.AGENT_WAKE.value,
                EventType.AGENT_SPAWNED.value,
            }:
                latest_lifecycle_by_agent[agent_name] = "active"
            elif event_type_value == EventType.AGENT_SHUTDOWN.value:
                latest_lifecycle_by_agent[agent_name] = "closed"
        return latest_lifecycle_by_agent

    async def run(
        self,
        user_message: str,
        thread_run_id: str,
        user_media_refs: Optional[List[Dict[str, Any]]] = None,
        resume_strategy: str = "auto",
        resume_window_minutes: int = 1440,
        dynamic_subtasks: Optional[List[Dict[str, Any]]] = None,
        execute_dynamic_plan: bool = False,
    ) -> AsyncGenerator[dict, None]:
        """Start a V2 run and emit the first replayable status event."""
        dynamic_team_hint = build_dynamic_team_hint()
        created_at = datetime.now(timezone.utc).isoformat()
        await event_log.append_event(
            run_id=self.agent_run_id,
            thread_id=self.thread_id,
            project_id=self.project_id,
            sequence=1,
            event_type=EventType.RUN_STARTED,
            activity_owner="facilitator",
            payload={
                "thread_run_id": thread_run_id,
                "model_key": self.model_key,
                "shadow_clone_mode": self.mode.value,
                "dynamic_team_hint": dynamic_team_hint,
                "resume_strategy": resume_strategy,
                "resume_window_minutes": resume_window_minutes,
                "has_user_media_refs": bool(user_media_refs),
                "user_message_preview": str(user_message or "")[:500],
            },
            created_at=created_at,
        )
        yield {
            "type": "shadow_clone_v2_started",
            "status": "running",
            "message": "Shadow Clone V2 run started.",
            "shadow_clone_mode": self.mode.value,
            "thread_run_id": thread_run_id,
            "agent_run_id": self.agent_run_id,
            "ui_phase": "planning",
            "activity_owner": "shadow_clone",
            "phase_reason": "shadow_clone_v2_started",
            "dynamic_team_hint": dynamic_team_hint,
        }
        yield self._build_main_agent_progress_event(
            phase_key="started",
            message="我已收到请求，正在创建团队并规划任务。",
            thread_run_id=thread_run_id,
            sequence=1,
            timestamp=created_at,
            ui_phase="planning",
            phase_reason="shadow_clone_v2_started_progress",
        )

        tool_driven_plan = False
        if dynamic_subtasks is None:
            tools = ShadowCloneV2AgentTools(
                context=ShadowCloneV2ToolContext(
                    run_id=self.agent_run_id,
                    thread_id=self.thread_id,
                    project_id=self.project_id,
                    account_id=self.account_id,
                    actor_name=f"facilitator@{self.thread_id}",
                    sequence_start=2,
                    state_ttl_seconds=SHADOW_CLONE_V2_STATE_TTL_SECONDS,
                    current_user_message=user_message,
                )
            )
            try:
                plan_result = await self.main_agent_planner.plan(
                    user_message=user_message,
                    user_media_refs=list(user_media_refs or []),
                    tools=tools,
                    model_key=self.model_key,
                    thread_run_id=thread_run_id,
                    created_at=created_at,
                    dynamic_team_hint=dynamic_team_hint,
                    db_client=self.db_client,
                    trace=self.trace,
                )
                team, tasks, sequence = await self._load_tool_created_plan(
                    tools=tools,
                    plan_result=plan_result,
                )
            except Exception as exc:
                failure_sequence = max(2, tools.next_sequence)
                await self._append_planning_failed_event(
                    thread_run_id=thread_run_id,
                    error=exc,
                    sequence=failure_sequence,
                )
                yield self._build_planning_failed_status(
                    thread_run_id=thread_run_id,
                    error=exc,
                )
                return
            should_execute_dynamic_plan = bool(plan_result.execute_dynamic_plan)
            tool_driven_plan = True
            planning_output = str(getattr(plan_result, "output", "") or "").strip()
            if planning_output:
                yield self._build_main_agent_planning_assistant_event(
                    output=planning_output,
                    thread_run_id=thread_run_id,
                    sequence=sequence,
                    timestamp=datetime.now(timezone.utc).isoformat(),
                )
                sequence += 1
        else:
            should_execute_dynamic_plan = execute_dynamic_plan
            team, tasks = normalize_dynamic_team_plan(
                run_id=self.agent_run_id,
                thread_id=self.thread_id,
                project_id=self.project_id,
                subtasks=dynamic_subtasks,
                created_at=created_at,
            )
            sequence = 2

        reused_agent_names: set[str] = set()
        expired_idle_agents: list[AgentIdentity] = []
        if not tool_driven_plan:
            created_at_dt = datetime.fromisoformat(created_at)
            existing_team = await team_store.read_team(
                project_id=self.project_id,
                thread_id=self.thread_id,
                account_id=self.account_id,
            )
        else:
            existing_team = None
        if existing_team is not None:
            expired_idle_agents = agent_registry.find_expired_idle_agents(
                existing_team.members,
                now=created_at_dt,
            )
            expired_idle_agent_names = {
                agent.agent_name for agent in expired_idle_agents
            }
            reusable_idle_by_name = {
                agent.agent_name: agent
                for agent in existing_team.members
                if agent.status == AgentLifecycleStatus.IDLE
                and agent.agent_name not in expired_idle_agent_names
            }
            resolved_members: list[AgentIdentity] = []
            for member in team.members:
                reusable_agent = reusable_idle_by_name.get(member.agent_name)
                if reusable_agent is None:
                    resolved_members.append(member)
                    continue
                reused_agent_names.add(reusable_agent.agent_name)
                resolved_members.append(
                    reusable_agent.model_copy(
                        update={
                            "status": AgentLifecycleStatus.WORKING,
                            "current_run_id": self.agent_run_id,
                            "idle_since": None,
                            "idle_expires_at": None,
                        }
                    )
                )
            team = team.model_copy(update={"members": resolved_members})
        if self.account_id and team.account_id != self.account_id:
            team = team.model_copy(
                update={
                    "account_id": self.account_id,
                    "members": [
                        member.model_copy(update={"account_id": self.account_id})
                        for member in team.members
                    ],
                }
            )
        if not tool_driven_plan:
            await team_store.store_team(
                team,
                ttl_seconds=SHADOW_CLONE_V2_STATE_TTL_SECONDS,
            )
            for expired_agent in expired_idle_agents:
                closed_agent = expired_agent.model_copy(
                    update={
                        "status": AgentLifecycleStatus.CLOSED,
                        "current_run_id": self.agent_run_id,
                    }
                )
                await event_log.append_event(
                    run_id=self.agent_run_id,
                    thread_id=self.thread_id,
                    project_id=self.project_id,
                    sequence=sequence,
                    event_type=EventType.AGENT_SHUTDOWN,
                    activity_owner="facilitator",
                    payload=closed_agent.model_dump(mode="json"),
                    created_at=created_at,
                )
                sequence += 1
            for member in team.members:
                member_event_type = (
                    EventType.AGENT_WAKE
                    if member.agent_name in reused_agent_names
                    else EventType.AGENT_SPAWNED
                )
                await event_log.append_event(
                    run_id=self.agent_run_id,
                    thread_id=self.thread_id,
                    project_id=self.project_id,
                    sequence=sequence,
                    event_type=member_event_type,
                    activity_owner="facilitator",
                    payload=member.model_dump(mode="json"),
                    created_at=created_at,
                )
                sequence += 1
            for task in tasks:
                await task_pool.store_task(
                    task,
                    ttl_seconds=SHADOW_CLONE_V2_STATE_TTL_SECONDS,
                )
                await event_log.append_event(
                    run_id=self.agent_run_id,
                    thread_id=self.thread_id,
                    project_id=self.project_id,
                    sequence=sequence,
                    event_type=EventType.TASK_CREATED,
                    activity_owner="facilitator",
                    payload=task.model_dump(mode="json"),
                    created_at=created_at,
                )
                sequence += 1
        yield self._build_main_agent_progress_event(
            phase_key="plan-created",
            message=(
                f"我已创建 {len(team.members)} 个团队成员并分配 "
                f"{len(tasks)} 个任务，接下来会协调他们执行。"
            ),
            thread_run_id=thread_run_id,
            sequence=sequence,
            timestamp=datetime.now(timezone.utc).isoformat(),
            ui_phase="subagents_running",
            phase_reason="shadow_clone_v2_plan_progress",
            extra_metadata={
                "subagent_count": len(team.members),
                "task_count": len(tasks),
            },
        )
        yield {
            "type": "shadow_clone_v2_plan_created",
            "status": "running",
            "shadow_clone_mode": self.mode.value,
            "thread_run_id": thread_run_id,
            "agent_run_id": self.agent_run_id,
            "ui_phase": "subagents_running",
            "activity_owner": "shadow_clone",
            "phase_reason": "shadow_clone_v2_plan_created",
            "subagent_count": len(team.members),
            "task_count": len(tasks),
            "subtasks": self._build_ui_subtasks(tasks, team.members),
            "dependencies": [],
        }

        if not should_execute_dynamic_plan:
            return

        sweep_started_at = datetime.now(timezone.utc).isoformat()
        next_sequence = await agent_loop.project_expired_task_lease_recovery_events(
            run_id=self.agent_run_id,
            thread_id=self.thread_id,
            project_id=self.project_id,
            now=sweep_started_at,
            ttl_seconds=SHADOW_CLONE_V2_STATE_TTL_SECONDS,
            sequence_start=sequence,
        )
        if next_sequence > sequence:
            sequence = next_sequence
            refreshed_tasks = await task_pool.list_tasks(run_id=self.agent_run_id)
            if refreshed_tasks:
                tasks = refreshed_tasks

        task_by_agent = {
            str(task.metadata.get("agent_name") or ""): task
            for task in tasks
            if task.status == "pending"
        }
        failed_tasks = [
            task
            for task in tasks
            if str(getattr(task.status, "value", task.status)) == "failed"
        ]
        blocked_tasks = [
            task
            for task in tasks
            if str(getattr(task.status, "value", task.status)) == "blocked"
        ]
        if failed_tasks:
            failed_task = failed_tasks[0]
            await event_log.append_event(
                run_id=self.agent_run_id,
                thread_id=self.thread_id,
                project_id=self.project_id,
                sequence=sequence,
                event_type=EventType.RUN_FAILED,
                activity_owner="facilitator",
                payload={
                    "failed_task_id": failed_task.id,
                    "completed_task_count": 0,
                    "error_type": "TaskFailedAfterRecovery",
                    "error": "Task recovery left one or more tasks failed.",
                },
                created_at=datetime.now(timezone.utc).isoformat(),
            )
            yield {
                "type": "shadow_clone_v2_execution_failed",
                "status": "failed",
                "shadow_clone_mode": self.mode.value,
                "thread_run_id": thread_run_id,
                "agent_run_id": self.agent_run_id,
                "ui_phase": "failed",
                "activity_owner": "none",
                "phase_reason": "shadow_clone_v2_task_failed_after_recovery",
                "executed_task_count": 0,
                "failed_task_id": failed_task.id,
                "failed_task_ids": [task.id for task in failed_tasks],
                "blocked_task_ids": [task.id for task in blocked_tasks],
                "subtasks": self._build_ui_subtasks(
                    tasks,
                    team.members,
                    status_by_task_id={
                        task.id: str(getattr(task.status, "value", task.status))
                        for task in tasks
                    },
                ),
            }
            return
        if blocked_tasks and not task_by_agent:
            await event_log.append_event(
                run_id=self.agent_run_id,
                thread_id=self.thread_id,
                project_id=self.project_id,
                sequence=sequence,
                event_type=EventType.RUN_FAILED,
                activity_owner="facilitator",
                payload={
                    "blocked_task_ids": [task.id for task in blocked_tasks],
                    "completed_task_count": 0,
                    "error_type": "TaskBlockedAfterRecovery",
                    "error": "Task recovery left work blocked with no claimable tasks.",
                },
                created_at=datetime.now(timezone.utc).isoformat(),
            )
            yield {
                "type": "shadow_clone_v2_execution_blocked",
                "status": "blocked",
                "shadow_clone_mode": self.mode.value,
                "thread_run_id": thread_run_id,
                "agent_run_id": self.agent_run_id,
                "ui_phase": "blocked",
                "activity_owner": "none",
                "phase_reason": "shadow_clone_v2_task_blocked_after_recovery",
                "executed_task_count": 0,
                "blocked_task_ids": [task.id for task in blocked_tasks],
                "subtasks": self._build_ui_subtasks(
                    tasks,
                    team.members,
                    status_by_task_id={
                        task.id: str(getattr(task.status, "value", task.status))
                        for task in tasks
                    },
                ),
            }
            return
        executed_task_count = 0
        worker_outputs: list[dict[str, Any]] = []
        member_by_name = {member.agent_name: member for member in team.members}
        completed_task_ids = {
            task.id
            for task in tasks
            if str(getattr(task.status, "value", task.status)) == "completed"
        }
        attempted_task_ids: set[str] = set()
        task_by_id = {task.id: task for task in tasks}
        pending_tasks_by_id = {
            task.id: task
            for task in tasks
            if str(getattr(task.status, "value", task.status))
            in {"pending", "blocked"}
        }
        parallel_limit = max(1, int(SHADOW_CLONE_V2_MAX_PARALLEL_SUBAGENTS or 1))

        async def _run_ready_task(
            *,
            task: Task,
            member: AgentIdentity,
            task_sequence_start: int,
            on_stream_activity=None,
        ) -> dict[str, Any]:
            turn_started_at = datetime.now(timezone.utc)
            started_event = {
                "type": "subagent_started",
                "status": "running",
                "source": "shadow_clone_v2",
                "shadow_clone_mode": self.mode.value,
                "thread_run_id": thread_run_id,
                "agent_run_id": self.agent_run_id,
                "ui_phase": "subagents_running",
                "activity_owner": "shadow_clone",
                "subtask_id": task.id,
                "agent_name": member.agent_name,
                "role": task.subject or member.role,
                "task_description": task.description,
            }
            try:
                async def _execute_v2_task_with_thread_run_id(
                    task: Task,
                    agent: AgentIdentity,
                    *,
                    sequence_start: int = 0,
                    inbox_messages: list[Any] | None = None,
                    on_stream_activity: (
                        Callable[[dict[str, Any]], Awaitable[None]] | None
                    ) = None,
                ) -> Any:
                    return await self._execute_v2_task(
                        task,
                        agent,
                        sequence_start=sequence_start,
                        inbox_messages=inbox_messages,
                        thread_run_id=thread_run_id,
                        on_stream_activity=on_stream_activity,
                    )

                turn_result = await agent_loop.run_agent_turn(
                    run_id=self.agent_run_id,
                    project_id=self.project_id,
                    agent=member,
                    task=task,
                    lease_token=f"{self.agent_run_id}:{member.agent_name}:{task.id}",
                    lease_expires_at=(
                        turn_started_at + timedelta(minutes=35)
                    ).isoformat(),
                    now=turn_started_at,
                    executor=_execute_v2_task_with_thread_run_id,
                    sequence_start=task_sequence_start,
                    clock=lambda: datetime.now(timezone.utc),
                    on_stream_activity=on_stream_activity,
                )
            except Exception as exc:
                try:
                    setattr(exc, "task", task)
                    setattr(exc, "member", member)
                except Exception:
                    pass
                return {
                    "ok": False,
                    "task": task,
                    "member": member,
                    "started_event": started_event,
                    "error": exc,
                }

            returned_agent = getattr(turn_result, "agent", None)
            turn_output = str(getattr(turn_result, "output", "") or "")
            turn_finished_at = datetime.now(timezone.utc).isoformat()
            output_item = None
            activity_event = None
            if turn_output.strip():
                output_item = {
                    "agent_name": member.agent_name,
                    "task_id": task.id,
                    "task_subject": task.subject,
                    "task_description": task.description,
                    "output": turn_output,
                }
                activity_event = self._build_subagent_activity_event(
                    task=task,
                    member=member,
                    output=turn_output,
                    thread_run_id=thread_run_id,
                    sequence=max(0, task_sequence_start + 1),
                    timestamp=turn_finished_at,
                )
            completed_event = {
                "type": "subagent_completed",
                "status": "completed",
                "source": "shadow_clone_v2",
                "shadow_clone_mode": self.mode.value,
                "thread_run_id": thread_run_id,
                "agent_run_id": self.agent_run_id,
                "ui_phase": "subagents_running",
                "activity_owner": "shadow_clone",
                "subtask_id": task.id,
                "agent_name": member.agent_name,
                "role": task.subject or member.role,
                "task_description": task.description,
                "result_summary": turn_output or None,
            }
            return {
                "ok": True,
                "task": task,
                "member": member,
                "turn_result": turn_result,
                "returned_agent": returned_agent,
                "turn_output": turn_output,
                "output_item": output_item,
                "activity_event": activity_event,
                "started_event": started_event,
                "completed_event": completed_event,
                "next_sequence": getattr(turn_result, "next_sequence", None),
            }

        while pending_tasks_by_id:
            ready_tasks: list[Task] = []
            for task in tasks:
                if task.id not in pending_tasks_by_id or task.id in attempted_task_ids:
                    continue
                blockers = [str(blocker_id) for blocker_id in (task.blocked_by or [])]
                if all(blocker_id in completed_task_ids for blocker_id in blockers):
                    ready_tasks.append(task)
            if not ready_tasks:
                await event_log.append_event(
                    run_id=self.agent_run_id,
                    thread_id=self.thread_id,
                    project_id=self.project_id,
                    sequence=sequence,
                    event_type=EventType.RUN_FAILED,
                    activity_owner="facilitator",
                    payload={
                        "blocked_task_ids": sorted(pending_tasks_by_id),
                        "completed_task_count": executed_task_count,
                        "error_type": "TaskBlockedByUnresolvedDependencies",
                        "error": "Shadow Clone V2 has pending tasks with unresolved blocked_by dependencies.",
                    },
                    created_at=datetime.now(timezone.utc).isoformat(),
                )
                yield {
                    "type": "shadow_clone_v2_execution_blocked",
                    "status": "blocked",
                    "shadow_clone_mode": self.mode.value,
                    "thread_run_id": thread_run_id,
                    "agent_run_id": self.agent_run_id,
                    "ui_phase": "blocked",
                    "activity_owner": "none",
                    "phase_reason": "shadow_clone_v2_task_blocked_by_unresolved_dependencies",
                    "executed_task_count": executed_task_count,
                    "blocked_task_ids": sorted(pending_tasks_by_id),
                    "subtasks": self._build_ui_subtasks(
                        tasks,
                        team.members,
                        status_by_task_id={
                            task.id: (
                                "completed"
                                if task.id in completed_task_ids
                                else "blocked"
                            )
                            for task in tasks
                        },
                    ),
                }
                return

            batch: list[tuple[Task, AgentIdentity, int]] = []
            batched_agent_names: set[str] = set()
            for task in ready_tasks:
                if len(batch) >= parallel_limit:
                    break
                agent_name = str(task.metadata.get("agent_name") or "").strip()
                if agent_name in batched_agent_names:
                    continue
                member = member_by_name.get(agent_name)
                if member is None:
                    await event_log.append_event(
                        run_id=self.agent_run_id,
                        thread_id=self.thread_id,
                        project_id=self.project_id,
                        sequence=sequence,
                        event_type=EventType.RUN_FAILED,
                        activity_owner="facilitator",
                        payload={
                            "failed_task_id": task.id,
                            "missing_agent_name": agent_name,
                            "completed_task_count": executed_task_count,
                            "error_type": "TaskAgentMissing",
                            "error": "Task metadata references a missing Shadow Clone V2 agent.",
                        },
                        created_at=datetime.now(timezone.utc).isoformat(),
                    )
                    yield {
                        "type": "shadow_clone_v2_execution_failed",
                        "status": "failed",
                        "shadow_clone_mode": self.mode.value,
                        "thread_run_id": thread_run_id,
                        "agent_run_id": self.agent_run_id,
                        "ui_phase": "failed",
                        "activity_owner": "none",
                        "phase_reason": "shadow_clone_v2_task_agent_missing",
                        "completed_task_count": executed_task_count,
                        "failed_task_id": task.id,
                        "failed_agent_name": agent_name,
                        "subtasks": self._build_ui_subtasks(
                            tasks,
                            team.members,
                            status_by_task_id={task.id: "failed"},
                        ),
                    }
                    return
                batch.append((task, member, sequence + (len(batch) * 100)))
                batched_agent_names.add(agent_name)
                attempted_task_ids.add(task.id)

            for task, member, _task_sequence_start in batch:
                yield {
                    "type": "subagent_started",
                    "status": "running",
                    "source": "shadow_clone_v2",
                    "shadow_clone_mode": self.mode.value,
                    "thread_run_id": thread_run_id,
                    "agent_run_id": self.agent_run_id,
                    "ui_phase": "subagents_running",
                    "activity_owner": "shadow_clone",
                    "subtask_id": task.id,
                    "agent_name": member.agent_name,
                    "role": task.subject or member.role,
                    "task_description": task.description,
                }
            yield self._build_main_agent_progress_event(
                phase_key=f"waiting-batch-{executed_task_count}-{len(batch)}",
                message=(
                    f"我已启动 {len(batch)} 个团队成员执行当前批次，"
                    "正在等待他们返回结果。"
                ),
                thread_run_id=thread_run_id,
                sequence=sequence,
                timestamp=datetime.now(timezone.utc).isoformat(),
                ui_phase="subagents_running",
                phase_reason="shadow_clone_v2_waiting_for_subagents",
                extra_metadata={
                    "running_subtask_ids": [task.id for task, _member, _seq in batch],
                    "batch_size": len(batch),
                },
            )

            live_activity_queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
            stream_completed_subtask_ids: set[str] = set()

            async def _enqueue_stream_activity(activity: dict[str, Any]) -> None:
                metadata = activity.get("metadata") or {}
                if (
                    activity.get("type") == "subagent_activity"
                    and activity.get("message_type") == "assistant"
                    and str(metadata.get("stream_status") or "").strip()
                    == "complete"
                ):
                    subtask_id = str(activity.get("subtask_id") or "").strip()
                    if subtask_id:
                        stream_completed_subtask_ids.add(subtask_id)
                await live_activity_queue.put(activity)

            batch_result_tasks = [
                asyncio.create_task(
                    _run_ready_task(
                        task=task,
                        member=member,
                        task_sequence_start=task_sequence_start,
                        on_stream_activity=_enqueue_stream_activity,
                    )
                )
                for task, member, task_sequence_start in batch
            ]

            pending_result_tasks = set(batch_result_tasks)
            while pending_result_tasks:
                queue_get_task = asyncio.create_task(live_activity_queue.get())
                done_tasks, _pending_tasks = await asyncio.wait(
                    [*pending_result_tasks, queue_get_task],
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if queue_get_task in done_tasks:
                    yield queue_get_task.result()
                    continue
                queue_get_task.cancel()
                await asyncio.gather(queue_get_task, return_exceptions=True)

                completed_result_tasks = done_tasks & pending_result_tasks
                for completed_result_task in completed_result_tasks:
                    pending_result_tasks.remove(completed_result_task)
                    result = await completed_result_task
                    task = result["task"]
                    member = result["member"]
                    if not result.get("ok"):
                        for pending_result_task in pending_result_tasks:
                            if not pending_result_task.done():
                                pending_result_task.cancel()
                        await asyncio.gather(
                            *batch_result_tasks,
                            return_exceptions=True,
                        )
                        exc = result["error"]
                        yield {
                            "type": "subagent_failed",
                            "status": "failed",
                            "source": "shadow_clone_v2",
                            "shadow_clone_mode": self.mode.value,
                            "thread_run_id": thread_run_id,
                            "agent_run_id": self.agent_run_id,
                            "ui_phase": "failed",
                            "activity_owner": "shadow_clone",
                            "subtask_id": task.id,
                            "agent_name": member.agent_name,
                            "role": task.subject or member.role,
                            "task_description": task.description,
                            "error": str(exc),
                            "failure_class": type(exc).__name__,
                        }
                        failure_sequence = getattr(exc, "next_sequence", None)
                        if (
                            not isinstance(failure_sequence, int)
                            or failure_sequence <= sequence
                        ):
                            failure_sequence = sequence + (len(batch) * 100) + 3
                        await event_log.append_event(
                            run_id=self.agent_run_id,
                            thread_id=self.thread_id,
                            project_id=self.project_id,
                            sequence=failure_sequence,
                            event_type=EventType.RUN_FAILED,
                            activity_owner="facilitator",
                            payload={
                                "failed_task_id": task.id,
                                "failed_agent_name": member.agent_name,
                                "completed_task_count": executed_task_count,
                                "error_type": type(exc).__name__,
                                "error": str(exc),
                            },
                            created_at=datetime.now(timezone.utc).isoformat(),
                        )
                        yield {
                            "type": "shadow_clone_v2_execution_failed",
                            "status": "failed",
                            "shadow_clone_mode": self.mode.value,
                            "thread_run_id": thread_run_id,
                            "agent_run_id": self.agent_run_id,
                            "ui_phase": "failed",
                            "activity_owner": "none",
                            "phase_reason": "shadow_clone_v2_execution_failed",
                            "completed_task_count": executed_task_count,
                            "failed_task_id": task.id,
                            "failed_agent_name": member.agent_name,
                            "error": str(exc),
                            "subtasks": self._build_ui_subtasks(
                                tasks,
                                team.members,
                                status="pending",
                                status_by_task_id={
                                    completed_task_id: "completed"
                                    for completed_task_id in completed_task_ids
                                }
                                | {task.id: "failed"},
                            ),
                        }
                        return

                    returned_agent = result.get("returned_agent")
                    if isinstance(returned_agent, AgentIdentity):
                        team = team_store.replace_team_member(team, returned_agent)
                        member_by_name[returned_agent.agent_name] = returned_agent
                        await team_store.store_team(
                            team,
                            ttl_seconds=SHADOW_CLONE_V2_STATE_TTL_SECONDS,
                        )
                    executed_task_count += 1
                    completed_task_ids.add(task.id)
                    pending_tasks_by_id.pop(task.id, None)
                    if result.get("output_item") is not None:
                        worker_outputs.append(result["output_item"])
                    if (
                        result.get("activity_event") is not None
                        and task.id not in stream_completed_subtask_ids
                    ):
                        yield result["activity_event"]
                    yield result["completed_event"]
                    next_sequence = result.get("next_sequence")
                    if isinstance(next_sequence, int) and next_sequence > sequence:
                        sequence = max(sequence, next_sequence)

            while not live_activity_queue.empty():
                yield live_activity_queue.get_nowait()

            sequence = max(sequence, sequence + (len(batch) * 100) + 3)
            latest_tasks = await task_pool.list_tasks(run_id=self.agent_run_id)
            if latest_tasks:
                task_by_id.update({task.id: task for task in latest_tasks})
                tasks = [task_by_id.get(task.id, task) for task in tasks]
                for latest_task in latest_tasks:
                    if latest_task.id not in {task.id for task in tasks}:
                        tasks.append(latest_task)
                pending_tasks_by_id = {
                    task.id: task
                    for task in tasks
                    if str(getattr(task.status, "value", task.status))
                    in {"pending", "blocked"}
                    and task.id not in completed_task_ids
                    and task.id not in attempted_task_ids
                }
            else:
                pending_tasks_by_id = {
                    task_id: task
                    for task_id, task in pending_tasks_by_id.items()
                    if task_id not in completed_task_ids
                }

        try:
            latest_tasks = await task_pool.list_tasks(run_id=self.agent_run_id)
            if latest_tasks:
                tasks = list(latest_tasks)
            team, sequence = await self._mark_completed_task_owners_idle(
                team=team,
                tasks=tasks,
                sequence_start=sequence,
            )
            yield self._build_main_agent_progress_event(
                phase_key="aggregating",
                message="我已收到团队成员的结果，正在整合最终答复。",
                thread_run_id=thread_run_id,
                sequence=sequence,
                timestamp=datetime.now(timezone.utc).isoformat(),
                ui_phase="aggregating",
                phase_reason="shadow_clone_v2_aggregation_progress",
                activity_owner="main_agent",
                extra_metadata={
                    "completed_task_count": executed_task_count,
                    "worker_output_count": len(worker_outputs),
                },
            )
            synthesis_result = await self.main_agent_synthesizer.synthesize(
                user_message=user_message,
                worker_outputs=worker_outputs,
                tasks=tasks,
                team=team,
                messages=await self._load_event_log_message_context(),
                model_key=self.model_key,
                thread_run_id=thread_run_id,
                agent_run_id=self.agent_run_id,
                db_client=self.db_client,
                trace=self.trace,
                sequence_start=sequence,
                created_at=datetime.now(timezone.utc).isoformat(),
            )
            if isinstance(synthesis_result, MainAgentSynthesisResult):
                final_output = str(synthesis_result.output or "").strip()
                synthesis_next_sequence = synthesis_result.next_sequence
            else:
                final_output = str(synthesis_result or "").strip()
                synthesis_next_sequence = None
            if not final_output:
                raise EmptyFinalSynthesisOutput(
                    "final synthesis produced no user-facing output"
                )
            if (
                isinstance(synthesis_next_sequence, int)
                and synthesis_next_sequence > sequence
            ):
                sequence = synthesis_next_sequence
        except Exception as exc:
            await self._append_synthesis_failed_event(
                thread_run_id=thread_run_id,
                error=exc,
                sequence=sequence,
                completed_task_count=executed_task_count,
            )
            yield self._build_synthesis_failed_status(
                thread_run_id=thread_run_id,
                error=exc,
                completed_task_count=executed_task_count,
                tasks=tasks,
                team=team,
            )
            return
        if final_output:
            yield self._build_main_agent_final_assistant_event(
                output=final_output,
                thread_run_id=thread_run_id,
                sequence=sequence,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )

        await event_log.append_event(
            run_id=self.agent_run_id,
            thread_id=self.thread_id,
            project_id=self.project_id,
            sequence=sequence,
            event_type=EventType.RUN_COMPLETED,
            activity_owner="facilitator",
            payload={
                "executed_task_count": executed_task_count,
                "subagent_count": len(team.members),
                "task_count": len(tasks),
                "final_output": final_output or None,
            },
            created_at=datetime.now(timezone.utc).isoformat(),
        )

        yield {
            "type": "shadow_clone_v2_execution_completed",
            "status": "completed",
            "shadow_clone_mode": self.mode.value,
            "thread_run_id": thread_run_id,
            "agent_run_id": self.agent_run_id,
            "ui_phase": "completed",
            "activity_owner": "none",
            "phase_reason": "shadow_clone_v2_execution_completed",
            "executed_task_count": executed_task_count,
            "subtasks": self._build_ui_subtasks(
                tasks, team.members, status="completed"
            ),
        }

    async def _execute_v2_task(
        self,
        task: Task,
        agent: AgentIdentity,
        *,
        sequence_start: int = 0,
        inbox_messages: list[Any] | None = None,
        thread_run_id: str | None = None,
        on_stream_activity: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> Any:
        """Execute one V2 task via the configured executor seam."""
        execute = self.task_executor.execute
        run_context = ShadowCloneV2RunContext(
            thread_id=self.thread_id,
            project_id=self.project_id,
            account_id=self.account_id,
            agent_run_id=self.agent_run_id,
            model_key=self.model_key,
            thread_run_id=thread_run_id,
            subagent_model=self.subagent_model,
            db_client=self.db_client,
            trace=self.trace,
            tool_sequence_start=sequence_start,
        )
        kwargs: dict[str, Any] = {
            "task": task,
            "agent": agent,
            "run_context": run_context,
            "inbox_messages": inbox_messages,
        }
        try:
            signature = inspect.signature(execute)
        except (TypeError, ValueError):
            signature = None
        if signature is not None:
            supports_var_kwargs = any(
                parameter.kind == inspect.Parameter.VAR_KEYWORD
                for parameter in signature.parameters.values()
            )
            if (
                "on_stream_activity" in signature.parameters
                or supports_var_kwargs
            ):
                kwargs["on_stream_activity"] = on_stream_activity
        elif on_stream_activity is not None:
            kwargs["on_stream_activity"] = on_stream_activity
        return await execute(**kwargs)

    async def close(self) -> None:
        """Close V2 resources."""
        return None


def _validate_tool_plan_consistency(
    *,
    returned_team: TeamConfig,
    returned_tasks: list[Task],
    durable_team: TeamConfig,
    durable_tasks: list[Task],
) -> None:
    """Ensure the planner's in-memory result matches durable tool effects."""
    if returned_team.team_id != durable_team.team_id:
        raise RuntimeError("main-agent planning durable team_id mismatch")
    if returned_team.facilitator_id != durable_team.facilitator_id:
        raise RuntimeError("main-agent planning durable facilitator mismatch")
    returned_members = {member.agent_name for member in returned_team.members}
    durable_members = {member.agent_name for member in durable_team.members}
    if returned_members != durable_members:
        raise RuntimeError("main-agent planning durable member set mismatch")

    returned_task_ids = {task.id for task in returned_tasks}
    durable_task_ids = {task.id for task in durable_tasks}
    if returned_task_ids != durable_task_ids:
        raise RuntimeError("main-agent planning durable task set mismatch")

    for task in durable_tasks:
        metadata = task.metadata if isinstance(task.metadata, dict) else {}
        agent_name = str(metadata.get("agent_name") or "").strip()
        if not agent_name:
            raise RuntimeError(
                f"main-agent planning durable task {task.id} is missing "
                "metadata.agent_name"
            )
        if agent_name not in durable_members:
            raise RuntimeError(
                f"main-agent planning durable task {task.id} targets unknown "
                f"agent {agent_name}"
            )


def _event_sequence(event: Any) -> int:
    """Return an event sequence number suitable for lifecycle ordering."""
    value = getattr(event, "sequence", None)
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0
