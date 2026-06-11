from __future__ import annotations

from datetime import datetime, timezone

from agentscope_integration.shadow_clone_v2.agent_registry import (
    find_expired_idle_agents,
    mark_agent_idle,
)
from agentscope_integration.shadow_clone_v2.models import (
    AgentIdentity,
    AgentLifecycleStatus,
)


def _agent(name: str, status: AgentLifecycleStatus = AgentLifecycleStatus.WORKING) -> AgentIdentity:
    return AgentIdentity(
        agent_id=f"{name}@thread-1",
        agent_name=name,
        thread_id="thread-1",
        project_id="project-1",
        role="specialist",
        status=status,
    )


def test_mark_agent_idle_sets_twenty_minute_expiration_without_mutating_original() -> None:
    now = datetime(2026, 5, 31, 12, 0, 0, tzinfo=timezone.utc)
    working = _agent("reviewer")

    idle = mark_agent_idle(working, now=now)

    assert working.status == AgentLifecycleStatus.WORKING
    assert idle.status == AgentLifecycleStatus.IDLE
    assert idle.idle_since == "2026-05-31T12:00:00+00:00"
    assert idle.idle_expires_at == "2026-05-31T12:20:00+00:00"


def test_find_expired_idle_agents_returns_only_idle_agents_past_ttl() -> None:
    now = datetime(2026, 5, 31, 12, 21, 0, tzinfo=timezone.utc)
    expired = _agent("expired", AgentLifecycleStatus.IDLE).model_copy(
        update={
            "idle_since": "2026-05-31T12:00:00+00:00",
            "idle_expires_at": "2026-05-31T12:20:00+00:00",
        }
    )
    still_idle = _agent("still-idle", AgentLifecycleStatus.IDLE).model_copy(
        update={
            "idle_since": "2026-05-31T12:05:00+00:00",
            "idle_expires_at": "2026-05-31T12:25:00+00:00",
        }
    )
    working = _agent("working", AgentLifecycleStatus.WORKING).model_copy(
        update={"idle_expires_at": "2026-05-31T12:10:00+00:00"}
    )

    expired_agents = find_expired_idle_agents([expired, still_idle, working], now=now)

    assert [agent.agent_name for agent in expired_agents] == ["expired"]
