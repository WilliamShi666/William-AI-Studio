"""Shadow Clone V2 runtime primitives."""

from .agent_loop import AgentTurnResult, run_agent_turn
from .constants import (
    SHADOW_CLONE_V2_IDLE_TTL_SECONDS,
    SHADOW_CLONE_V2_MAX_SUBAGENTS_PER_RUN,
    SHADOW_CLONE_V2_EVENT_STREAM_MAXLEN,
    SHADOW_CLONE_V2_MAILBOX_STREAM_MAXLEN,
)
from .task_executor import (
    AgentScopeShadowCloneV2TaskExecutor,
    ShadowCloneV2RunContext,
    ShadowCloneV2TaskExecutor,
    StubShadowCloneV2TaskExecutor,
)
from .policy import (
    ShadowCloneV2CapacityError,
    SubagentCapacityDecision,
    compute_idle_expiration,
    validate_run_subagent_capacity,
)
from .models import (
    AgentIdentity,
    AgentLifecycleStatus,
    AgentPersistenceScope,
    EventRecord,
    EventType,
    MailboxMessage,
    MessageKind,
    MessageType,
    Task,
    TaskStatus,
    TeamConfig,
)

__all__ = [
    "AgentTurnResult",
    "run_agent_turn",
    "SHADOW_CLONE_V2_IDLE_TTL_SECONDS",
    "SHADOW_CLONE_V2_MAX_SUBAGENTS_PER_RUN",
    "SHADOW_CLONE_V2_EVENT_STREAM_MAXLEN",
    "SHADOW_CLONE_V2_MAILBOX_STREAM_MAXLEN",
    "AgentIdentity",
    "AgentLifecycleStatus",
    "AgentPersistenceScope",
    "EventRecord",
    "EventType",
    "MailboxMessage",
    "MessageKind",
    "MessageType",
    "Task",
    "TaskStatus",
    "TeamConfig",
    "ShadowCloneV2CapacityError",
    "SubagentCapacityDecision",
    "compute_idle_expiration",
    "validate_run_subagent_capacity",
    "ShadowCloneV2RunContext",
    "ShadowCloneV2TaskExecutor",
    "AgentScopeShadowCloneV2TaskExecutor",
    "StubShadowCloneV2TaskExecutor",
]
