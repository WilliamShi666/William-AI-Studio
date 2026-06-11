"""Pydantic models for Shadow Clone V2 runtime state."""

from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

from .constants import (
    SHADOW_CLONE_V2_IDLE_TTL_SECONDS,
    SHADOW_CLONE_V2_MAX_SUBAGENTS_PER_RUN,
)


class AgentPersistenceScope(str, Enum):
    """Supported persistence boundaries for V2 agent teams."""

    THREAD = "thread"


class AgentLifecycleStatus(str, Enum):
    """Lifecycle states for thread-scoped persistent agents."""

    STARTING = "starting"
    WORKING = "working"
    IDLE = "idle"
    SHUTTING_DOWN = "shutting_down"
    CLOSED = "closed"
    FAILED = "failed"


class TaskStatus(str, Enum):
    """Task states used by the V2 task pool."""

    PENDING = "pending"
    BLOCKED = "blocked"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    DEPRECATED = "deprecated"


class EventType(str, Enum):
    """Durable event types for V2 replay and debugging."""

    RUN_STARTED = "run_started"
    RUN_COMPLETED = "run_completed"
    RUN_FAILED = "run_failed"
    TEAM_CREATED = "team_created"
    TEAM_UPDATED = "team_updated"
    TEAM_DELETED = "team_deleted"
    TASK_CREATED = "task_created"
    TASK_CLAIMED = "task_claimed"
    TASK_UPDATED = "task_updated"
    TASK_COMPLETED = "task_completed"
    TASK_FAILED = "task_failed"
    AGENT_SPAWNED = "agent_spawned"
    AGENT_IDLE = "agent_idle"
    AGENT_WAKE = "agent_wake"
    AGENT_SHUTDOWN = "agent_shutdown"
    MAILBOX_SENT = "mailbox_sent"
    MAILBOX_ACKED = "mailbox_acked"
    MAILBOX_DEAD_LETTERED = "mailbox_dead_lettered"
    TOOL_CALL_STARTED = "tool_call_started"
    TOOL_CALL_COMPLETED = "tool_call_completed"
    TOOL_CALL_FAILED = "tool_call_failed"
    SANDBOX_LEASE_ACQUIRED = "sandbox_lease_acquired"
    SANDBOX_LEASE_RELEASED = "sandbox_lease_released"


class EventRecord(BaseModel):
    """Append-only V2 event-log record."""

    id: str
    run_id: str
    thread_id: str
    project_id: str
    sequence: int
    type: EventType
    activity_owner: str = "system"
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: str


class MessageKind(str, Enum):
    """Mailbox stream lane for a message."""

    CONTROL = "control"
    TEXT = "text"
    TASK = "task"
    EVENT_SUMMARY = "event_summary"


class MessageType(str, Enum):
    """Structured mailbox message type."""

    TEXT = "text"
    BROADCAST = "broadcast"
    WAKE = "wake"
    SHUTDOWN = "shutdown"
    HANDOFF_REQUEST = "handoff_request"
    HANDOFF_RESPONSE = "handoff_response"
    PERMISSION_REQUEST = "permission_request"
    PERMISSION_RESPONSE = "permission_response"
    PLAN_APPROVAL_REQUEST = "plan_approval_request"
    PLAN_APPROVAL_RESPONSE = "plan_approval_response"


class MailboxMessage(BaseModel):
    """Durable mailbox message stored in a recipient stream."""

    id: str
    run_id: str
    thread_id: str
    project_id: str
    account_id: str | None = None
    sender: str
    recipient: str
    kind: MessageKind
    type: MessageType
    idempotency_key: str
    summary: str | None = None
    text: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    priority: int = 100
    delivery_attempts: int = 0
    read_at: str | None = None
    acked_at: str | None = None
    acked_by: str | None = None
    dead_lettered_at: str | None = None
    dead_letter_reason: str | None = None
    created_at: str
    expires_at: str | None = None


class AgentIdentity(BaseModel):
    """Stable identity for a subagent inside a thread-scoped team."""

    agent_id: str
    agent_name: str
    thread_id: str
    project_id: str
    account_id: str | None = None
    role: str
    status: AgentLifecycleStatus = AgentLifecycleStatus.STARTING
    current_run_id: str | None = None
    idle_since: str | None = None
    idle_expires_at: str | None = None
    last_handoff_summary: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class TeamConfig(BaseModel):
    """Thread-scoped Shadow Clone V2 team configuration."""

    team_id: str
    thread_id: str
    project_id: str
    account_id: str | None = None
    facilitator_id: str
    persistence_scope: AgentPersistenceScope = AgentPersistenceScope.THREAD
    idle_ttl_seconds: int = SHADOW_CLONE_V2_IDLE_TTL_SECONDS
    max_subagents_per_run: int = SHADOW_CLONE_V2_MAX_SUBAGENTS_PER_RUN
    members: list[AgentIdentity] = Field(default_factory=list)
    schema_version: int = 1
    budget: dict[str, Any] = Field(default_factory=dict)
    sandbox_policy: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("idle_ttl_seconds", "max_subagents_per_run")
    @classmethod
    def _positive_int(cls, value: int) -> int:
        if value <= 0:
            raise ValueError("must be positive")
        return value

    @model_validator(mode="after")
    def _enforce_subagent_cap(self) -> "TeamConfig":
        effective_cap = min(
            int(self.max_subagents_per_run),
            SHADOW_CLONE_V2_MAX_SUBAGENTS_PER_RUN,
        )
        if len(self.members) > effective_cap:
            raise ValueError(
                "Shadow Clone V2 team may use at most "
                f"{effective_cap} subagents per run"
            )
        return self


class Task(BaseModel):
    """Durable task record for the V2 task pool."""

    id: str
    run_id: str
    thread_id: str
    subject: str
    description: str
    status: TaskStatus = TaskStatus.PENDING
    priority: int = 100
    blocked_by: list[str] = Field(default_factory=list)
    blocks: list[str] = Field(default_factory=list)
    owner_agent: str | None = None
    lease_token: str | None = None
    lease_expires_at: str | None = None
    attempt: int = 0
    max_attempts: int = 2
    created_by: str = "facilitator"
    plan_revision: int = 1
    result_ref: str | None = None
    error: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    updated_at: str
