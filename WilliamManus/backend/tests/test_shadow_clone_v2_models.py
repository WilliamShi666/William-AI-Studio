from __future__ import annotations

import pytest
from pydantic import ValidationError

from agentscope_integration.shadow_clone_v2.constants import (
    SHADOW_CLONE_V2_IDLE_TTL_SECONDS,
    SHADOW_CLONE_V2_MAX_SUBAGENTS_PER_RUN,
)
from agentscope_integration.shadow_clone_v2.models import (
    AgentIdentity,
    AgentLifecycleStatus,
    AgentPersistenceScope,
    Task,
    TaskStatus,
    TeamConfig,
)


def _agent(index: int) -> AgentIdentity:
    return AgentIdentity(
        agent_id=f"agent-{index}@thread-1",
        agent_name=f"agent-{index}",
        thread_id="thread-1",
        project_id="project-1",
        role="specialist",
        status=AgentLifecycleStatus.IDLE,
    )


def test_thread_scoped_idle_persistence_defaults_to_twenty_minutes() -> None:
    team = TeamConfig(
        team_id="team-thread-1",
        thread_id="thread-1",
        project_id="project-1",
        facilitator_id="facilitator@thread-1",
    )

    assert SHADOW_CLONE_V2_IDLE_TTL_SECONDS == 20 * 60
    assert team.persistence_scope == AgentPersistenceScope.THREAD
    assert team.idle_ttl_seconds == 20 * 60


def test_per_run_subagent_capacity_is_capped_at_ten_members() -> None:
    team = TeamConfig(
        team_id="team-thread-1",
        thread_id="thread-1",
        project_id="project-1",
        facilitator_id="facilitator@thread-1",
        members=[_agent(index) for index in range(SHADOW_CLONE_V2_MAX_SUBAGENTS_PER_RUN)],
    )

    assert len(team.members) == 10
    assert SHADOW_CLONE_V2_MAX_SUBAGENTS_PER_RUN == 10

    with pytest.raises(ValidationError, match="at most 10 subagents"):
        TeamConfig(
            team_id="team-thread-1",
            thread_id="thread-1",
            project_id="project-1",
            facilitator_id="facilitator@thread-1",
            members=[_agent(index) for index in range(11)],
        )



def test_team_config_can_lower_but_not_ignore_the_per_run_capacity() -> None:
    with pytest.raises(ValidationError, match="at most 2 subagents"):
        TeamConfig(
            team_id="team-thread-1",
            thread_id="thread-1",
            project_id="project-1",
            facilitator_id="facilitator@thread-1",
            max_subagents_per_run=2,
            members=[_agent(index) for index in range(3)],
        )

def test_task_model_uses_safe_mutable_defaults_and_v2_statuses() -> None:
    first = Task(
        id="task-1",
        run_id="run-1",
        thread_id="thread-1",
        subject="Research",
        description="Find evidence.",
        created_at="2026-05-31T00:00:00Z",
        updated_at="2026-05-31T00:00:00Z",
    )
    second = Task(
        id="task-2",
        run_id="run-1",
        thread_id="thread-1",
        subject="Review",
        description="Review evidence.",
        created_at="2026-05-31T00:00:00Z",
        updated_at="2026-05-31T00:00:00Z",
    )

    first.blocked_by.append("task-x")
    first.metadata["source"] = "test"

    assert second.blocked_by == []
    assert second.metadata == {}
    assert TaskStatus.TIMED_OUT.value == "timed_out"
    assert TaskStatus.DEPRECATED.value == "deprecated"
