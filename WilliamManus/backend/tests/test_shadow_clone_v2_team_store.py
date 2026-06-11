from __future__ import annotations

import pytest

from agentscope_integration.shadow_clone_v2 import team_store
from agentscope_integration.shadow_clone_v2.models import (
    AgentIdentity,
    AgentLifecycleStatus,
    TeamConfig,
)


@pytest.mark.asyncio
async def test_store_team_persists_thread_scoped_team_config(monkeypatch) -> None:
    stored = {}

    async def _set(key, value, ex=None, nx=False, timeout=None):
        stored[key] = {"value": value, "ex": ex, "nx": nx, "timeout": timeout}
        return True

    monkeypatch.setattr(team_store.redis_service, "set", _set)
    team = TeamConfig(
        team_id="shadow-clone-v2:project-1:thread-1",
        thread_id="thread-1",
        project_id="project-1",
        facilitator_id="facilitator@thread-1",
    )

    await team_store.store_team(team, ttl_seconds=3600)

    assert "sc_v2:project:project-1:thread:thread-1:team" in stored
    assert stored["sc_v2:project:project-1:thread:thread-1:team"]["ex"] == 3600


@pytest.mark.asyncio
async def test_read_team_loads_thread_scoped_team_config(monkeypatch) -> None:
    team = TeamConfig(
        team_id="shadow-clone-v2:project-1:thread-1",
        thread_id="thread-1",
        project_id="project-1",
        facilitator_id="facilitator@thread-1",
    )

    async def _get(key, default=None, timeout=None):
        assert key == "sc_v2:project:project-1:thread:thread-1:team"
        return team.model_dump_json()

    monkeypatch.setattr(team_store.redis_service, "get", _get)

    loaded = await team_store.read_team(project_id="project-1", thread_id="thread-1")

    assert loaded == team


@pytest.mark.asyncio
async def test_store_and_read_team_scope_by_account_when_available(monkeypatch) -> None:
    stored = {}
    team = TeamConfig(
        team_id="shadow-clone-v2:project-1:thread-1",
        thread_id="thread-1",
        project_id="project-1",
        account_id="account-a",
        facilitator_id="facilitator@thread-1",
    )

    async def _set(key, value, ex=None, nx=False, timeout=None):
        stored[key] = {"value": value, "ex": ex, "nx": nx, "timeout": timeout}
        return True

    async def _get(key, default=None, timeout=None):
        return stored.get(key, {}).get("value")

    monkeypatch.setattr(team_store.redis_service, "set", _set)
    monkeypatch.setattr(team_store.redis_service, "get", _get)

    await team_store.store_team(team, ttl_seconds=3600)

    account_a_key = "sc_v2:account:account-a:project:project-1:thread:thread-1:team"
    assert account_a_key in stored
    assert (
        await team_store.read_team(
            project_id="project-1",
            thread_id="thread-1",
            account_id="account-a",
        )
    ) == team
    assert (
        await team_store.read_team(
            project_id="project-1",
            thread_id="thread-1",
            account_id="account-b",
        )
        is None
    )


def test_replace_team_member_updates_existing_member_without_mutating_original() -> (
    None
):
    original_agent = AgentIdentity(
        agent_id="reviewer@thread-1",
        agent_name="reviewer",
        thread_id="thread-1",
        project_id="project-1",
        role="reviewer",
        status=AgentLifecycleStatus.WORKING,
    )
    idle_agent = original_agent.model_copy(
        update={
            "status": AgentLifecycleStatus.IDLE,
            "idle_since": "2026-05-31T12:00:00+00:00",
            "idle_expires_at": "2026-05-31T12:20:00+00:00",
        }
    )
    team = TeamConfig(
        team_id="shadow-clone-v2:project-1:thread-1",
        thread_id="thread-1",
        project_id="project-1",
        facilitator_id="facilitator@thread-1",
        members=[original_agent],
    )

    updated = team_store.replace_team_member(team, idle_agent)

    assert team.members[0].status == AgentLifecycleStatus.WORKING
    assert updated.members[0].status == AgentLifecycleStatus.IDLE
    assert updated.members[0].idle_expires_at == "2026-05-31T12:20:00+00:00"
