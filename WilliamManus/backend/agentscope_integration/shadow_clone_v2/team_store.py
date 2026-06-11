"""Persistence helpers for thread-scoped Shadow Clone V2 teams."""

from __future__ import annotations

from services import redis as redis_service

from .keys import thread_team_key
from .models import AgentIdentity, TeamConfig


async def store_team(
    team: TeamConfig,
    *,
    ttl_seconds: int,
    timeout: float | None = None,
) -> None:
    """Persist a thread-scoped V2 team config."""
    await redis_service.set(
        thread_team_key(
            project_id=team.project_id,
            thread_id=team.thread_id,
            account_id=team.account_id,
        ),
        team.model_dump_json(),
        ex=ttl_seconds,
        timeout=timeout,
    )


async def read_team(
    *,
    project_id: str,
    thread_id: str,
    account_id: str | None = None,
    timeout: float | None = None,
) -> TeamConfig | None:
    """Read the thread-scoped V2 team config if it exists."""
    raw = await redis_service.get(
        thread_team_key(
            project_id=project_id,
            thread_id=thread_id,
            account_id=account_id,
        ),
        timeout=timeout,
    )
    if not raw:
        return None
    return TeamConfig.model_validate_json(raw)


def replace_team_member(team: TeamConfig, agent: AgentIdentity) -> TeamConfig:
    """Return a copy of team with one member replaced or appended by agent_name."""
    replaced = False
    members: list[AgentIdentity] = []
    for member in team.members:
        if member.agent_name == agent.agent_name:
            members.append(agent)
            replaced = True
        else:
            members.append(member)
    if not replaced:
        members.append(agent)
    return team.model_copy(update={"members": members})
