"""Agent-facing Team/Task/Message tools for Shadow Clone V2.

These tools are intentionally thin wrappers over the V2 persistence primitives.
The runner may supervise and enforce limits, but main/subagents need this module
as the operation surface for creating teams, creating/listing tasks, and sending
peer messages during their own turns.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import hashlib
import re
from datetime import datetime, timezone
from typing import Any, Callable

from services import redis as redis_service

from . import event_log, mailbox, task_pool, team_store
from .constants import (
    SHADOW_CLONE_V2_IDLE_TTL_SECONDS,
    SHADOW_CLONE_V2_MAX_SUBAGENTS_PER_RUN,
    SHADOW_CLONE_V2_STATE_TTL_SECONDS,
)
from .models import (
    AgentIdentity,
    AgentLifecycleStatus,
    EventType,
    MailboxMessage,
    MessageKind,
    MessageType,
    Task,
    TaskStatus,
    TeamConfig,
)
from .validation import (
    enforce_max_text,
    require_key_part,
    require_non_empty_text,
    validate_json_payload,
)

Clock = Callable[[], str]

MAX_MESSAGE_SUMMARY_CHARS = 160
MAX_MESSAGE_TEXT_CHARS = 20_000
MAX_MESSAGE_PAYLOAD_BYTES = 16_384
MAX_TASK_METADATA_BYTES = 16_384
MAX_MESSAGES_PER_TOOL_TURN = 20


def _task_event_snapshot(task: Task) -> dict[str, Any]:
    """Return a replay-safe task snapshot for event-log projection."""
    return {
        "id": task.id,
        "task_id": task.id,
        "run_id": task.run_id,
        "thread_id": task.thread_id,
        "subject": task.subject,
        "description": task.description,
        "status": task.status.value,
        "priority": task.priority,
        "blocked_by": list(task.blocked_by),
        "blocks": list(task.blocks),
        "owner_agent": task.owner_agent,
        "lease_expires_at": task.lease_expires_at,
        "attempt": task.attempt,
        "max_attempts": task.max_attempts,
        "created_by": task.created_by,
        "plan_revision": task.plan_revision,
        "version": task.plan_revision,
        "result_ref": task.result_ref,
        "error": dict(task.error or {}),
        "metadata": dict(task.metadata or {}),
        "created_at": task.created_at,
        "updated_at": task.updated_at,
    }


_UPPERCASE_MARKER_RE = re.compile(r"\b[A-Z][A-Z0-9]*(?:_[A-Z0-9]+){2,}\b")
_P2P_CONSUMED_MARKER_RE = re.compile(
    r"\bP2P_B_CONSUMED_A_MESSAGE_[A-Za-z0-9_-]+\b"
)
_FOLLOW_UP_REQUEST_RE = re.compile(
    r"\b(follow[- ]?up|wake|reuse|idle|feedback|previous|prior)\b|上一轮|复用|唤醒|反馈",
    re.IGNORECASE,
)
_PRIOR_DELIVERY_MARKER_PREFIXES = (
    "IDLE_DRAFT_",
    "LIVE_CLICK_SUBAGENT_OK",
    "MAIN_LIVE_CLICK_OK",
)
_EXPLICIT_OUTPUT_MARKER_REQUEST_RE = re.compile(
    r"must\s+(?:explicitly\s+)?(?:contain|include)|"
    r"new\s+(?:result|task\s+result)|"
    r"required\s+output\s+markers?|"
    r"\brequire\b|"
    r"\breturn\b|"
    r"必须包含|需要包含|子任务结果|最终回答",
    re.IGNORECASE,
)


def _extract_explicit_output_marker_literals(message: str) -> set[str]:
    """Return markers from clauses that explicitly define current output."""
    requested: set[str] = set()
    for clause in re.split(r"(?<=[。.!?])\s*|\n+", message):
        if not _EXPLICIT_OUTPUT_MARKER_REQUEST_RE.search(clause):
            continue
        requested.update(_UPPERCASE_MARKER_RE.findall(clause))
    return requested


def _strip_non_requested_marker_literals(
    text: str,
    *,
    current_user_message: str | None,
) -> str:
    """Remove prior-output marker literals that are not present in this request."""
    text = _P2P_CONSUMED_MARKER_RE.sub(
        "P2P_B_CONSUMED_A_MESSAGE_<unique>",
        text,
    )
    if not current_user_message:
        return text
    allowed_markers = set(_UPPERCASE_MARKER_RE.findall(current_user_message))
    if not allowed_markers:
        return text
    if _FOLLOW_UP_REQUEST_RE.search(current_user_message):
        explicit_output_markers = _extract_explicit_output_marker_literals(
            current_user_message
        )
        allowed_markers = {
            marker
            for marker in allowed_markers
            if not marker.startswith(_PRIOR_DELIVERY_MARKER_PREFIXES)
        } | explicit_output_markers

    def _replace(match: re.Match[str]) -> str:
        marker = match.group(0)
        return marker if marker in allowed_markers else ""

    cleaned = _UPPERCASE_MARKER_RE.sub(_replace, text)
    cleaned = re.sub(r"[ \t]{2,}", " ", cleaned)
    cleaned = re.sub(r"\s+([,.;:])", r"\1", cleaned)
    return cleaned.strip()


@dataclass(frozen=True)
class ShadowCloneV2ToolContext:
    """Stable run identity supplied to agent-facing V2 tools."""

    run_id: str
    thread_id: str
    project_id: str
    actor_name: str
    account_id: str | None = None
    sequence_start: int = 0
    state_ttl_seconds: int = SHADOW_CLONE_V2_STATE_TTL_SECONDS
    current_user_message: str | None = None


@dataclass(frozen=True)
class TeamToolResult:
    team: TeamConfig
    next_sequence: int


@dataclass(frozen=True)
class TeamShutdownToolResult:
    team: TeamConfig
    next_sequence: int


@dataclass(frozen=True)
class TaskToolResult:
    task: Task
    next_sequence: int


@dataclass(frozen=True)
class TaskGetToolResult:
    task: Task
    next_sequence: int


@dataclass(frozen=True)
class TaskListToolResult:
    tasks: list[Task]
    next_sequence: int


@dataclass(frozen=True)
class MessageToolResult:
    message: MailboxMessage
    next_sequence: int


@dataclass(frozen=True)
class MessageAckToolResult:
    ack: mailbox.MailboxAckResult
    next_sequence: int


@dataclass(frozen=True)
class MessageReadToolResult:
    messages: list[MailboxMessage]
    next_sequence: int


@dataclass(frozen=True)
class MessageDeadLetterToolResult:
    message: MailboxMessage
    next_sequence: int


class ShadowCloneV2AgentTools:
    """Tool surface used by main and subagents to operate V2 state."""

    def __init__(
        self,
        *,
        context: ShadowCloneV2ToolContext,
        clock: Clock | None = None,
    ) -> None:
        self.context = context
        self._clock = clock or _now_iso
        self._messages_sent = 0
        self._next_sequence = context.sequence_start

    @property
    def next_sequence(self) -> int:
        """Next event sequence after tool calls made by this tool surface."""
        return self._next_sequence

    def _reserve_sequences(self, count: int) -> int:
        """Reserve a contiguous event sequence range before awaited I/O."""
        if count <= 0:
            raise ValueError("sequence reservation count must be positive")
        start = self._next_sequence
        self._next_sequence += count
        return start

    async def team_create(self, *, members: list[dict[str, str]]) -> TeamToolResult:
        """Create or replace the thread-scoped team from an agent tool call."""
        created_at = self._clock()
        normalized_members = _build_members(
            members=members,
            thread_id=self.context.thread_id,
            project_id=self.context.project_id,
            account_id=self.context.account_id,
        )
        existing_team = await team_store.read_team(
            project_id=self.context.project_id,
            thread_id=self.context.thread_id,
            account_id=self.context.account_id,
        )
        if (
            existing_team is not None
            and self.context.account_id
            and existing_team.account_id
            and existing_team.account_id != self.context.account_id
        ):
            raise ValueError("existing team belongs to a different account")
        if (
            existing_team is not None
            and self.context.actor_name != existing_team.facilitator_id
        ):
            raise ValueError("only the team facilitator can replace an existing team")

        preserved_metadata = (
            dict(existing_team.metadata) if existing_team is not None else {}
        )
        existing_by_name: dict[str, AgentIdentity] = {}
        reusable_idle_by_name: dict[str, AgentIdentity] = {}
        expired_idle_by_name: dict[str, AgentIdentity] = {}
        omitted_idle_members: list[AgentIdentity] = []
        requested_member_names = {member.agent_name for member in normalized_members}
        if existing_team is not None:
            existing_by_name = {
                member.agent_name: member for member in existing_team.members
            }
            omitted_active_members = [
                member.agent_name
                for member in existing_team.members
                if member.agent_name not in requested_member_names
                and member.status
                not in {AgentLifecycleStatus.IDLE, AgentLifecycleStatus.CLOSED}
            ]
            if omitted_active_members:
                raise ValueError(
                    "active existing teammate must be shut down or idled "
                    "before replacement: " + ", ".join(sorted(omitted_active_members))
                )
            omitted_idle_members = [
                member
                for member in existing_team.members
                if member.agent_name not in requested_member_names
                and member.status == AgentLifecycleStatus.IDLE
            ]
            reusable_idle_by_name = {
                member.agent_name: member
                for member in existing_team.members
                if _should_wake_idle_agent(member, now_text=created_at)
            }
            expired_idle_by_name = {
                member.agent_name: member
                for member in existing_team.members
                if member.status == AgentLifecycleStatus.IDLE
                and not _should_wake_idle_agent(member, now_text=created_at)
            }
        resolved_members: list[AgentIdentity] = []
        woke_agent_names: set[str] = set()
        spawned_agent_names: set[str] = set()
        expired_shutdown_members: list[AgentIdentity] = []
        previous_member_by_name: dict[str, AgentIdentity] = {}
        for omitted_member in omitted_idle_members:
            if not _should_wake_idle_agent(omitted_member, now_text=created_at):
                expired_shutdown_members.append(omitted_member)
        for member in normalized_members:
            existing_member = existing_by_name.get(member.agent_name)
            if existing_member is not None:
                previous_member_by_name[member.agent_name] = existing_member
                if existing_member.status not in {
                    AgentLifecycleStatus.IDLE,
                    AgentLifecycleStatus.CLOSED,
                }:
                    raise ValueError(
                        "active existing teammate must be shut down or idled "
                        f"before replacement: {member.agent_name}"
                    )
            reusable_member = reusable_idle_by_name.get(member.agent_name)
            if reusable_member is not None:
                woke_agent_names.add(member.agent_name)
                resolved_members.append(
                    reusable_member.model_copy(
                        update={
                            "role": member.role,
                            "status": AgentLifecycleStatus.WORKING,
                            "current_run_id": self.context.run_id,
                            "idle_since": None,
                            "idle_expires_at": None,
                        }
                    )
                )
                continue
            expired_member = expired_idle_by_name.get(member.agent_name)
            if expired_member is not None:
                expired_shutdown_members.append(expired_member)
                spawned_agent_names.add(member.agent_name)
                resolved_members.append(member)
                continue
            spawned_agent_names.add(member.agent_name)
            resolved_members.append(member)
        for omitted_member in omitted_idle_members:
            if _should_wake_idle_agent(omitted_member, now_text=created_at):
                resolved_members.append(omitted_member)
        team = TeamConfig(
            team_id=_team_id(self.context.project_id, self.context.thread_id),
            thread_id=self.context.thread_id,
            project_id=self.context.project_id,
            account_id=self.context.account_id
            or (existing_team.account_id if existing_team is not None else None),
            facilitator_id=(
                existing_team.facilitator_id
                if existing_team is not None
                else require_key_part(self.context.actor_name, "actor_name")
            ),
            idle_ttl_seconds=(
                existing_team.idle_ttl_seconds
                if existing_team is not None
                else SHADOW_CLONE_V2_IDLE_TTL_SECONDS
            ),
            max_subagents_per_run=(
                existing_team.max_subagents_per_run
                if existing_team is not None
                else SHADOW_CLONE_V2_MAX_SUBAGENTS_PER_RUN
            ),
            members=resolved_members,
            budget=dict(existing_team.budget) if existing_team is not None else {},
            sandbox_policy=(
                dict(existing_team.sandbox_policy) if existing_team is not None else {}
            ),
            metadata={**preserved_metadata, "updated_by_tool": "TeamCreate"},
        )
        request_digest = _team_create_request_digest(
            context=self.context,
            requested_members=normalized_members,
        )
        sequence = self.next_sequence
        event_specs: list[dict[str, Any]] = [
            {
                "phase": "team_created",
                "sequence": sequence,
                "event_type": EventType.TEAM_CREATED,
                "payload": {
                    "team_id": team.team_id,
                    "account_id": team.account_id,
                    "facilitator_id": team.facilitator_id,
                    "member_count": len(team.members),
                    "members": [
                        _agent_identity_event_snapshot(member)
                        for member in team.members
                    ],
                    "tool": "TeamCreate",
                },
            }
        ]
        sequence += 1
        for expired_member in expired_shutdown_members:
            event_specs.append(
                {
                    "phase": f"expired-shutdown-{expired_member.agent_name}",
                    "sequence": sequence,
                    "event_type": EventType.AGENT_SHUTDOWN,
                    "payload": {
                        "team_id": team.team_id,
                        "account_id": expired_member.account_id,
                        "agent_id": expired_member.agent_id,
                        "agent_name": expired_member.agent_name,
                        "role": expired_member.role,
                        "status": AgentLifecycleStatus.CLOSED.value,
                        "shutdown_reason": "idle_ttl_expired",
                        "tool": "TeamCreate",
                    },
                },
            )
            sequence += 1
        for member in team.members:
            event_type = (
                EventType.AGENT_WAKE
                if member.agent_name in woke_agent_names
                else EventType.AGENT_SPAWNED
            )
            if (
                member.agent_name not in woke_agent_names
                and member.agent_name not in spawned_agent_names
            ):
                continue
            payload = {
                "team_id": team.team_id,
                "account_id": member.account_id,
                "agent_id": member.agent_id,
                "agent_name": member.agent_name,
                "role": member.role,
                "status": member.status.value,
                "tool": "TeamCreate",
            }
            if event_type == EventType.AGENT_WAKE:
                payload["wake_reason"] = "team_create_reuse"
                previous_member = previous_member_by_name.get(member.agent_name)
                if previous_member is not None:
                    payload["previous_status"] = previous_member.status.value
                    payload["previous_run_id"] = previous_member.current_run_id
                    payload["idle_since"] = previous_member.idle_since
                    payload["idle_expires_at"] = previous_member.idle_expires_at
            event_specs.append(
                {
                    "phase": f"{event_type.value}-{member.agent_name}",
                    "sequence": sequence,
                    "event_type": event_type,
                    "payload": payload,
                }
            )
            sequence += 1
        session_payload = await _claim_team_create_session(
            context=self.context,
            request_digest=request_digest,
            session_payload=_team_create_session_payload(
                request_digest=request_digest,
                team=team,
                event_specs=event_specs,
                next_sequence=sequence,
            ),
            ttl_seconds=self.context.state_ttl_seconds,
        )
        team = _team_from_session_payload(session_payload)
        event_specs = _event_specs_from_session_payload(session_payload)
        self._next_sequence = max(
            self._next_sequence,
            int(session_payload.get("next_sequence") or self._next_sequence),
        )
        for spec in event_specs:
            phase_key = _team_create_phase_key(
                context=self.context,
                phase=str(spec["phase"]),
            )
            claimed = await _claim_team_create_phase(
                phase_key=phase_key,
                ttl_seconds=self.context.state_ttl_seconds,
            )
            if claimed == "in_progress":
                raise RuntimeError(
                    f"team create phase is already in progress: {spec['phase']}"
                )
            if claimed is True or claimed == "claimed":
                try:
                    await event_log.append_event(
                        run_id=self.context.run_id,
                        thread_id=self.context.thread_id,
                        project_id=self.context.project_id,
                        sequence=int(spec["sequence"]),
                        event_type=spec["event_type"],
                        activity_owner=_activity_owner(self.context.actor_name),
                        payload=spec["payload"],
                        created_at=created_at,
                    )
                    await _mark_team_create_phase_done(
                        phase_key=phase_key,
                        ttl_seconds=self.context.state_ttl_seconds,
                    )
                except Exception:
                    await _release_team_create_phase(phase_key=phase_key)
                    raise
        await team_store.store_team(
            team,
            ttl_seconds=self.context.state_ttl_seconds,
        )
        return TeamToolResult(team=team, next_sequence=self._next_sequence)

    async def team_shutdown(self, *, reason: str) -> TeamShutdownToolResult:
        """Close an idle team after the facilitator has completed work."""
        shutdown_reason = require_non_empty_text(reason, "reason")
        team = await team_store.read_team(
            project_id=self.context.project_id,
            thread_id=self.context.thread_id,
            account_id=self.context.account_id,
        )
        if team is None:
            raise ValueError("team does not exist for this thread")
        if self.context.actor_name != team.facilitator_id:
            raise ValueError("only the team facilitator can shutdown the team")
        existing_shutdown_reason = str(
            (team.metadata or {}).get("shutdown_reason") or ""
        ).strip()
        if existing_shutdown_reason and existing_shutdown_reason != shutdown_reason:
            raise ValueError("shutdown already completed with a different reason")
        shutdown_reason = await _claim_shutdown_session(
            context=self.context,
            reason=shutdown_reason,
            ttl_seconds=self.context.state_ttl_seconds,
        )
        active_members = [
            member.agent_name
            for member in team.members
            if member.status
            not in {AgentLifecycleStatus.IDLE, AgentLifecycleStatus.CLOSED}
        ]
        if active_members:
            raise ValueError(
                "active teammates must be cancelled or idled before shutdown: "
                + ", ".join(sorted(active_members))
            )
        tasks = list(await task_pool.list_tasks(run_id=self.context.run_id))
        open_tasks = [
            task.id for task in tasks if task.status not in _terminal_task_statuses()
        ]
        if open_tasks:
            raise ValueError(
                "open tasks must be completed, failed, cancelled, or deprecated "
                "before shutdown: " + ", ".join(sorted(open_tasks))
            )

        shutdown_at = self._clock()
        closed_members = [
            member.model_copy(
                update={
                    "status": AgentLifecycleStatus.CLOSED,
                    "current_run_id": self.context.run_id,
                    "idle_since": None,
                    "idle_expires_at": None,
                    "metadata": {
                        **dict(member.metadata or {}),
                        "shutdown_reason": shutdown_reason,
                        "shutdown_by": self.context.actor_name,
                        "shutdown_at": shutdown_at,
                    },
                }
            )
            for member in team.members
        ]
        closed_team = team.model_copy(
            update={
                "members": closed_members,
                "metadata": {
                    **dict(team.metadata or {}),
                    "shutdown_reason": shutdown_reason,
                    "shutdown_by": self.context.actor_name,
                    "shutdown_at": shutdown_at,
                },
            }
        )
        sequence = self._reserve_sequences(len(closed_members) + 1)
        event_sequence = sequence
        for member in closed_members:
            phase_key = _shutdown_phase_key(
                context=self.context,
                phase=f"agent-{member.agent_name}",
            )
            claimed = await _claim_shutdown_phase(
                phase_key=phase_key,
                ttl_seconds=self.context.state_ttl_seconds,
            )
            if claimed == "in_progress":
                raise RuntimeError(
                    f"shutdown phase is already in progress: {member.agent_name}"
                )
            if claimed is True or claimed == "claimed":
                try:
                    await event_log.append_event(
                        run_id=self.context.run_id,
                        thread_id=self.context.thread_id,
                        project_id=self.context.project_id,
                        sequence=event_sequence,
                        event_type=EventType.AGENT_SHUTDOWN,
                        activity_owner=_activity_owner(self.context.actor_name),
                        payload={
                            "team_id": closed_team.team_id,
                            "agent_id": member.agent_id,
                            "agent_name": member.agent_name,
                            "status": member.status.value,
                            "shutdown_reason": shutdown_reason,
                            "shutdown_by": self.context.actor_name,
                        },
                        created_at=shutdown_at,
                    )
                    await _mark_shutdown_phase_done(
                        phase_key=phase_key,
                        ttl_seconds=self.context.state_ttl_seconds,
                    )
                except Exception:
                    await _release_shutdown_phase(phase_key=phase_key)
                    raise
            event_sequence += 1
        team_deleted_phase_key = _shutdown_phase_key(
            context=self.context,
            phase="team_deleted",
        )
        claimed_team_deleted = await _claim_shutdown_phase(
            phase_key=team_deleted_phase_key,
            ttl_seconds=self.context.state_ttl_seconds,
        )
        if claimed_team_deleted == "in_progress":
            raise RuntimeError("shutdown phase is already in progress: team_deleted")
        if claimed_team_deleted is True or claimed_team_deleted == "claimed":
            try:
                await event_log.append_event(
                    run_id=self.context.run_id,
                    thread_id=self.context.thread_id,
                    project_id=self.context.project_id,
                    sequence=event_sequence,
                    event_type=EventType.TEAM_DELETED,
                    activity_owner=_activity_owner(self.context.actor_name),
                    payload={
                        "team_id": closed_team.team_id,
                        "actor": self.context.actor_name,
                        "shutdown_reason": shutdown_reason,
                        "closed_agent_names": [
                            member.agent_name for member in closed_members
                        ],
                    },
                    created_at=shutdown_at,
                )
                await _mark_shutdown_phase_done(
                    phase_key=team_deleted_phase_key,
                    ttl_seconds=self.context.state_ttl_seconds,
                )
            except Exception:
                await _release_shutdown_phase(phase_key=team_deleted_phase_key)
                raise
        await team_store.store_team(
            closed_team,
            ttl_seconds=self.context.state_ttl_seconds,
        )
        return TeamShutdownToolResult(
            team=closed_team,
            next_sequence=self._next_sequence,
        )

    async def task_create(
        self,
        *,
        task_id: str,
        subject: str,
        description: str,
        blocked_by: list[str] | None = None,
        blocks: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> TaskToolResult:
        """Create a living task document from an agent tool call."""
        if blocked_by or blocks:
            raise ValueError(
                "TaskCreate dependencies must be linked through TaskUpdate so "
                "the dependency graph stays bidirectional and cycle-safe"
            )
        normalized_blocked_by = _normalize_id_list(blocked_by or [], "blocked_by")
        normalized_blocks = _normalize_id_list(blocks or [], "blocks")
        safe_metadata = validate_json_payload(
            metadata or {},
            max_bytes=MAX_TASK_METADATA_BYTES,
        )
        created_at = self._clock()
        safe_description = _strip_non_requested_marker_literals(
            require_non_empty_text(description, "description"),
            current_user_message=self.context.current_user_message,
        )
        task = Task(
            id=require_key_part(task_id, "task_id"),
            run_id=self.context.run_id,
            thread_id=self.context.thread_id,
            subject=require_non_empty_text(subject, "subject"),
            description=safe_description,
            status=(
                TaskStatus.BLOCKED if normalized_blocked_by else TaskStatus.PENDING
            ),
            blocked_by=normalized_blocked_by,
            blocks=normalized_blocks,
            created_by=self.context.actor_name,
            metadata=safe_metadata,
            created_at=created_at,
            updated_at=created_at,
        )
        await self._validate_task_create_assignment(task)
        sequence = self._reserve_sequences(1)
        created = await task_pool.create_task(
            task,
            ttl_seconds=self.context.state_ttl_seconds,
        )
        if not created:
            raise ValueError(f"task already exists: {task.id}")
        await event_log.append_event(
            run_id=self.context.run_id,
            thread_id=self.context.thread_id,
            project_id=self.context.project_id,
            sequence=sequence,
            event_type=EventType.TASK_CREATED,
            activity_owner=_activity_owner(self.context.actor_name),
            payload={
                "task_id": task.id,
                "task": _task_event_snapshot(task),
                "subject": task.subject,
                "description": task.description,
                "created_by": task.created_by,
                "status": task.status.value,
                "blocked_by": list(task.blocked_by),
                "blocks": list(task.blocks),
                "metadata": dict(task.metadata or {}),
                "owner_agent": task.owner_agent,
                "plan_revision": task.plan_revision,
                "attempt": task.attempt,
                "max_attempts": task.max_attempts,
            },
            created_at=created_at,
        )
        return TaskToolResult(task=task, next_sequence=self._next_sequence)

    async def _validate_task_create_assignment(self, task: Task) -> None:
        """Validate TaskCreate assignment metadata before durable creation."""
        metadata = task.metadata if isinstance(task.metadata, dict) else {}
        assigned_agent = str(metadata.get("agent_name") or "").strip()
        if not assigned_agent:
            return
        team = await team_store.read_team(
            project_id=self.context.project_id,
            thread_id=self.context.thread_id,
            account_id=self.context.account_id,
        )
        member_names = set()
        if team is not None:
            member_names = {member.agent_name for member in team.members}
        _validate_assignment_targets(
            team=team,
            actor_name=self.context.actor_name,
            current=task,
            next_owner_agent=task.owner_agent,
            next_metadata=metadata,
            member_names=member_names,
        )

    async def task_list(self) -> TaskListToolResult:
        """List current run tasks without runner-owned plan state."""
        tasks = await task_pool.list_tasks(run_id=self.context.run_id)
        return TaskListToolResult(
            tasks=list(tasks),
            next_sequence=self._next_sequence,
        )

    async def task_get(self, *, task_id: str) -> TaskGetToolResult:
        """Read one full living task document from durable task state."""
        task = await task_pool.read_task(
            run_id=self.context.run_id,
            task_id=require_key_part(task_id, "task_id"),
        )
        if task is None:
            raise ValueError(f"task not found: {task_id}")
        return TaskGetToolResult(task=task, next_sequence=self._next_sequence)

    async def task_update(
        self,
        *,
        task_id: str,
        expected_version: int | None = None,
        subject: str | None = None,
        description: str | None = None,
        status: TaskStatus | str | None = None,
        owner_agent: str | None = None,
        metadata: dict[str, Any] | None = None,
        add_blocked_by: list[str] | None = None,
        add_blocks: list[str] | None = None,
        result_ref: str | None = None,
        error: dict[str, Any] | None = None,
    ) -> TaskToolResult:
        """Patch a living task document with optimistic version protection."""
        if expected_version is None:
            raise ValueError("expected_version is required")
        if int(expected_version) <= 0:
            raise ValueError("expected_version must be positive")

        safe_task_id = require_key_part(task_id, "task_id")
        current = await task_pool.read_task(
            run_id=self.context.run_id,
            task_id=safe_task_id,
        )
        if current is None:
            raise ValueError(f"task not found: {safe_task_id}")
        if current.plan_revision != int(expected_version):
            raise ValueError(
                f"stale task version: expected {expected_version}, "
                f"current {current.plan_revision}"
            )
        _reject_terminal_task_mutation(current)
        team = await team_store.read_team(
            project_id=self.context.project_id,
            thread_id=self.context.thread_id,
            account_id=self.context.account_id,
        )
        _authorize_task_update_actor(
            team=team,
            actor_name=self.context.actor_name,
            task=current,
        )
        member_names = set()
        if team is not None:
            member_names = {member.agent_name for member in team.members}

        changed_fields: list[str] = []
        update_values: dict[str, Any] = {}
        if subject is not None:
            update_values["subject"] = require_non_empty_text(subject, "subject")
            changed_fields.append("subject")
        if description is not None:
            update_values["description"] = require_non_empty_text(
                description, "description"
            )
            changed_fields.append("description")
        normalized_status = _normalize_task_status(status)
        if normalized_status is not None:
            _validate_status_transition(current.status, normalized_status)
            update_values["status"] = normalized_status
            changed_fields.append("status")
        if owner_agent is not None:
            update_values["owner_agent"] = require_key_part(owner_agent, "owner_agent")
            changed_fields.append("owner_agent")
        if metadata is not None:
            update_values["metadata"] = {
                **dict(current.metadata or {}),
                **validate_json_payload(
                    metadata,
                    max_bytes=MAX_TASK_METADATA_BYTES,
                ),
            }
            changed_fields.append("metadata")
        _validate_assignment_targets(
            team=team,
            actor_name=self.context.actor_name,
            current=current,
            next_owner_agent=update_values.get("owner_agent", current.owner_agent),
            next_metadata=update_values.get("metadata", current.metadata),
            member_names=member_names,
        )
        if result_ref is not None:
            update_values["result_ref"] = require_non_empty_text(
                result_ref, "result_ref"
            )
            changed_fields.append("result_ref")
        if error is not None:
            update_values["error"] = validate_json_payload(
                error,
                max_bytes=MAX_TASK_METADATA_BYTES,
            )
            changed_fields.append("error")

        all_tasks = await task_pool.list_tasks(run_id=self.context.run_id)
        task_by_id = {task.id: task for task in all_tasks}
        task_by_id[current.id] = current

        blocked_by = _merged_dependency_ids(
            current.blocked_by,
            add_blocked_by or [],
            field_name="blocked_by",
            current_task_id=current.id,
            task_by_id=task_by_id,
        )
        blocks = _merged_dependency_ids(
            current.blocks,
            add_blocks or [],
            field_name="blocks",
            current_task_id=current.id,
            task_by_id=task_by_id,
        )
        if blocked_by != current.blocked_by:
            update_values["blocked_by"] = blocked_by
            changed_fields.append("blocked_by")
        if blocks != current.blocks:
            update_values["blocks"] = blocks
            changed_fields.append("blocks")
        _reject_dependency_cycle(
            task_id=current.id,
            task_by_id=task_by_id,
            blocked_by=blocked_by,
            blocks=blocks,
        )
        effective_status = normalized_status or current.status
        if (
            normalized_status is None
            and current.status == TaskStatus.PENDING
            and _has_unresolved_blockers(blocked_by=blocked_by, task_by_id=task_by_id)
        ):
            effective_status = TaskStatus.BLOCKED
            update_values["status"] = TaskStatus.BLOCKED
            changed_fields.append("status")
        _reject_unresolved_blockers_for_active_status(
            status=effective_status,
            blocked_by=blocked_by,
            task_by_id=task_by_id,
        )

        updated_at = self._clock()
        updated_task = current.model_copy(
            update={
                **update_values,
                "plan_revision": current.plan_revision + 1,
                "updated_at": updated_at,
            }
        )
        reverse_tasks = _build_reverse_dependency_updates(
            current=current,
            updated=updated_task,
            task_by_id=task_by_id,
            updated_at=updated_at,
        )
        affected_task_ids = sorted(task.id for task in reverse_tasks)
        if reverse_tasks:
            updated = await task_pool.update_tasks(
                tasks=[updated_task, *reverse_tasks],
                expected_plan_revisions={
                    updated_task.id: int(expected_version),
                    **{task.id: task.plan_revision - 1 for task in reverse_tasks},
                },
                ttl_seconds=self.context.state_ttl_seconds,
            )
        else:
            updated = await task_pool.update_task(
                updated_task,
                expected_plan_revision=int(expected_version),
                ttl_seconds=self.context.state_ttl_seconds,
            )
        if not updated:
            raise ValueError(f"stale task version: expected {expected_version}")

        sequence = self._reserve_sequences(1)
        await event_log.append_event(
            run_id=self.context.run_id,
            thread_id=self.context.thread_id,
            project_id=self.context.project_id,
            sequence=sequence,
            event_type=EventType.TASK_UPDATED,
            activity_owner=_activity_owner(self.context.actor_name),
            payload={
                "task_id": updated_task.id,
                "actor": self.context.actor_name,
                "task": _task_event_snapshot(updated_task),
                "previous_version": current.plan_revision,
                "new_version": updated_task.plan_revision,
                "changed_fields": sorted(set(changed_fields)),
                "previous_status": current.status.value,
                "new_status": updated_task.status.value,
                "blocked_by": list(updated_task.blocked_by),
                "blocks": list(updated_task.blocks),
                "affected_task_ids": affected_task_ids,
                "affected_tasks": [
                    _task_event_snapshot(task)
                    for task in sorted(reverse_tasks, key=lambda item: item.id)
                ],
            },
            created_at=updated_at,
        )
        return TaskToolResult(task=updated_task, next_sequence=self._next_sequence)

    async def send_message(
        self,
        *,
        recipient: str,
        text: str,
        summary: str,
        idempotency_key: str,
        payload: dict[str, Any] | None = None,
        kind: MessageKind = MessageKind.TEXT,
        message_type: MessageType = MessageType.TEXT,
    ) -> MessageToolResult:
        """Send one peer-to-peer message or fan out one broadcast."""
        safe_summary = enforce_max_text(summary, "summary", MAX_MESSAGE_SUMMARY_CHARS)
        safe_text = enforce_max_text(text, "text", MAX_MESSAGE_TEXT_CHARS)
        safe_payload = validate_json_payload(
            payload, max_bytes=MAX_MESSAGE_PAYLOAD_BYTES
        )

        team = await team_store.read_team(
            project_id=self.context.project_id,
            thread_id=self.context.thread_id,
            account_id=self.context.account_id,
        )
        if team is None:
            raise ValueError("team does not exist for this thread")

        normalized_actor = require_key_part(self.context.actor_name, "actor_name")
        member_names = {member.agent_name for member in team.members}
        valid_names = {team.facilitator_id, *member_names}
        if normalized_actor not in valid_names:
            raise ValueError("sender is not a member of this team")

        is_broadcast = message_type == MessageType.BROADCAST or recipient == "*"
        if is_broadcast:
            recipients = [
                member.agent_name
                for member in team.members
                if member.agent_name != normalized_actor
            ]
            if normalized_actor != team.facilitator_id:
                recipients.append(team.facilitator_id)
            if not recipients:
                raise ValueError("broadcast requires at least one recipient")
            if self._messages_sent + len(recipients) > MAX_MESSAGES_PER_TOOL_TURN:
                raise ValueError(
                    f"message turn quota exceeds {MAX_MESSAGES_PER_TOOL_TURN}"
                )
            effective_message_type = MessageType.BROADCAST
        else:
            if self._messages_sent >= MAX_MESSAGES_PER_TOOL_TURN:
                raise ValueError(
                    f"message turn quota exceeds {MAX_MESSAGES_PER_TOOL_TURN}"
                )
            _validate_peer(
                team=team, actor_name=self.context.actor_name, recipient=recipient
            )
            recipients = [require_key_part(recipient, "recipient")]
            effective_message_type = message_type

        created_at = self._clock()
        current_team = team

        async def _send_one(
            *, recipient_name: str, recipient_idempotency_key: str
        ) -> MailboxMessage:
            message = await mailbox.send_message(
                run_id=self.context.run_id,
                thread_id=self.context.thread_id,
                project_id=self.context.project_id,
                account_id=self.context.account_id,
                sender=normalized_actor,
                recipient=require_key_part(recipient_name, "recipient"),
                kind=kind,
                message_type=effective_message_type,
                idempotency_key=require_key_part(
                    recipient_idempotency_key, "idempotency_key"
                ),
                summary=safe_summary,
                text=safe_text,
                payload=safe_payload,
                created_at=created_at,
            )
            should_project_message = bool(getattr(message, "_was_created", True)) or (
                not bool(getattr(message, "_sent_projected", True))
            )
            if not should_project_message:
                return message
            projection_claimed = await mailbox.claim_sent_projection(message)
            if not projection_claimed:
                return message

            try:
                nonlocal current_team
                recipient_agent = _find_team_member(current_team, recipient_name)
                wake_team: TeamConfig | None = None
                woke_agent: AgentIdentity | None = None
                if recipient_agent is not None and _should_wake_idle_agent(
                    recipient_agent,
                    now_text=created_at,
                ):
                    woke_agent = recipient_agent.model_copy(
                        update={
                            "status": AgentLifecycleStatus.WORKING,
                            "current_run_id": self.context.run_id,
                            "idle_since": None,
                            "idle_expires_at": None,
                        }
                    )
                    wake_team = team_store.replace_team_member(current_team, woke_agent)

                projection_state = getattr(message, "_sent_projection_state", None)
                needs_mailbox_sent = projection_state not in {
                    "mailbox_sent",
                    "wake_state",
                    "sent_done",
                }
                needs_wake_state = wake_team is not None and projection_state not in {
                    "wake_state",
                    "sent_done",
                }
                needs_wake_event = (
                    projection_state == "wake_state" or wake_team is not None
                )
                event_count = int(needs_mailbox_sent) + int(needs_wake_event)
                if event_count <= 0:
                    await mailbox.mark_sent_projected(message)
                    return message

                sequence = self._reserve_sequences(event_count)
                if needs_mailbox_sent:
                    self._messages_sent += 1
                next_sequence = sequence
                if needs_mailbox_sent:
                    await event_log.append_event(
                        run_id=self.context.run_id,
                        thread_id=self.context.thread_id,
                        project_id=self.context.project_id,
                        sequence=next_sequence,
                        event_type=EventType.MAILBOX_SENT,
                        activity_owner=_activity_owner(self.context.actor_name),
                        payload={
                            "message_id": message.id,
                            "sender": message.sender,
                            "recipient": message.recipient,
                            "kind": message.kind.value,
                            "message_type": message.type.value,
                            "idempotency_key": message.idempotency_key,
                            "summary": message.summary,
                            "text": message.text,
                            "payload": dict(message.payload or {}),
                        },
                        created_at=created_at,
                    )
                    next_sequence += 1
                    if wake_team is not None:
                        await mailbox.mark_sent_projection_state(
                            message, "mailbox_sent"
                        )
                    else:
                        await mailbox.mark_sent_projected(message)

                if wake_team is not None and woke_agent is not None and needs_wake_state:
                    await team_store.store_team(
                        wake_team,
                        ttl_seconds=self.context.state_ttl_seconds,
                    )
                    current_team = wake_team
                    await mailbox.mark_sent_projection_state(message, "wake_state")

                if needs_wake_event:
                    await event_log.append_event(
                        run_id=self.context.run_id,
                        thread_id=self.context.thread_id,
                        project_id=self.context.project_id,
                        sequence=next_sequence,
                        event_type=EventType.AGENT_WAKE,
                        activity_owner=_activity_owner(self.context.actor_name),
                        payload={
                            "agent_name": (
                                woke_agent.agent_name
                                if woke_agent is not None
                                else message.recipient
                            ),
                            "account_id": (
                                woke_agent.account_id
                                if woke_agent is not None
                                else None
                            ),
                            "wake_reason": "mailbox_unread",
                            "message_id": message.id,
                            "status": AgentLifecycleStatus.WORKING.value,
                        },
                        created_at=created_at,
                    )
                    await mailbox.mark_sent_projected(message)
            except Exception:
                await mailbox.release_sent_projection(message)
                raise
            return message

        last_message: MailboxMessage | None = None
        for recipient_name in recipients:
            recipient_key = (
                f"{idempotency_key}__{recipient_name}"
                if is_broadcast
                else idempotency_key
            )
            last_message = await _send_one(
                recipient_name=recipient_name,
                recipient_idempotency_key=recipient_key,
            )

        if last_message is None:
            raise ValueError("message delivery produced no recipient")
        return MessageToolResult(
            message=last_message,
            next_sequence=self._next_sequence,
        )

    async def message_read(self, *, count: int = 20) -> MessageReadToolResult:
        """Read unread messages addressed to the current agent."""
        read_result = await mailbox.read_next_messages_with_dead_letters(
            project_id=self.context.project_id,
            thread_id=self.context.thread_id,
            account_id=self.context.account_id,
            agent_name=require_key_part(self.context.actor_name, "actor_name"),
            count=count,
        )
        for dead_lettered in read_result.dead_lettered:
            sequence = self._reserve_sequences(1)
            await event_log.append_event(
                run_id=self.context.run_id,
                thread_id=self.context.thread_id,
                project_id=self.context.project_id,
                sequence=sequence,
                event_type=EventType.MAILBOX_DEAD_LETTERED,
                activity_owner=_activity_owner(self.context.actor_name),
                payload={
                    "message_id": dead_lettered.id,
                    "agent_name": dead_lettered.recipient,
                    "reason": dead_lettered.dead_letter_reason,
                },
                created_at=self._clock(),
            )
        return MessageReadToolResult(
            messages=read_result.messages,
            next_sequence=self._next_sequence,
        )

    async def message_ack(self, *, message_id: str) -> MessageAckToolResult:
        """Acknowledge one message addressed to the current agent."""
        acked_at = self._clock()
        ack = await mailbox.ack_message(
            project_id=self.context.project_id,
            thread_id=self.context.thread_id,
            account_id=self.context.account_id,
            agent_name=require_key_part(self.context.actor_name, "actor_name"),
            message_id=require_key_part(message_id, "message_id"),
            acked_by=require_key_part(self.context.actor_name, "actor_name"),
            acked_at=acked_at,
        )
        if not ack.already_acked:
            sequence = self._reserve_sequences(1)
            await event_log.append_event(
                run_id=self.context.run_id,
                thread_id=self.context.thread_id,
                project_id=self.context.project_id,
                sequence=sequence,
                event_type=EventType.MAILBOX_ACKED,
                activity_owner=_activity_owner(self.context.actor_name),
                payload={
                    "message_id": ack.message_id,
                    "agent_name": ack.agent_name,
                    "acked_by": ack.acked_by,
                    "already_acked": ack.already_acked,
                },
                created_at=acked_at,
            )
        return MessageAckToolResult(ack=ack, next_sequence=self._next_sequence)

    async def message_dead_letter(
        self,
        *,
        message_id: str,
        reason: str,
    ) -> MessageDeadLetterToolResult:
        """Move one message addressed to the current agent to dead-letter."""
        dead_lettered_at = self._clock()
        agent_name = require_key_part(self.context.actor_name, "actor_name")
        message = await mailbox.get_message(
            project_id=self.context.project_id,
            thread_id=self.context.thread_id,
            account_id=self.context.account_id,
            agent_name=agent_name,
            message_id=require_key_part(message_id, "message_id"),
        )
        if message is None:
            raise ValueError("message does not exist in recipient mailbox")
        dead_lettered = await mailbox.dead_letter_message(
            message=message,
            reason=reason,
            dead_lettered_at=dead_lettered_at,
        )
        if bool(getattr(dead_lettered, "_was_dead_lettered", True)):
            sequence = self._reserve_sequences(1)
            await event_log.append_event(
                run_id=self.context.run_id,
                thread_id=self.context.thread_id,
                project_id=self.context.project_id,
                sequence=sequence,
                event_type=EventType.MAILBOX_DEAD_LETTERED,
                activity_owner=_activity_owner(self.context.actor_name),
                payload={
                    "message_id": dead_lettered.id,
                    "agent_name": agent_name,
                    "reason": dead_lettered.dead_letter_reason,
                },
                created_at=dead_lettered_at,
            )
        return MessageDeadLetterToolResult(
            message=dead_lettered,
            next_sequence=self._next_sequence,
        )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _activity_owner(actor_name: str) -> str:
    return f"agent:{require_key_part(actor_name, 'actor_name')}"


def _team_id(project_id: str, thread_id: str) -> str:
    return f"shadow-clone-v2:{require_key_part(project_id, 'project_id')}:{require_key_part(thread_id, 'thread_id')}"


def _team_create_phase_key(
    *,
    context: ShadowCloneV2ToolContext,
    phase: str,
) -> str:
    return (
        "sc_v2:"
        f"project:{require_key_part(context.project_id, 'project_id')}:"
        f"thread:{require_key_part(context.thread_id, 'thread_id')}:"
        f"run:{require_key_part(context.run_id, 'run_id')}:"
        f"team_create_phase:{require_key_part(phase, 'phase')}"
    )


def _team_create_session_key(*, context: ShadowCloneV2ToolContext) -> str:
    return (
        "sc_v2:"
        f"project:{require_key_part(context.project_id, 'project_id')}:"
        f"thread:{require_key_part(context.thread_id, 'thread_id')}:"
        f"run:{require_key_part(context.run_id, 'run_id')}:"
        "team_create_session"
    )


def _shutdown_phase_key(
    *,
    context: ShadowCloneV2ToolContext,
    phase: str,
) -> str:
    return (
        "sc_v2:"
        f"project:{require_key_part(context.project_id, 'project_id')}:"
        f"thread:{require_key_part(context.thread_id, 'thread_id')}:"
        f"run:{require_key_part(context.run_id, 'run_id')}:"
        f"shutdown_phase:{require_key_part(phase, 'phase')}"
    )


def _shutdown_session_key(*, context: ShadowCloneV2ToolContext) -> str:
    return (
        "sc_v2:"
        f"project:{require_key_part(context.project_id, 'project_id')}:"
        f"thread:{require_key_part(context.thread_id, 'thread_id')}:"
        f"run:{require_key_part(context.run_id, 'run_id')}:"
        "shutdown_session"
    )


async def _claim_shutdown_session(
    *,
    context: ShadowCloneV2ToolContext,
    reason: str,
    ttl_seconds: int,
) -> str:
    redis_client = await redis_service.get_client()
    key = _shutdown_session_key(context=context)
    existing = await redis_client.get(key)
    existing_text = _decode_redis_text(existing)
    if existing_text:
        if existing_text != reason:
            raise ValueError("shutdown already started with a different reason")
        return existing_text
    stored = await redis_client.set(key, reason, ex=ttl_seconds, nx=True)
    if stored:
        return reason
    existing_after_race = _decode_redis_text(await redis_client.get(key))
    if existing_after_race != reason:
        raise ValueError("shutdown already started with a different reason")
    return existing_after_race


def _agent_identity_event_snapshot(member: AgentIdentity) -> dict[str, Any]:
    return {
        "agent_id": member.agent_id,
        "agent_name": member.agent_name,
        "thread_id": member.thread_id,
        "project_id": member.project_id,
        "account_id": member.account_id,
        "role": member.role,
        "status": member.status.value,
        "current_run_id": member.current_run_id,
        "idle_since": member.idle_since,
        "idle_expires_at": member.idle_expires_at,
        "last_handoff_summary": member.last_handoff_summary,
        "metadata": dict(member.metadata or {}),
    }


def _team_event_snapshot(team: TeamConfig) -> dict[str, Any]:
    return {
        "team_id": team.team_id,
        "thread_id": team.thread_id,
        "project_id": team.project_id,
        "account_id": team.account_id,
        "facilitator_id": team.facilitator_id,
        "persistence_scope": team.persistence_scope.value,
        "idle_ttl_seconds": team.idle_ttl_seconds,
        "max_subagents_per_run": team.max_subagents_per_run,
        "members": [_agent_identity_event_snapshot(member) for member in team.members],
        "schema_version": team.schema_version,
        "budget": dict(team.budget or {}),
        "sandbox_policy": dict(team.sandbox_policy or {}),
        "metadata": dict(team.metadata or {}),
    }


def _team_create_request_digest(
    *,
    context: ShadowCloneV2ToolContext,
    requested_members: list[AgentIdentity],
) -> str:
    payload = {
        "actor_name": context.actor_name,
        "project_id": context.project_id,
        "thread_id": context.thread_id,
        "requested_members": [
            {
                "agent_id": member.agent_id,
                "agent_name": member.agent_name,
                "thread_id": member.thread_id,
                "project_id": member.project_id,
                "account_id": member.account_id,
                "role": member.role,
            }
            for member in sorted(requested_members, key=lambda item: item.agent_name)
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _team_create_session_payload(
    *,
    request_digest: str,
    team: TeamConfig,
    event_specs: list[dict[str, Any]],
    next_sequence: int,
) -> dict[str, Any]:
    return {
        "request_digest": request_digest,
        "team": _team_event_snapshot(team),
        "event_specs": [
            {
                "phase": str(spec["phase"]),
                "sequence": int(spec["sequence"]),
                "event_type": getattr(spec["event_type"], "value", spec["event_type"]),
                "payload": dict(spec["payload"]),
            }
            for spec in event_specs
        ],
        "next_sequence": int(next_sequence),
    }


def _team_from_session_payload(payload: dict[str, Any]) -> TeamConfig:
    team_payload = payload.get("team")
    if not isinstance(team_payload, dict):
        raise ValueError("invalid TeamCreate session: missing team")
    return TeamConfig.model_validate(team_payload)


def _event_specs_from_session_payload(payload: dict[str, Any]) -> list[dict[str, Any]]:
    raw_specs = payload.get("event_specs")
    if not isinstance(raw_specs, list):
        raise ValueError("invalid TeamCreate session: missing event specs")
    specs: list[dict[str, Any]] = []
    for raw_spec in raw_specs:
        if not isinstance(raw_spec, dict):
            raise ValueError("invalid TeamCreate session: event spec must be an object")
        specs.append(
            {
                "phase": str(raw_spec.get("phase") or ""),
                "sequence": int(raw_spec.get("sequence") or 0),
                "event_type": EventType(str(raw_spec.get("event_type") or "")),
                "payload": (
                    dict(raw_spec.get("payload"))
                    if isinstance(raw_spec.get("payload"), dict)
                    else {}
                ),
            }
        )
    return specs


async def _claim_team_create_session(
    *,
    context: ShadowCloneV2ToolContext,
    request_digest: str,
    session_payload: dict[str, Any],
    ttl_seconds: int,
) -> dict[str, Any]:
    redis_client = await redis_service.get_client()
    key = _team_create_session_key(context=context)
    existing_text = _decode_redis_text(await redis_client.get(key))
    if existing_text:
        existing_payload = json.loads(existing_text)
        if existing_payload.get("request_digest") != request_digest:
            raise ValueError(
                "different TeamCreate request already started for this run"
            )
        return existing_payload
    encoded_payload = json.dumps(session_payload, sort_keys=True, separators=(",", ":"))
    stored = await redis_client.set(key, encoded_payload, ex=ttl_seconds, nx=True)
    if stored:
        return session_payload
    existing_after_race_text = _decode_redis_text(await redis_client.get(key))
    existing_after_race = json.loads(existing_after_race_text)
    if existing_after_race.get("request_digest") != request_digest:
        raise ValueError("different TeamCreate request already started for this run")
    return existing_after_race


def _decode_redis_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


async def _claim_team_create_phase(*, phase_key: str, ttl_seconds: int) -> str:
    return await _claim_projection_phase(phase_key=phase_key, ttl_seconds=ttl_seconds)


async def _mark_team_create_phase_done(*, phase_key: str, ttl_seconds: int) -> None:
    await _mark_projection_phase_done(phase_key=phase_key, ttl_seconds=ttl_seconds)


async def _release_team_create_phase(*, phase_key: str) -> None:
    await _release_projection_phase(phase_key=phase_key)


async def _claim_projection_phase(*, phase_key: str, ttl_seconds: int) -> str:
    redis_client = await redis_service.get_client()
    existing = await redis_client.get(phase_key)
    if existing == "done" or existing == b"done":
        return "done"
    if existing == "projecting" or existing == b"projecting":
        return "in_progress"
    claimed = await redis_client.set(phase_key, "projecting", ex=ttl_seconds, nx=True)
    if claimed:
        return "claimed"
    return "in_progress"


async def _mark_projection_phase_done(*, phase_key: str, ttl_seconds: int) -> None:
    redis_client = await redis_service.get_client()
    await redis_client.set(phase_key, "done", ex=ttl_seconds)


async def _release_projection_phase(*, phase_key: str) -> None:
    redis_client = await redis_service.get_client()
    if hasattr(redis_client, "delete"):
        await redis_client.delete(phase_key)


async def _claim_shutdown_phase(*, phase_key: str, ttl_seconds: int) -> str:
    return await _claim_projection_phase(phase_key=phase_key, ttl_seconds=ttl_seconds)


async def _mark_shutdown_phase_done(*, phase_key: str, ttl_seconds: int) -> None:
    await _mark_projection_phase_done(phase_key=phase_key, ttl_seconds=ttl_seconds)


async def _release_shutdown_phase(*, phase_key: str) -> None:
    await _release_projection_phase(phase_key=phase_key)


def _normalize_id_list(values: list[str], name: str) -> list[str]:
    normalized = [require_key_part(value, name) for value in values]
    if len(set(normalized)) != len(normalized):
        raise ValueError(f"duplicate {name}")
    return normalized


def _normalize_task_status(status: TaskStatus | str | None) -> TaskStatus | None:
    if status is None:
        return None
    if isinstance(status, TaskStatus):
        return status
    try:
        return TaskStatus(str(status))
    except ValueError as exc:
        raise ValueError(f"invalid task status: {status}") from exc


def _validate_status_transition(current: TaskStatus, new_status: TaskStatus) -> None:
    if current == new_status:
        return
    terminal_statuses = {
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
        TaskStatus.TIMED_OUT,
        TaskStatus.DEPRECATED,
    }
    if current in terminal_statuses:
        raise ValueError(f"cannot update terminal task status: {current.value}")
    allowed = {
        TaskStatus.PENDING: {
            TaskStatus.BLOCKED,
            TaskStatus.IN_PROGRESS,
            TaskStatus.CANCELLED,
            TaskStatus.DEPRECATED,
        },
        TaskStatus.BLOCKED: {
            TaskStatus.PENDING,
            TaskStatus.IN_PROGRESS,
            TaskStatus.CANCELLED,
            TaskStatus.DEPRECATED,
        },
        TaskStatus.IN_PROGRESS: {
            TaskStatus.COMPLETED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
            TaskStatus.TIMED_OUT,
        },
    }
    if new_status not in allowed.get(current, set()):
        raise ValueError(
            f"invalid task status transition: {current.value} -> {new_status.value}"
        )


def _merged_dependency_ids(
    existing: list[str],
    additions: list[str],
    *,
    field_name: str,
    current_task_id: str,
    task_by_id: dict[str, Task],
) -> list[str]:
    normalized_additions = _normalize_id_list(additions, field_name)
    for dependency_id in normalized_additions:
        if dependency_id == current_task_id:
            raise ValueError("task dependency cannot reference itself")
        if dependency_id not in task_by_id:
            raise ValueError(f"unknown dependency task: {dependency_id}")
    merged = list(dict.fromkeys([*existing, *normalized_additions]))
    return merged


def _reject_dependency_cycle(
    *,
    task_id: str,
    task_by_id: dict[str, Task],
    blocked_by: list[str],
    blocks: list[str],
) -> None:
    graph: dict[str, set[str]] = {
        existing_task.id: set(existing_task.blocks)
        for existing_task in task_by_id.values()
    }
    graph.setdefault(task_id, set())
    graph[task_id] = set(blocks)
    for blocker in blocked_by:
        graph.setdefault(blocker, set()).add(task_id)

    visiting: set[str] = set()
    visited: set[str] = set()

    def _visit(node: str) -> None:
        if node in visiting:
            raise ValueError("dependency cycle detected")
        if node in visited:
            return
        visiting.add(node)
        for child in graph.get(node, set()):
            _visit(child)
        visiting.remove(node)
        visited.add(node)

    for node in sorted(graph):
        _visit(node)


def _build_reverse_dependency_updates(
    *,
    current: Task,
    updated: Task,
    task_by_id: dict[str, Task],
    updated_at: str,
) -> list[Task]:
    reverse_updates: dict[str, Task] = {}
    added_blocked_by = set(updated.blocked_by) - set(current.blocked_by)
    for blocker_id in added_blocked_by:
        blocker = reverse_updates.get(blocker_id) or task_by_id[blocker_id]
        reverse_updates[blocker_id] = blocker.model_copy(
            update={
                "blocks": list(dict.fromkeys([*blocker.blocks, updated.id])),
                "plan_revision": blocker.plan_revision + 1,
                "updated_at": updated_at,
            }
        )
    added_blocks = set(updated.blocks) - set(current.blocks)
    for blocked_id in added_blocks:
        blocked = reverse_updates.get(blocked_id) or task_by_id[blocked_id]
        reverse_updates[blocked_id] = blocked.model_copy(
            update={
                "blocked_by": list(dict.fromkeys([*blocked.blocked_by, updated.id])),
                "plan_revision": blocked.plan_revision + 1,
                "updated_at": updated_at,
            }
        )
    return list(reverse_updates.values())


def _reject_unresolved_blockers_for_active_status(
    *,
    status: TaskStatus,
    blocked_by: list[str],
    task_by_id: dict[str, Task],
) -> None:
    if status not in {TaskStatus.PENDING, TaskStatus.IN_PROGRESS}:
        return
    unresolved = _unresolved_blockers(blocked_by=blocked_by, task_by_id=task_by_id)
    if unresolved:
        raise ValueError(f"task has unresolved blockers: {', '.join(unresolved)}")


def _has_unresolved_blockers(
    *,
    blocked_by: list[str],
    task_by_id: dict[str, Task],
) -> bool:
    return bool(_unresolved_blockers(blocked_by=blocked_by, task_by_id=task_by_id))


def _unresolved_blockers(
    *,
    blocked_by: list[str],
    task_by_id: dict[str, Task],
) -> list[str]:
    return [
        task_id
        for task_id in blocked_by
        if task_by_id[task_id].status != TaskStatus.COMPLETED
    ]


def _reject_terminal_task_mutation(task: Task) -> None:
    if task.status in _terminal_task_statuses():
        raise ValueError(
            "terminal task mutation is not supported; create a follow-up task "
            "or implement an explicit reopen operation"
        )


def _terminal_task_statuses() -> set[TaskStatus]:
    return {
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
        TaskStatus.TIMED_OUT,
        TaskStatus.DEPRECATED,
    }


def _authorize_task_update_actor(
    *,
    team: TeamConfig | None,
    actor_name: str,
    task: Task,
) -> None:
    if team is None:
        raise ValueError("team does not exist for this thread")
    normalized_actor = require_key_part(actor_name, "actor_name")
    member_names = {member.agent_name for member in team.members}
    if normalized_actor == team.facilitator_id:
        return
    if normalized_actor not in member_names:
        raise ValueError("actor is not a member of this team")

    metadata = task.metadata if isinstance(task.metadata, dict) else {}
    assigned_agent = str(metadata.get("agent_name") or "").strip()
    owner_agent = str(task.owner_agent or "").strip()
    if owner_agent and owner_agent != normalized_actor:
        raise ValueError("actor cannot update a task owned by another agent")
    if assigned_agent and assigned_agent != normalized_actor:
        raise ValueError("actor cannot update a task assigned to another agent")


def _validate_assignment_targets(
    *,
    team: TeamConfig | None,
    actor_name: str,
    current: Task,
    next_owner_agent: str | None,
    next_metadata: dict[str, Any],
    member_names: set[str],
) -> None:
    if team is None:
        raise ValueError("team does not exist for this thread")
    normalized_actor = require_key_part(actor_name, "actor_name")
    next_owner = str(next_owner_agent or "").strip()
    if next_owner and next_owner not in member_names:
        raise ValueError(f"unknown owner_agent: {next_owner}")

    next_assigned = str((next_metadata or {}).get("agent_name") or "").strip()
    if next_assigned and next_assigned not in member_names:
        raise ValueError(f"unknown metadata.agent_name: {next_assigned}")

    if normalized_actor == team.facilitator_id:
        return
    if next_owner and next_owner != normalized_actor:
        raise ValueError("actor cannot assign task ownership to another agent")
    if next_assigned and next_assigned != normalized_actor:
        raise ValueError("actor cannot assign task to another agent")


def _build_members(
    *,
    members: list[dict[str, str]],
    thread_id: str,
    project_id: str,
    account_id: str | None = None,
) -> list[AgentIdentity]:
    if not members:
        raise ValueError("members are required")
    if len(members) > SHADOW_CLONE_V2_MAX_SUBAGENTS_PER_RUN:
        raise ValueError(
            f"max subagents exceeded: {len(members)} > "
            f"{SHADOW_CLONE_V2_MAX_SUBAGENTS_PER_RUN}"
        )
    normalized_members: list[AgentIdentity] = []
    seen_names: set[str] = set()
    for member in members:
        agent_name = require_key_part(member.get("agent_name", ""), "agent_name")
        if agent_name in seen_names:
            raise ValueError(f"duplicate agent_name: {agent_name}")
        seen_names.add(agent_name)
        role = require_non_empty_text(member.get("role", ""), "role")
        normalized_members.append(
            AgentIdentity(
                agent_id=f"{agent_name}@{thread_id}",
                agent_name=agent_name,
                thread_id=thread_id,
                project_id=project_id,
                account_id=account_id,
                role=role,
                status=AgentLifecycleStatus.STARTING,
            )
        )
    return normalized_members


def _validate_peer(*, team: TeamConfig, actor_name: str, recipient: str) -> None:
    normalized_actor = require_key_part(actor_name, "actor_name")
    normalized_recipient = require_key_part(recipient, "recipient")
    member_names = {member.agent_name for member in team.members}
    valid_names = {team.facilitator_id, *member_names}
    if normalized_actor not in valid_names:
        raise ValueError("sender is not a member of this team")
    if normalized_recipient not in valid_names:
        raise ValueError("recipient is not a member of this team")
    if normalized_actor == normalized_recipient:
        raise ValueError("recipient must be a different agent")


def _find_team_member(team: TeamConfig, agent_name: str) -> AgentIdentity | None:
    normalized = require_key_part(agent_name, "agent_name")
    for member in team.members:
        if member.agent_name == normalized:
            return member
    return None


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = str(value)
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def _should_wake_idle_agent(agent: AgentIdentity, *, now_text: str) -> bool:
    if agent.status != AgentLifecycleStatus.IDLE:
        return False
    expires_at = _parse_datetime(agent.idle_expires_at)
    now = _parse_datetime(now_text)
    if expires_at is None or now is None:
        return True
    return expires_at > now


def _json_tool_response(payload: dict[str, Any]) -> Any:
    """Return an AgentScope-compatible text ToolResponse for a JSON payload."""
    from agentscope.message import TextBlock
    from agentscope.tool import ToolResponse

    return ToolResponse(
        content=[
            TextBlock(
                type="text",
                text=json.dumps(payload, ensure_ascii=False),
            )
        ]
    )


def register_agent_tools(
    toolkit: Any,
    *,
    context: ShadowCloneV2ToolContext,
    clock: Clock | None = None,
    tools: ShadowCloneV2AgentTools | None = None,
) -> ShadowCloneV2AgentTools:
    """Register Shadow Clone V2 Team/Task/Message tools on an AgentScope toolkit.

    The returned ``ShadowCloneV2AgentTools`` instance owns per-turn counters such
    as message quota. Wrappers return compact JSON so model-facing tool results
    can be consumed by AgentScope without importing project-specific ToolResult
    classes.
    """
    tools = tools or ShadowCloneV2AgentTools(context=context, clock=clock)

    async def team_create(members: list[dict[str, str]]) -> Any:
        """Create or replace the Shadow Clone V2 team.

        Args:
            members: List of team members with agent_name and role.
        """
        result = await tools.team_create(members=members)
        return _json_tool_response(
            {
                "team_id": result.team.team_id,
                "members": [member.model_dump() for member in result.team.members],
                "next_sequence": result.next_sequence,
            },
        )

    async def team_shutdown(reason: str) -> Any:
        """Shutdown an idle Shadow Clone V2 team after work is complete.

        Args:
            reason: Non-empty shutdown reason, e.g. work_complete.
        """
        result = await tools.team_shutdown(reason=reason)
        return _json_tool_response(
            {
                "team_id": result.team.team_id,
                "members": [member.model_dump() for member in result.team.members],
                "metadata": result.team.metadata,
                "next_sequence": result.next_sequence,
            },
        )

    async def task_create(
        task_id: str,
        subject: str,
        description: str,
        blocked_by: list[str] | None = None,
        blocks: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Any:
        """Create a Shadow Clone V2 living task document.

        Args:
            task_id: Key-safe task id unique within the run.
            subject: Short task title.
            description: Detailed task instructions. Do not copy prior delivery
                marker strings or prior output text into follow-up, wake, or
                idle-reuse task descriptions; reference prior work by artifact
                path or short summary and include only the current request's
                new required output markers.
            blocked_by: Rejected in this phase; link dependencies with TaskUpdate.
            blocks: Rejected in this phase; link dependencies with TaskUpdate.
            metadata: Optional JSON metadata.
        """
        result = await tools.task_create(
            task_id=task_id,
            subject=subject,
            description=description,
            blocked_by=blocked_by,
            blocks=blocks,
            metadata=metadata,
        )
        return _json_tool_response(
            {
                "task": result.task.model_dump(),
                "version": result.task.plan_revision,
                "next_sequence": result.next_sequence,
            },
        )

    async def task_list() -> Any:
        """List Shadow Clone V2 living task documents for the current run."""
        result = await tools.task_list()
        return _json_tool_response(
            {
                "tasks": [
                    {**task.model_dump(), "version": task.plan_revision}
                    for task in result.tasks
                ],
                "next_sequence": result.next_sequence,
            },
        )

    async def task_get(task_id: str) -> Any:
        """Read one full Shadow Clone V2 living task document.

        Args:
            task_id: Key-safe task id unique within the run.
        """
        result = await tools.task_get(task_id=task_id)
        return _json_tool_response(
            {
                "task": result.task.model_dump(),
                "version": result.task.plan_revision,
                "next_sequence": result.next_sequence,
            },
        )

    async def task_update(
        task_id: str,
        expected_version: int,
        subject: str | None = None,
        description: str | None = None,
        status: str | None = None,
        owner_agent: str | None = None,
        metadata: dict[str, Any] | None = None,
        add_blocked_by: list[str] | None = None,
        add_blocks: list[str] | None = None,
        result_ref: str | None = None,
        error: dict[str, Any] | None = None,
    ) -> Any:
        """Patch a Shadow Clone V2 task using its current expected version.

        Args:
            task_id: Key-safe task id unique within the run.
            expected_version: Current task version from TaskGet/TaskList
                (`task.version` alias or `task.plan_revision`).
            subject: Optional replacement subject.
            description: Optional replacement description.
            status: Optional replacement status.
            owner_agent: Optional owner/assignee agent name.
            metadata: Optional JSON metadata merged into existing metadata.
            add_blocked_by: Optional task ids that block this task.
            add_blocks: Optional task ids this task blocks.
            result_ref: Optional result reference.
            error: Optional JSON error metadata.
        """
        result = await tools.task_update(
            task_id=task_id,
            expected_version=expected_version,
            subject=subject,
            description=description,
            status=status,
            owner_agent=owner_agent,
            metadata=metadata,
            add_blocked_by=add_blocked_by,
            add_blocks=add_blocks,
            result_ref=result_ref,
            error=error,
        )
        return _json_tool_response(
            {
                "task": result.task.model_dump(),
                "version": result.task.plan_revision,
                "next_sequence": result.next_sequence,
            },
        )

    async def send_message(
        recipient: str,
        text: str,
        summary: str,
        idempotency_key: str,
        payload: dict[str, Any] | None = None,
        message_type: str = "text",
    ) -> Any:
        """Send a Shadow Clone V2 message to a teammate or broadcast to all teammates.

        Args:
            recipient: Recipient agent_name/team facilitator id, or `*` for broadcast.
            text: Message body.
            summary: Short UI preview.
            idempotency_key: Unique key for dedupe.
            payload: Optional JSON metadata.
            message_type: `text` for one recipient or `broadcast` for fan-out.
        """
        try:
            parsed_message_type = MessageType(str(message_type or "text"))
        except ValueError as exc:
            raise ValueError(f"unsupported message_type: {message_type}") from exc
        result = await tools.send_message(
            recipient=recipient,
            text=text,
            summary=summary,
            idempotency_key=idempotency_key,
            payload=payload,
            message_type=parsed_message_type,
        )
        return _json_tool_response(
            {
                "message": result.message.model_dump(),
                "next_sequence": result.next_sequence,
            },
        )

    async def message_read(count: int = 20) -> Any:
        """Read unread Shadow Clone V2 messages addressed to this agent.

        Args:
            count: Maximum messages to return. Control-lane messages are
                returned before text-lane messages.
        """
        result = await tools.message_read(count=count)
        return _json_tool_response(
            {
                "messages": [message.model_dump() for message in result.messages],
                "next_sequence": result.next_sequence,
            },
        )

    async def message_ack(message_id: str) -> Any:
        """Acknowledge one Shadow Clone V2 message addressed to this agent.

        Args:
            message_id: Redis stream id of the message to acknowledge.
        """
        result = await tools.message_ack(message_id=message_id)
        return _json_tool_response(
            {
                "ack": {
                    "message_id": result.ack.message_id,
                    "agent_name": result.ack.agent_name,
                    "acked_by": result.ack.acked_by,
                    "acked_at": result.ack.acked_at,
                    "already_acked": result.ack.already_acked,
                },
                "next_sequence": result.next_sequence,
            },
        )

    async def message_dead_letter(message_id: str, reason: str) -> Any:
        """Move one Shadow Clone V2 message to this agent's dead-letter lane.

        Args:
            message_id: Redis stream id of the message to dead-letter.
            reason: Non-empty diagnostic reason.
        """
        result = await tools.message_dead_letter(
            message_id=message_id,
            reason=reason,
        )
        return _json_tool_response(
            {
                "message": result.message.model_dump(),
                "next_sequence": result.next_sequence,
            },
        )

    for func in (
        team_create,
        team_shutdown,
        task_create,
        task_get,
        task_update,
        task_list,
        send_message,
        message_read,
        message_ack,
        message_dead_letter,
    ):
        toolkit.register_tool_function(func)
    return tools
