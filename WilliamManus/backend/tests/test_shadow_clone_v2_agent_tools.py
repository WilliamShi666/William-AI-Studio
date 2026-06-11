from __future__ import annotations

import asyncio

import pytest

from agentscope_integration.shadow_clone_v2 import agent_tools
from agentscope_integration.shadow_clone_v2.models import (
    AgentIdentity,
    AgentLifecycleStatus,
    EventType,
    MessageKind,
    MessageType,
    TaskStatus,
    TeamConfig,
)


def _context(actor_name: str = "team-lead") -> agent_tools.ShadowCloneV2ToolContext:
    return agent_tools.ShadowCloneV2ToolContext(
        run_id="run-1",
        thread_id="thread-1",
        project_id="project-1",
        account_id="account-1",
        actor_name=actor_name,
        sequence_start=10,
        state_ttl_seconds=3600,
    )


def _context_with_user_message(
    *, actor_name: str = "team-lead", current_user_message: str
) -> agent_tools.ShadowCloneV2ToolContext:
    return agent_tools.ShadowCloneV2ToolContext(
        run_id="run-1",
        thread_id="thread-1",
        project_id="project-1",
        account_id="account-1",
        actor_name=actor_name,
        sequence_start=10,
        state_ttl_seconds=3600,
        current_user_message=current_user_message,
    )


def _team() -> TeamConfig:
    return TeamConfig(
        team_id="shadow-clone-v2:project-1:thread-1",
        thread_id="thread-1",
        project_id="project-1",
        account_id="account-1",
        facilitator_id="team-lead",
        members=[
            AgentIdentity(
                agent_id="researcher@thread-1",
                agent_name="researcher",
                thread_id="thread-1",
                project_id="project-1",
                account_id="account-1",
                role="research analyst",
            ),
            AgentIdentity(
                agent_id="reviewer@thread-1",
                agent_name="reviewer",
                thread_id="thread-1",
                project_id="project-1",
                account_id="account-1",
                role="quality reviewer",
            ),
        ],
    )


@pytest.fixture(autouse=True)
def _default_team_create_session(monkeypatch):
    async def _claim_team_create_session(**kwargs):
        return kwargs["session_payload"]

    monkeypatch.setattr(
        agent_tools,
        "_claim_team_create_session",
        _claim_team_create_session,
        raising=False,
    )


class _AsyncBarrier:
    def __init__(self, parties: int) -> None:
        self._parties = parties
        self._arrived = 0
        self._event = asyncio.Event()

    async def wait(self) -> None:
        self._arrived += 1
        if self._arrived >= self._parties:
            self._event.set()
        await self._event.wait()


@pytest.mark.asyncio
async def test_team_create_tool_persists_members_and_appends_tool_events(
    monkeypatch,
) -> None:
    stored_teams = []
    appended_events = []

    async def _read_team(**_kwargs):
        return None

    async def _store_team(team, *, ttl_seconds, timeout=None):
        stored_teams.append((team, ttl_seconds, timeout))

    async def _append_event(**kwargs):
        appended_events.append(kwargs)
        return object()

    monkeypatch.setattr(agent_tools.team_store, "read_team", _read_team)
    monkeypatch.setattr(agent_tools.team_store, "store_team", _store_team)
    monkeypatch.setattr(agent_tools.event_log, "append_event", _append_event)

    tools = agent_tools.ShadowCloneV2AgentTools(
        context=_context(),
        clock=lambda: "2026-06-02T00:00:00+00:00",
    )

    result = await tools.team_create(
        members=[
            {"agent_name": "researcher", "role": "research analyst"},
            {"agent_name": "reviewer", "role": "quality reviewer"},
        ]
    )

    assert result.team.team_id == "shadow-clone-v2:project-1:thread-1"
    assert [member.agent_name for member in result.team.members] == [
        "researcher",
        "reviewer",
    ]
    assert result.team.max_subagents_per_run == 10
    assert stored_teams[0][1] == 3600
    assert [event["event_type"] for event in appended_events] == [
        EventType.TEAM_CREATED,
        EventType.AGENT_SPAWNED,
        EventType.AGENT_SPAWNED,
    ]
    assert appended_events[0]["activity_owner"] == "agent:team-lead"
    assert appended_events[0]["sequence"] == 10
    assert appended_events[1]["payload"]["agent_name"] == "researcher"
    assert result.next_sequence == 13


@pytest.mark.asyncio
async def test_team_create_tool_reuses_idle_existing_member_and_emits_wake(
    monkeypatch,
) -> None:
    stored_teams = []
    appended_events = []
    existing_team = _team().model_copy(
        update={
            "members": [
                _team()
                .members[0]
                .model_copy(
                    update={
                        "status": AgentLifecycleStatus.IDLE,
                        "idle_since": "2026-06-02T00:00:00+00:00",
                        "idle_expires_at": "2026-06-02T00:20:00+00:00",
                        "current_run_id": "previous-run",
                    }
                ),
            ]
        }
    )

    async def _read_team(**_kwargs):
        return existing_team

    async def _store_team(team, *, ttl_seconds, timeout=None):
        stored_teams.append((team, ttl_seconds, timeout))

    async def _append_event(**kwargs):
        appended_events.append(kwargs)
        return object()

    monkeypatch.setattr(agent_tools.team_store, "read_team", _read_team)
    monkeypatch.setattr(agent_tools.team_store, "store_team", _store_team)
    monkeypatch.setattr(agent_tools.event_log, "append_event", _append_event)

    tools = agent_tools.ShadowCloneV2AgentTools(
        context=_context(),
        clock=lambda: "2026-06-02T00:05:00+00:00",
    )

    result = await tools.team_create(
        members=[{"agent_name": "researcher", "role": "research analyst"}]
    )

    member = result.team.members[0]
    assert member.agent_id == "researcher@thread-1"
    assert member.status == AgentLifecycleStatus.WORKING
    assert member.current_run_id == "run-1"
    assert member.idle_since is None
    assert member.idle_expires_at is None
    assert stored_teams[0][0].members[0] == member
    assert [event["event_type"] for event in appended_events] == [
        EventType.TEAM_CREATED,
        EventType.AGENT_WAKE,
    ]
    assert appended_events[1]["payload"] == {
        "team_id": result.team.team_id,
        "account_id": "account-1",
        "agent_id": "researcher@thread-1",
        "agent_name": "researcher",
        "role": "research analyst",
        "status": "working",
        "wake_reason": "team_create_reuse",
        "previous_status": "idle",
        "previous_run_id": "previous-run",
        "idle_since": "2026-06-02T00:00:00+00:00",
        "idle_expires_at": "2026-06-02T00:20:00+00:00",
        "tool": "TeamCreate",
    }
    assert result.next_sequence == 12


@pytest.mark.asyncio
async def test_team_create_tool_does_not_reuse_expired_idle_member(
    monkeypatch,
) -> None:
    stored_teams = []
    appended_events = []
    existing_team = _team().model_copy(
        update={
            "members": [
                _team()
                .members[0]
                .model_copy(
                    update={
                        "agent_id": "persisted-researcher@thread-1",
                        "status": AgentLifecycleStatus.IDLE,
                        "idle_since": "2026-06-02T00:00:00+00:00",
                        "idle_expires_at": "2026-06-02T00:20:00+00:00",
                    }
                ),
            ]
        }
    )

    async def _read_team(**_kwargs):
        return existing_team

    async def _store_team(team, *, ttl_seconds, timeout=None):
        stored_teams.append((team, ttl_seconds, timeout))

    async def _append_event(**kwargs):
        appended_events.append(kwargs)
        return object()

    monkeypatch.setattr(agent_tools.team_store, "read_team", _read_team)
    monkeypatch.setattr(agent_tools.team_store, "store_team", _store_team)
    monkeypatch.setattr(agent_tools.event_log, "append_event", _append_event)

    tools = agent_tools.ShadowCloneV2AgentTools(
        context=_context(),
        clock=lambda: "2026-06-02T00:25:00+00:00",
    )

    result = await tools.team_create(
        members=[{"agent_name": "researcher", "role": "research analyst"}]
    )

    member = result.team.members[0]
    assert member.agent_id == "researcher@thread-1"
    assert member.status == AgentLifecycleStatus.STARTING
    assert stored_teams[0][0].members[0] == member
    assert [event["event_type"] for event in appended_events] == [
        EventType.TEAM_CREATED,
        EventType.AGENT_SHUTDOWN,
        EventType.AGENT_SPAWNED,
    ]
    assert appended_events[1]["payload"] == {
        "team_id": result.team.team_id,
        "account_id": "account-1",
        "agent_id": "persisted-researcher@thread-1",
        "agent_name": "researcher",
        "role": "research analyst",
        "status": "closed",
        "shutdown_reason": "idle_ttl_expired",
        "tool": "TeamCreate",
    }
    assert result.next_sequence == 13


@pytest.mark.asyncio
async def test_team_create_tool_rejects_active_existing_member_replacement(
    monkeypatch,
) -> None:
    stored_teams = []
    appended_events = []
    existing_team = _team().model_copy(
        update={
            "members": [
                _team()
                .members[0]
                .model_copy(
                    update={
                        "status": AgentLifecycleStatus.WORKING,
                        "current_run_id": "active-run",
                    }
                ),
            ]
        }
    )

    async def _read_team(**_kwargs):
        return existing_team

    async def _store_team(team, *, ttl_seconds, timeout=None):
        stored_teams.append((team, ttl_seconds, timeout))

    async def _append_event(**kwargs):
        appended_events.append(kwargs)
        return object()

    monkeypatch.setattr(agent_tools.team_store, "read_team", _read_team)
    monkeypatch.setattr(agent_tools.team_store, "store_team", _store_team)
    monkeypatch.setattr(agent_tools.event_log, "append_event", _append_event)

    tools = agent_tools.ShadowCloneV2AgentTools(
        context=_context(),
        clock=lambda: "2026-06-02T00:05:00+00:00",
    )

    with pytest.raises(ValueError, match="active existing teammate"):
        await tools.team_create(
            members=[{"agent_name": "researcher", "role": "research analyst"}]
        )

    assert stored_teams == []
    assert appended_events == []


@pytest.mark.asyncio
async def test_team_create_tool_rejects_omitted_active_existing_member(
    monkeypatch,
) -> None:
    stored_teams = []
    appended_events = []
    existing_team = _team().model_copy(
        update={
            "members": [
                _team()
                .members[0]
                .model_copy(
                    update={
                        "status": AgentLifecycleStatus.WORKING,
                        "current_run_id": "active-run",
                    }
                ),
                _team()
                .members[1]
                .model_copy(update={"status": AgentLifecycleStatus.IDLE}),
            ]
        }
    )

    async def _read_team(**_kwargs):
        return existing_team

    async def _store_team(team, *, ttl_seconds, timeout=None):
        stored_teams.append((team, ttl_seconds, timeout))

    async def _append_event(**kwargs):
        appended_events.append(kwargs)
        return object()

    monkeypatch.setattr(agent_tools.team_store, "read_team", _read_team)
    monkeypatch.setattr(agent_tools.team_store, "store_team", _store_team)
    monkeypatch.setattr(agent_tools.event_log, "append_event", _append_event)

    tools = agent_tools.ShadowCloneV2AgentTools(
        context=_context(),
        clock=lambda: "2026-06-02T00:05:00+00:00",
    )

    with pytest.raises(ValueError, match="active existing teammate"):
        await tools.team_create(
            members=[{"agent_name": "reviewer", "role": "quality reviewer"}]
        )

    assert stored_teams == []
    assert appended_events == []


@pytest.mark.asyncio
async def test_team_create_tool_preserves_omitted_unexpired_idle_member(
    monkeypatch,
) -> None:
    stored_teams = []
    appended_events = []
    existing_team = _team().model_copy(
        update={
            "members": [
                _team()
                .members[1]
                .model_copy(
                    update={
                        "status": AgentLifecycleStatus.IDLE,
                        "idle_since": "2026-06-02T00:00:00+00:00",
                        "idle_expires_at": "2026-06-02T00:20:00+00:00",
                    }
                ),
            ]
        }
    )

    async def _read_team(**_kwargs):
        return existing_team

    async def _store_team(team, *, ttl_seconds, timeout=None):
        stored_teams.append((team, ttl_seconds, timeout))

    async def _append_event(**kwargs):
        appended_events.append(kwargs)
        return object()

    monkeypatch.setattr(agent_tools.team_store, "read_team", _read_team)
    monkeypatch.setattr(agent_tools.team_store, "store_team", _store_team)
    monkeypatch.setattr(agent_tools.event_log, "append_event", _append_event)

    tools = agent_tools.ShadowCloneV2AgentTools(
        context=_context(),
        clock=lambda: "2026-06-02T00:05:00+00:00",
    )

    result = await tools.team_create(
        members=[{"agent_name": "researcher", "role": "research analyst"}]
    )

    assert [member.agent_name for member in result.team.members] == [
        "researcher",
        "reviewer",
    ]
    reviewer = next(
        member for member in result.team.members if member.agent_name == "reviewer"
    )
    assert reviewer.status == AgentLifecycleStatus.IDLE
    assert stored_teams[0][0] == result.team
    assert [event["event_type"] for event in appended_events] == [
        EventType.TEAM_CREATED,
        EventType.AGENT_SPAWNED,
    ]
    assert appended_events[1]["payload"]["agent_name"] == "researcher"


@pytest.mark.asyncio
async def test_team_create_tool_shutdowns_omitted_expired_idle_member(
    monkeypatch,
) -> None:
    stored_teams = []
    appended_events = []
    existing_team = _team().model_copy(
        update={
            "members": [
                _team()
                .members[1]
                .model_copy(
                    update={
                        "status": AgentLifecycleStatus.IDLE,
                        "idle_since": "2026-06-02T00:00:00+00:00",
                        "idle_expires_at": "2026-06-02T00:20:00+00:00",
                    }
                ),
            ]
        }
    )

    async def _read_team(**_kwargs):
        return existing_team

    async def _store_team(team, *, ttl_seconds, timeout=None):
        stored_teams.append((team, ttl_seconds, timeout))

    async def _append_event(**kwargs):
        appended_events.append(kwargs)
        return object()

    monkeypatch.setattr(agent_tools.team_store, "read_team", _read_team)
    monkeypatch.setattr(agent_tools.team_store, "store_team", _store_team)
    monkeypatch.setattr(agent_tools.event_log, "append_event", _append_event)

    tools = agent_tools.ShadowCloneV2AgentTools(
        context=_context(),
        clock=lambda: "2026-06-02T00:25:00+00:00",
    )

    result = await tools.team_create(
        members=[{"agent_name": "researcher", "role": "research analyst"}]
    )

    assert [member.agent_name for member in result.team.members] == ["researcher"]
    assert stored_teams[0][0] == result.team
    assert [event["event_type"] for event in appended_events] == [
        EventType.TEAM_CREATED,
        EventType.AGENT_SHUTDOWN,
        EventType.AGENT_SPAWNED,
    ]
    assert appended_events[0]["payload"]["account_id"] == "account-1"
    assert appended_events[1]["payload"]["account_id"] == "account-1"
    assert appended_events[1]["payload"]["agent_name"] == "reviewer"
    assert appended_events[1]["payload"]["shutdown_reason"] == "idle_ttl_expired"


@pytest.mark.asyncio
async def test_team_create_tool_retries_failed_wake_projection_before_persisting(
    monkeypatch,
) -> None:
    stored_teams = []
    appended_events = []
    phases: set[str] = set()
    wake_attempts = {"count": 0}
    existing_team = _team().model_copy(
        update={
            "members": [
                _team()
                .members[0]
                .model_copy(
                    update={
                        "status": AgentLifecycleStatus.IDLE,
                        "idle_since": "2026-06-02T00:00:00+00:00",
                        "idle_expires_at": "2026-06-02T00:20:00+00:00",
                        "current_run_id": "previous-run",
                    }
                ),
            ]
        }
    )

    async def _read_team(**_kwargs):
        return existing_team

    async def _store_team(team, *, ttl_seconds, timeout=None):
        stored_teams.append((team, ttl_seconds, timeout))

    async def _claim_team_create_phase(*, phase_key, **_kwargs):
        if phase_key in phases:
            return "done"
        return "claimed"

    async def _mark_team_create_phase_done(*, phase_key, **_kwargs):
        phases.add(phase_key)

    async def _release_team_create_phase(*, phase_key, **_kwargs):
        phases.discard(phase_key)

    async def _append_event(**kwargs):
        if kwargs["event_type"] == EventType.AGENT_WAKE:
            wake_attempts["count"] += 1
            if wake_attempts["count"] == 1:
                raise RuntimeError("wake append failed")
        appended_events.append(kwargs)
        return object()

    monkeypatch.setattr(agent_tools.team_store, "read_team", _read_team)
    monkeypatch.setattr(agent_tools.team_store, "store_team", _store_team)
    monkeypatch.setattr(
        agent_tools, "_claim_team_create_phase", _claim_team_create_phase, raising=False
    )
    monkeypatch.setattr(
        agent_tools,
        "_mark_team_create_phase_done",
        _mark_team_create_phase_done,
        raising=False,
    )
    monkeypatch.setattr(
        agent_tools,
        "_release_team_create_phase",
        _release_team_create_phase,
        raising=False,
    )
    monkeypatch.setattr(agent_tools.event_log, "append_event", _append_event)

    first_tools = agent_tools.ShadowCloneV2AgentTools(
        context=_context(),
        clock=lambda: "2026-06-02T00:05:00+00:00",
    )
    with pytest.raises(RuntimeError, match="wake append failed"):
        await first_tools.team_create(
            members=[{"agent_name": "researcher", "role": "research analyst"}]
        )

    assert stored_teams == []
    assert [event["event_type"] for event in appended_events] == [
        EventType.TEAM_CREATED
    ]

    second_tools = agent_tools.ShadowCloneV2AgentTools(
        context=_context(),
        clock=lambda: "2026-06-02T00:06:00+00:00",
    )
    result = await second_tools.team_create(
        members=[{"agent_name": "researcher", "role": "research analyst"}]
    )

    assert [event["event_type"] for event in appended_events] == [
        EventType.TEAM_CREATED,
        EventType.AGENT_WAKE,
    ]
    assert len(stored_teams) == 1
    assert result.team.members[0].status == AgentLifecycleStatus.WORKING


@pytest.mark.asyncio
async def test_team_create_tool_retries_wake_plan_after_idle_ttl_expires(
    monkeypatch,
) -> None:
    stored_teams = []
    appended_events = []
    phases: set[str] = set()
    session = {"payload": None}
    wake_attempts = {"count": 0}
    existing_team = _team().model_copy(
        update={
            "members": [
                _team()
                .members[0]
                .model_copy(
                    update={
                        "status": AgentLifecycleStatus.IDLE,
                        "idle_since": "2026-06-02T00:00:00+00:00",
                        "idle_expires_at": "2026-06-02T00:20:00+00:00",
                        "current_run_id": "previous-run",
                    }
                ),
            ]
        }
    )

    async def _read_team(**_kwargs):
        return existing_team

    async def _store_team(team, *, ttl_seconds, timeout=None):
        stored_teams.append((team, ttl_seconds, timeout))

    async def _claim_team_create_phase(*, phase_key, **_kwargs):
        if phase_key in phases:
            return "done"
        return "claimed"

    async def _mark_team_create_phase_done(*, phase_key, **_kwargs):
        phases.add(phase_key)

    async def _release_team_create_phase(*, phase_key, **_kwargs):
        phases.discard(phase_key)

    async def _claim_team_create_session(**kwargs):
        if session["payload"] is None:
            session["payload"] = kwargs["session_payload"]
            return kwargs["session_payload"]
        return session["payload"]

    async def _append_event(**kwargs):
        if kwargs["event_type"] == EventType.AGENT_WAKE:
            wake_attempts["count"] += 1
            if wake_attempts["count"] == 1:
                raise RuntimeError("wake append failed")
        appended_events.append(kwargs)
        return object()

    monkeypatch.setattr(agent_tools.team_store, "read_team", _read_team)
    monkeypatch.setattr(agent_tools.team_store, "store_team", _store_team)
    monkeypatch.setattr(
        agent_tools, "_claim_team_create_phase", _claim_team_create_phase, raising=False
    )
    monkeypatch.setattr(
        agent_tools,
        "_mark_team_create_phase_done",
        _mark_team_create_phase_done,
        raising=False,
    )
    monkeypatch.setattr(
        agent_tools,
        "_release_team_create_phase",
        _release_team_create_phase,
        raising=False,
    )
    monkeypatch.setattr(
        agent_tools,
        "_claim_team_create_session",
        _claim_team_create_session,
        raising=False,
    )
    monkeypatch.setattr(agent_tools.event_log, "append_event", _append_event)

    first_tools = agent_tools.ShadowCloneV2AgentTools(
        context=_context(),
        clock=lambda: "2026-06-02T00:05:00+00:00",
    )
    with pytest.raises(RuntimeError, match="wake append failed"):
        await first_tools.team_create(
            members=[{"agent_name": "researcher", "role": "research analyst"}]
        )

    second_tools = agent_tools.ShadowCloneV2AgentTools(
        context=_context(),
        clock=lambda: "2026-06-02T00:25:00+00:00",
    )
    result = await second_tools.team_create(
        members=[{"agent_name": "researcher", "role": "research analyst"}]
    )

    assert [event["event_type"] for event in appended_events] == [
        EventType.TEAM_CREATED,
        EventType.AGENT_WAKE,
    ]
    assert len(stored_teams) == 1
    assert result.team.members[0].status == AgentLifecycleStatus.WORKING
    assert result.team.members[0].agent_id == "researcher@thread-1"


@pytest.mark.asyncio
async def test_team_create_tool_retries_failed_expired_shutdown_before_persisting(
    monkeypatch,
) -> None:
    stored_teams = []
    appended_events = []
    phases: set[str] = set()
    shutdown_attempts = {"count": 0}
    existing_team = _team().model_copy(
        update={
            "members": [
                _team()
                .members[0]
                .model_copy(
                    update={
                        "agent_id": "persisted-researcher@thread-1",
                        "status": AgentLifecycleStatus.IDLE,
                        "idle_since": "2026-06-02T00:00:00+00:00",
                        "idle_expires_at": "2026-06-02T00:20:00+00:00",
                    }
                ),
            ]
        }
    )

    async def _read_team(**_kwargs):
        return existing_team

    async def _store_team(team, *, ttl_seconds, timeout=None):
        stored_teams.append((team, ttl_seconds, timeout))

    async def _claim_team_create_phase(*, phase_key, **_kwargs):
        if phase_key in phases:
            return "done"
        return "claimed"

    async def _mark_team_create_phase_done(*, phase_key, **_kwargs):
        phases.add(phase_key)

    async def _release_team_create_phase(*, phase_key, **_kwargs):
        phases.discard(phase_key)

    async def _append_event(**kwargs):
        if kwargs["event_type"] == EventType.AGENT_SHUTDOWN:
            shutdown_attempts["count"] += 1
            if shutdown_attempts["count"] == 1:
                raise RuntimeError("expired shutdown append failed")
        appended_events.append(kwargs)
        return object()

    monkeypatch.setattr(agent_tools.team_store, "read_team", _read_team)
    monkeypatch.setattr(agent_tools.team_store, "store_team", _store_team)
    monkeypatch.setattr(
        agent_tools, "_claim_team_create_phase", _claim_team_create_phase, raising=False
    )
    monkeypatch.setattr(
        agent_tools,
        "_mark_team_create_phase_done",
        _mark_team_create_phase_done,
        raising=False,
    )
    monkeypatch.setattr(
        agent_tools,
        "_release_team_create_phase",
        _release_team_create_phase,
        raising=False,
    )
    monkeypatch.setattr(agent_tools.event_log, "append_event", _append_event)

    first_tools = agent_tools.ShadowCloneV2AgentTools(
        context=_context(),
        clock=lambda: "2026-06-02T00:25:00+00:00",
    )
    with pytest.raises(RuntimeError, match="expired shutdown append failed"):
        await first_tools.team_create(
            members=[{"agent_name": "researcher", "role": "research analyst"}]
        )

    assert stored_teams == []
    assert [event["event_type"] for event in appended_events] == [
        EventType.TEAM_CREATED
    ]

    second_tools = agent_tools.ShadowCloneV2AgentTools(
        context=_context(),
        clock=lambda: "2026-06-02T00:26:00+00:00",
    )
    result = await second_tools.team_create(
        members=[{"agent_name": "researcher", "role": "research analyst"}]
    )

    assert [event["event_type"] for event in appended_events] == [
        EventType.TEAM_CREATED,
        EventType.AGENT_SHUTDOWN,
        EventType.AGENT_SPAWNED,
    ]
    assert len(stored_teams) == 1
    assert result.team.members[0].agent_id == "researcher@thread-1"


@pytest.mark.asyncio
async def test_team_create_tool_retries_failed_spawn_projection_before_persisting(
    monkeypatch,
) -> None:
    stored_teams = []
    appended_events = []
    phases: set[str] = set()
    spawn_attempts = {"count": 0}

    async def _read_team(**_kwargs):
        return None

    async def _store_team(team, *, ttl_seconds, timeout=None):
        stored_teams.append((team, ttl_seconds, timeout))

    async def _claim_team_create_phase(*, phase_key, **_kwargs):
        if phase_key in phases:
            return "done"
        return "claimed"

    async def _mark_team_create_phase_done(*, phase_key, **_kwargs):
        phases.add(phase_key)

    async def _release_team_create_phase(*, phase_key, **_kwargs):
        phases.discard(phase_key)

    async def _append_event(**kwargs):
        if kwargs["event_type"] == EventType.AGENT_SPAWNED:
            spawn_attempts["count"] += 1
            if spawn_attempts["count"] == 1:
                raise RuntimeError("spawn append failed")
        appended_events.append(kwargs)
        return object()

    monkeypatch.setattr(agent_tools.team_store, "read_team", _read_team)
    monkeypatch.setattr(agent_tools.team_store, "store_team", _store_team)
    monkeypatch.setattr(
        agent_tools, "_claim_team_create_phase", _claim_team_create_phase, raising=False
    )
    monkeypatch.setattr(
        agent_tools,
        "_mark_team_create_phase_done",
        _mark_team_create_phase_done,
        raising=False,
    )
    monkeypatch.setattr(
        agent_tools,
        "_release_team_create_phase",
        _release_team_create_phase,
        raising=False,
    )
    monkeypatch.setattr(agent_tools.event_log, "append_event", _append_event)

    first_tools = agent_tools.ShadowCloneV2AgentTools(
        context=_context(),
        clock=lambda: "2026-06-02T00:00:00+00:00",
    )
    with pytest.raises(RuntimeError, match="spawn append failed"):
        await first_tools.team_create(
            members=[{"agent_name": "researcher", "role": "research analyst"}]
        )

    assert stored_teams == []
    assert [event["event_type"] for event in appended_events] == [
        EventType.TEAM_CREATED
    ]

    second_tools = agent_tools.ShadowCloneV2AgentTools(
        context=_context(),
        clock=lambda: "2026-06-02T00:01:00+00:00",
    )
    result = await second_tools.team_create(
        members=[{"agent_name": "researcher", "role": "research analyst"}]
    )

    assert [event["event_type"] for event in appended_events] == [
        EventType.TEAM_CREATED,
        EventType.AGENT_SPAWNED,
    ]
    assert len(stored_teams) == 1
    assert result.team.members[0].agent_name == "researcher"


@pytest.mark.asyncio
async def test_team_create_tool_rejects_different_payload_after_completed_request(
    monkeypatch,
) -> None:
    stored_teams = []
    appended_events = []
    phases: set[str] = set()
    session = {"digest": ""}

    async def _read_team(**_kwargs):
        return None

    async def _store_team(team, *, ttl_seconds, timeout=None):
        stored_teams.append((team, ttl_seconds, timeout))

    async def _claim_team_create_phase(*, phase_key, **_kwargs):
        if phase_key in phases:
            return "done"
        return "claimed"

    async def _mark_team_create_phase_done(*, phase_key, **_kwargs):
        phases.add(phase_key)

    async def _release_team_create_phase(*, phase_key, **_kwargs):
        phases.discard(phase_key)

    async def _claim_team_create_session(**kwargs):
        request_digest = kwargs["request_digest"]
        if session["digest"] and session["digest"] != request_digest:
            raise ValueError(
                "different TeamCreate request already started for this run"
            )
        session["digest"] = request_digest
        return kwargs["session_payload"]

    async def _append_event(**kwargs):
        appended_events.append(kwargs)
        return object()

    monkeypatch.setattr(agent_tools.team_store, "read_team", _read_team)
    monkeypatch.setattr(agent_tools.team_store, "store_team", _store_team)
    monkeypatch.setattr(
        agent_tools, "_claim_team_create_phase", _claim_team_create_phase, raising=False
    )
    monkeypatch.setattr(
        agent_tools,
        "_mark_team_create_phase_done",
        _mark_team_create_phase_done,
        raising=False,
    )
    monkeypatch.setattr(
        agent_tools,
        "_release_team_create_phase",
        _release_team_create_phase,
        raising=False,
    )
    monkeypatch.setattr(
        agent_tools,
        "_claim_team_create_session",
        _claim_team_create_session,
        raising=False,
    )
    monkeypatch.setattr(agent_tools.event_log, "append_event", _append_event)

    tools = agent_tools.ShadowCloneV2AgentTools(
        context=_context(),
        clock=lambda: "2026-06-02T00:00:00+00:00",
    )
    await tools.team_create(
        members=[{"agent_name": "researcher", "role": "research analyst"}]
    )

    with pytest.raises(ValueError, match="different TeamCreate request"):
        await tools.team_create(
            members=[{"agent_name": "reviewer", "role": "quality reviewer"}]
        )

    assert len(stored_teams) == 1
    assert [event["event_type"] for event in appended_events] == [
        EventType.TEAM_CREATED,
        EventType.AGENT_SPAWNED,
    ]


@pytest.mark.asyncio
async def test_team_create_tool_rejects_different_payload_after_partial_failure(
    monkeypatch,
) -> None:
    stored_teams = []
    appended_events = []
    phases: set[str] = set()
    session = {"digest": ""}
    spawn_attempts = {"count": 0}

    async def _read_team(**_kwargs):
        return None

    async def _store_team(team, *, ttl_seconds, timeout=None):
        stored_teams.append((team, ttl_seconds, timeout))

    async def _claim_team_create_phase(*, phase_key, **_kwargs):
        if phase_key in phases:
            return "done"
        return "claimed"

    async def _mark_team_create_phase_done(*, phase_key, **_kwargs):
        phases.add(phase_key)

    async def _release_team_create_phase(*, phase_key, **_kwargs):
        phases.discard(phase_key)

    async def _claim_team_create_session(**kwargs):
        request_digest = kwargs["request_digest"]
        if session["digest"] and session["digest"] != request_digest:
            raise ValueError(
                "different TeamCreate request already started for this run"
            )
        session["digest"] = request_digest
        return kwargs["session_payload"]

    async def _append_event(**kwargs):
        if kwargs["event_type"] == EventType.AGENT_SPAWNED:
            spawn_attempts["count"] += 1
            if spawn_attempts["count"] == 1:
                raise RuntimeError("spawn append failed")
        appended_events.append(kwargs)
        return object()

    monkeypatch.setattr(agent_tools.team_store, "read_team", _read_team)
    monkeypatch.setattr(agent_tools.team_store, "store_team", _store_team)
    monkeypatch.setattr(
        agent_tools, "_claim_team_create_phase", _claim_team_create_phase, raising=False
    )
    monkeypatch.setattr(
        agent_tools,
        "_mark_team_create_phase_done",
        _mark_team_create_phase_done,
        raising=False,
    )
    monkeypatch.setattr(
        agent_tools,
        "_release_team_create_phase",
        _release_team_create_phase,
        raising=False,
    )
    monkeypatch.setattr(
        agent_tools,
        "_claim_team_create_session",
        _claim_team_create_session,
        raising=False,
    )
    monkeypatch.setattr(agent_tools.event_log, "append_event", _append_event)

    first_tools = agent_tools.ShadowCloneV2AgentTools(
        context=_context(),
        clock=lambda: "2026-06-02T00:00:00+00:00",
    )
    with pytest.raises(RuntimeError, match="spawn append failed"):
        await first_tools.team_create(
            members=[{"agent_name": "researcher", "role": "research analyst"}]
        )

    second_tools = agent_tools.ShadowCloneV2AgentTools(
        context=_context(),
        clock=lambda: "2026-06-02T00:01:00+00:00",
    )
    with pytest.raises(ValueError, match="different TeamCreate request"):
        await second_tools.team_create(
            members=[{"agent_name": "reviewer", "role": "quality reviewer"}]
        )

    assert stored_teams == []
    assert [event["event_type"] for event in appended_events] == [
        EventType.TEAM_CREATED
    ]


@pytest.mark.asyncio
async def test_team_create_tool_rejects_duplicate_member_names_before_persisting(
    monkeypatch,
) -> None:
    stored_teams = []

    async def _read_team(**_kwargs):
        return None

    async def _store_team(team, *, ttl_seconds, timeout=None):
        stored_teams.append(team)

    monkeypatch.setattr(agent_tools.team_store, "read_team", _read_team)
    monkeypatch.setattr(agent_tools.team_store, "store_team", _store_team)

    tools = agent_tools.ShadowCloneV2AgentTools(context=_context())

    with pytest.raises(ValueError, match="duplicate agent_name"):
        await tools.team_create(
            members=[
                {"agent_name": "reviewer", "role": "quality reviewer"},
                {"agent_name": "reviewer", "role": "second reviewer"},
            ]
        )

    assert stored_teams == []


@pytest.mark.asyncio
async def test_team_create_tool_rejects_more_than_max_subagents_before_persisting(
    monkeypatch,
) -> None:
    stored_teams = []

    async def _read_team(**_kwargs):
        return None

    async def _store_team(team, *, ttl_seconds, timeout=None):
        stored_teams.append(team)

    monkeypatch.setattr(agent_tools.team_store, "read_team", _read_team)
    monkeypatch.setattr(agent_tools.team_store, "store_team", _store_team)

    tools = agent_tools.ShadowCloneV2AgentTools(context=_context())

    with pytest.raises(ValueError, match="max subagents"):
        await tools.team_create(
            members=[
                {"agent_name": f"agent-{index}", "role": f"role {index}"}
                for index in range(11)
            ]
        )

    assert stored_teams == []


@pytest.mark.asyncio
async def test_team_shutdown_tool_closes_idle_team_and_appends_events(
    monkeypatch,
) -> None:
    stored_teams = []
    appended_events = []
    team = _team().model_copy(
        update={
            "members": [
                member.model_copy(
                    update={
                        "status": AgentLifecycleStatus.IDLE,
                        "idle_since": "2026-06-02T00:00:00+00:00",
                        "idle_expires_at": "2026-06-02T00:20:00+00:00",
                    }
                )
                for member in _team().members
            ]
        }
    )

    async def _read_team(**_kwargs):
        return team

    async def _list_tasks(**_kwargs):
        return [
            agent_tools.Task(
                id="task-1",
                run_id="run-1",
                thread_id="thread-1",
                subject="Done",
                description="Already complete.",
                status=TaskStatus.COMPLETED,
                created_at="2026-06-02T00:00:00+00:00",
                updated_at="2026-06-02T00:01:00+00:00",
            )
        ]

    async def _store_team(team, *, ttl_seconds, timeout=None):
        stored_teams.append((team, ttl_seconds, timeout))

    async def _append_event(**kwargs):
        appended_events.append(kwargs)
        return object()

    monkeypatch.setattr(agent_tools.team_store, "read_team", _read_team)
    monkeypatch.setattr(agent_tools.team_store, "store_team", _store_team)
    monkeypatch.setattr(agent_tools.task_pool, "list_tasks", _list_tasks)
    monkeypatch.setattr(agent_tools.event_log, "append_event", _append_event)

    tools = agent_tools.ShadowCloneV2AgentTools(
        context=_context(actor_name="team-lead"),
        clock=lambda: "2026-06-02T00:05:00+00:00",
    )

    result = await tools.team_shutdown(reason="work_complete")

    assert [member.status for member in result.team.members] == [
        AgentLifecycleStatus.CLOSED,
        AgentLifecycleStatus.CLOSED,
    ]
    assert result.team.metadata["shutdown_reason"] == "work_complete"
    assert stored_teams[0][0] == result.team
    assert [event["event_type"] for event in appended_events] == [
        EventType.AGENT_SHUTDOWN,
        EventType.AGENT_SHUTDOWN,
        EventType.TEAM_DELETED,
    ]
    assert appended_events[0]["payload"]["agent_name"] == "researcher"
    assert appended_events[0]["payload"]["shutdown_reason"] == "work_complete"
    assert appended_events[-1]["payload"] == {
        "team_id": team.team_id,
        "actor": "team-lead",
        "shutdown_reason": "work_complete",
        "closed_agent_names": ["researcher", "reviewer"],
    }
    assert result.next_sequence == 13


@pytest.mark.asyncio
async def test_team_shutdown_tool_rejects_non_facilitator_without_events(
    monkeypatch,
) -> None:
    appended_events = []

    async def _read_team(**_kwargs):
        return _team()

    async def _append_event(**kwargs):
        appended_events.append(kwargs)
        return object()

    monkeypatch.setattr(agent_tools.team_store, "read_team", _read_team)
    monkeypatch.setattr(agent_tools.event_log, "append_event", _append_event)

    tools = agent_tools.ShadowCloneV2AgentTools(
        context=_context(actor_name="researcher")
    )

    with pytest.raises(ValueError, match="only the team facilitator"):
        await tools.team_shutdown(reason="not_authorized")

    assert appended_events == []


@pytest.mark.asyncio
async def test_team_shutdown_tool_rejects_active_member_without_events(
    monkeypatch,
) -> None:
    appended_events = []
    team = _team().model_copy(
        update={
            "members": [
                _team()
                .members[0]
                .model_copy(update={"status": AgentLifecycleStatus.WORKING}),
                _team()
                .members[1]
                .model_copy(update={"status": AgentLifecycleStatus.IDLE}),
            ]
        }
    )

    async def _read_team(**_kwargs):
        return team

    async def _append_event(**kwargs):
        appended_events.append(kwargs)
        return object()

    monkeypatch.setattr(agent_tools.team_store, "read_team", _read_team)
    monkeypatch.setattr(agent_tools.event_log, "append_event", _append_event)

    tools = agent_tools.ShadowCloneV2AgentTools(context=_context())

    with pytest.raises(ValueError, match="active teammates must be cancelled"):
        await tools.team_shutdown(reason="work_complete")

    assert appended_events == []


@pytest.mark.asyncio
async def test_team_shutdown_tool_rejects_open_tasks_without_events(
    monkeypatch,
) -> None:
    appended_events = []
    team = _team().model_copy(
        update={
            "members": [
                member.model_copy(update={"status": AgentLifecycleStatus.IDLE})
                for member in _team().members
            ]
        }
    )

    async def _read_team(**_kwargs):
        return team

    async def _list_tasks(**_kwargs):
        return [
            agent_tools.Task(
                id="task-open",
                run_id="run-1",
                thread_id="thread-1",
                subject="Still open",
                description="Cannot shut down yet.",
                status=TaskStatus.PENDING,
                created_at="2026-06-02T00:00:00+00:00",
                updated_at="2026-06-02T00:00:00+00:00",
            )
        ]

    async def _append_event(**kwargs):
        appended_events.append(kwargs)
        return object()

    monkeypatch.setattr(agent_tools.team_store, "read_team", _read_team)
    monkeypatch.setattr(agent_tools.task_pool, "list_tasks", _list_tasks)
    monkeypatch.setattr(agent_tools.event_log, "append_event", _append_event)

    tools = agent_tools.ShadowCloneV2AgentTools(context=_context())

    with pytest.raises(ValueError, match="open tasks must be completed"):
        await tools.team_shutdown(reason="work_complete")

    assert appended_events == []


@pytest.mark.asyncio
async def test_team_shutdown_tool_retries_partial_event_failure_before_persisting(
    monkeypatch,
) -> None:
    stored_teams = []
    appended_events = []
    phases: set[str] = set()
    event_attempts = {"agent_shutdown_reviewer": 0}
    team = _team().model_copy(
        update={
            "members": [
                member.model_copy(update={"status": AgentLifecycleStatus.IDLE})
                for member in _team().members
            ]
        }
    )

    async def _read_team(**_kwargs):
        return team

    async def _list_tasks(**_kwargs):
        return [
            agent_tools.Task(
                id="task-1",
                run_id="run-1",
                thread_id="thread-1",
                subject="Done",
                description="Done.",
                status=TaskStatus.COMPLETED,
                created_at="2026-06-02T00:00:00+00:00",
                updated_at="2026-06-02T00:01:00+00:00",
            )
        ]

    async def _store_team(team, *, ttl_seconds, timeout=None):
        stored_teams.append(team)

    async def _claim_shutdown_phase(*, phase_key, **_kwargs):
        if phase_key in phases:
            return False
        return True

    async def _mark_shutdown_phase_done(*, phase_key, **_kwargs):
        phases.add(phase_key)

    async def _release_shutdown_phase(*, phase_key, **_kwargs):
        phases.discard(phase_key)

    async def _append_event(**kwargs):
        if (
            kwargs["event_type"] == EventType.AGENT_SHUTDOWN
            and kwargs["payload"]["agent_name"] == "reviewer"
        ):
            event_attempts["agent_shutdown_reviewer"] += 1
            if event_attempts["agent_shutdown_reviewer"] == 1:
                raise RuntimeError("reviewer shutdown event failed")
        appended_events.append(kwargs)
        return object()

    monkeypatch.setattr(agent_tools.team_store, "read_team", _read_team)
    monkeypatch.setattr(agent_tools.team_store, "store_team", _store_team)
    monkeypatch.setattr(agent_tools.task_pool, "list_tasks", _list_tasks)
    monkeypatch.setattr(
        agent_tools, "_claim_shutdown_phase", _claim_shutdown_phase, raising=False
    )
    monkeypatch.setattr(
        agent_tools,
        "_mark_shutdown_phase_done",
        _mark_shutdown_phase_done,
        raising=False,
    )
    monkeypatch.setattr(
        agent_tools,
        "_release_shutdown_phase",
        _release_shutdown_phase,
        raising=False,
    )
    monkeypatch.setattr(agent_tools.event_log, "append_event", _append_event)

    first_tools = agent_tools.ShadowCloneV2AgentTools(
        context=_context(actor_name="team-lead"),
        clock=lambda: "2026-06-02T00:05:00+00:00",
    )
    with pytest.raises(RuntimeError, match="reviewer shutdown event failed"):
        await first_tools.team_shutdown(reason="work_complete")

    assert stored_teams == []
    assert [event["payload"]["agent_name"] for event in appended_events] == [
        "researcher"
    ]

    second_tools = agent_tools.ShadowCloneV2AgentTools(
        context=_context(actor_name="team-lead"),
        clock=lambda: "2026-06-02T00:06:00+00:00",
    )
    result = await second_tools.team_shutdown(reason="work_complete")

    assert [event["event_type"] for event in appended_events] == [
        EventType.AGENT_SHUTDOWN,
        EventType.AGENT_SHUTDOWN,
        EventType.TEAM_DELETED,
    ]
    assert [event["payload"].get("agent_name") for event in appended_events] == [
        "researcher",
        "reviewer",
        None,
    ]
    assert len(stored_teams) == 1
    assert all(
        member.status == AgentLifecycleStatus.CLOSED for member in result.team.members
    )


@pytest.mark.asyncio
async def test_team_shutdown_tool_rejects_unresolved_projecting_phase(
    monkeypatch,
) -> None:
    stored_teams = []
    appended_events = []
    team = _team().model_copy(
        update={
            "members": [
                member.model_copy(update={"status": AgentLifecycleStatus.IDLE})
                for member in _team().members
            ]
        }
    )

    async def _read_team(**_kwargs):
        return team

    async def _list_tasks(**_kwargs):
        return [
            agent_tools.Task(
                id="task-1",
                run_id="run-1",
                thread_id="thread-1",
                subject="Done",
                description="Done.",
                status=TaskStatus.COMPLETED,
                created_at="2026-06-02T00:00:00+00:00",
                updated_at="2026-06-02T00:01:00+00:00",
            )
        ]

    async def _store_team(team, *, ttl_seconds, timeout=None):
        stored_teams.append(team)

    async def _claim_shutdown_phase(*, phase_key, **_kwargs):
        if phase_key.endswith("agent-reviewer"):
            return "in_progress"
        return "claimed"

    async def _mark_shutdown_phase_done(*, phase_key, **_kwargs):
        return None

    async def _release_shutdown_phase(*, phase_key, **_kwargs):
        return None

    async def _append_event(**kwargs):
        appended_events.append(kwargs)
        return object()

    monkeypatch.setattr(agent_tools.team_store, "read_team", _read_team)
    monkeypatch.setattr(agent_tools.team_store, "store_team", _store_team)
    monkeypatch.setattr(agent_tools.task_pool, "list_tasks", _list_tasks)
    monkeypatch.setattr(
        agent_tools, "_claim_shutdown_phase", _claim_shutdown_phase, raising=False
    )
    monkeypatch.setattr(
        agent_tools,
        "_mark_shutdown_phase_done",
        _mark_shutdown_phase_done,
        raising=False,
    )
    monkeypatch.setattr(
        agent_tools,
        "_release_shutdown_phase",
        _release_shutdown_phase,
        raising=False,
    )
    monkeypatch.setattr(agent_tools.event_log, "append_event", _append_event)

    tools = agent_tools.ShadowCloneV2AgentTools(
        context=_context(actor_name="team-lead"),
        clock=lambda: "2026-06-02T00:05:00+00:00",
    )

    with pytest.raises(RuntimeError, match="shutdown phase is already in progress"):
        await tools.team_shutdown(reason="work_complete")

    assert stored_teams == []
    assert EventType.TEAM_DELETED not in [
        event["event_type"] for event in appended_events
    ]


@pytest.mark.asyncio
async def test_team_shutdown_tool_rejects_different_reason_after_completed_shutdown(
    monkeypatch,
) -> None:
    stored_teams = []
    appended_events = []
    phases: set[str] = set()
    current_team = {
        "value": _team().model_copy(
            update={
                "members": [
                    member.model_copy(update={"status": AgentLifecycleStatus.IDLE})
                    for member in _team().members
                ]
            }
        )
    }
    session = {"reason": None}

    async def _read_team(**_kwargs):
        return current_team["value"]

    async def _list_tasks(**_kwargs):
        return [
            agent_tools.Task(
                id="task-1",
                run_id="run-1",
                thread_id="thread-1",
                subject="Done",
                description="Done.",
                status=TaskStatus.COMPLETED,
                created_at="2026-06-02T00:00:00+00:00",
                updated_at="2026-06-02T00:01:00+00:00",
            )
        ]

    async def _store_team(team, *, ttl_seconds, timeout=None):
        stored_teams.append(team)
        current_team["value"] = team

    async def _claim_shutdown_session(*, reason, **_kwargs):
        existing = session["reason"]
        if existing is not None and existing != reason:
            raise ValueError("shutdown already started with a different reason")
        session["reason"] = reason
        return reason

    async def _claim_shutdown_phase(*, phase_key, **_kwargs):
        if phase_key in phases:
            return "done"
        return "claimed"

    async def _mark_shutdown_phase_done(*, phase_key, **_kwargs):
        phases.add(phase_key)

    async def _release_shutdown_phase(*, phase_key, **_kwargs):
        phases.discard(phase_key)

    async def _append_event(**kwargs):
        appended_events.append(kwargs)
        return object()

    monkeypatch.setattr(agent_tools.team_store, "read_team", _read_team)
    monkeypatch.setattr(agent_tools.team_store, "store_team", _store_team)
    monkeypatch.setattr(agent_tools.task_pool, "list_tasks", _list_tasks)
    monkeypatch.setattr(
        agent_tools, "_claim_shutdown_session", _claim_shutdown_session, raising=False
    )
    monkeypatch.setattr(
        agent_tools, "_claim_shutdown_phase", _claim_shutdown_phase, raising=False
    )
    monkeypatch.setattr(
        agent_tools,
        "_mark_shutdown_phase_done",
        _mark_shutdown_phase_done,
        raising=False,
    )
    monkeypatch.setattr(
        agent_tools,
        "_release_shutdown_phase",
        _release_shutdown_phase,
        raising=False,
    )
    monkeypatch.setattr(agent_tools.event_log, "append_event", _append_event)

    first_tools = agent_tools.ShadowCloneV2AgentTools(
        context=_context(actor_name="team-lead"),
        clock=lambda: "2026-06-02T00:05:00+00:00",
    )
    await first_tools.team_shutdown(reason="work_complete")

    second_tools = agent_tools.ShadowCloneV2AgentTools(
        context=_context(actor_name="team-lead"),
        clock=lambda: "2026-06-02T00:06:00+00:00",
    )
    with pytest.raises(ValueError, match="different reason"):
        await second_tools.team_shutdown(reason="manual_cleanup")

    assert current_team["value"].metadata["shutdown_reason"] == "work_complete"
    assert len(stored_teams) == 1
    assert [event["event_type"] for event in appended_events].count(
        EventType.TEAM_DELETED
    ) == 1


@pytest.mark.asyncio
async def test_team_shutdown_tool_rejects_partial_retry_with_different_reason(
    monkeypatch,
) -> None:
    stored_teams = []
    appended_events = []
    phases: set[str] = set()
    event_attempts = {"reviewer": 0}
    session = {"reason": None}
    team = _team().model_copy(
        update={
            "members": [
                member.model_copy(update={"status": AgentLifecycleStatus.IDLE})
                for member in _team().members
            ]
        }
    )

    async def _read_team(**_kwargs):
        return team

    async def _list_tasks(**_kwargs):
        return [
            agent_tools.Task(
                id="task-1",
                run_id="run-1",
                thread_id="thread-1",
                subject="Done",
                description="Done.",
                status=TaskStatus.COMPLETED,
                created_at="2026-06-02T00:00:00+00:00",
                updated_at="2026-06-02T00:01:00+00:00",
            )
        ]

    async def _store_team(team, *, ttl_seconds, timeout=None):
        stored_teams.append(team)

    async def _claim_shutdown_session(*, reason, **_kwargs):
        existing = session["reason"]
        if existing is not None and existing != reason:
            raise ValueError("shutdown already started with a different reason")
        session["reason"] = reason
        return reason

    async def _claim_shutdown_phase(*, phase_key, **_kwargs):
        if phase_key in phases:
            return "done"
        return "claimed"

    async def _mark_shutdown_phase_done(*, phase_key, **_kwargs):
        phases.add(phase_key)

    async def _release_shutdown_phase(*, phase_key, **_kwargs):
        phases.discard(phase_key)

    async def _append_event(**kwargs):
        if (
            kwargs["event_type"] == EventType.AGENT_SHUTDOWN
            and kwargs["payload"]["agent_name"] == "reviewer"
        ):
            event_attempts["reviewer"] += 1
            if event_attempts["reviewer"] == 1:
                raise RuntimeError("reviewer shutdown event failed")
        appended_events.append(kwargs)
        return object()

    monkeypatch.setattr(agent_tools.team_store, "read_team", _read_team)
    monkeypatch.setattr(agent_tools.team_store, "store_team", _store_team)
    monkeypatch.setattr(agent_tools.task_pool, "list_tasks", _list_tasks)
    monkeypatch.setattr(
        agent_tools, "_claim_shutdown_session", _claim_shutdown_session, raising=False
    )
    monkeypatch.setattr(
        agent_tools, "_claim_shutdown_phase", _claim_shutdown_phase, raising=False
    )
    monkeypatch.setattr(
        agent_tools,
        "_mark_shutdown_phase_done",
        _mark_shutdown_phase_done,
        raising=False,
    )
    monkeypatch.setattr(
        agent_tools,
        "_release_shutdown_phase",
        _release_shutdown_phase,
        raising=False,
    )
    monkeypatch.setattr(agent_tools.event_log, "append_event", _append_event)

    first_tools = agent_tools.ShadowCloneV2AgentTools(
        context=_context(actor_name="team-lead"),
        clock=lambda: "2026-06-02T00:05:00+00:00",
    )
    with pytest.raises(RuntimeError, match="reviewer shutdown event failed"):
        await first_tools.team_shutdown(reason="work_complete")

    second_tools = agent_tools.ShadowCloneV2AgentTools(
        context=_context(actor_name="team-lead"),
        clock=lambda: "2026-06-02T00:06:00+00:00",
    )
    with pytest.raises(ValueError, match="different reason"):
        await second_tools.team_shutdown(reason="manual_cleanup")

    assert stored_teams == []
    assert all(
        event["payload"].get("shutdown_reason") == "work_complete"
        for event in appended_events
    )


@pytest.mark.asyncio
async def test_task_create_tool_persists_living_task_document_and_event(
    monkeypatch,
) -> None:
    stored_tasks = []
    appended_events = []

    async def _create_task(task, *, ttl_seconds, timeout=None):
        stored_tasks.append((task, ttl_seconds, timeout))
        return True

    async def _append_event(**kwargs):
        appended_events.append(kwargs)
        return object()

    async def _read_team(**_kwargs):
        return _team()

    monkeypatch.setattr(agent_tools.team_store, "read_team", _read_team)
    monkeypatch.setattr(agent_tools.task_pool, "create_task", _create_task)
    monkeypatch.setattr(agent_tools.event_log, "append_event", _append_event)

    tools = agent_tools.ShadowCloneV2AgentTools(
        context=_context(actor_name="researcher"),
        clock=lambda: "2026-06-02T00:01:00+00:00",
    )

    result = await tools.task_create(
        task_id="task-1",
        subject="Research competitors",
        description="Find three competitor examples and risks.",
        metadata={"reason": "discovered_during_run"},
    )

    task = result.task
    assert task.status == TaskStatus.PENDING
    assert task.created_by == "researcher"
    assert task.blocked_by == []
    assert task.metadata == {"reason": "discovered_during_run"}
    assert stored_tasks[0][0] == task
    assert stored_tasks[0][1] == 3600
    assert appended_events[0]["event_type"] == EventType.TASK_CREATED
    assert appended_events[0]["payload"]["task_id"] == "task-1"
    assert appended_events[0]["payload"]["description"] == (
        "Find three competitor examples and risks."
    )
    assert appended_events[0]["payload"]["metadata"] == {
        "reason": "discovered_during_run"
    }
    assert appended_events[0]["payload"]["task"]["description"] == (
        "Find three competitor examples and risks."
    )
    assert appended_events[0]["payload"]["task"]["metadata"] == {
        "reason": "discovered_during_run"
    }
    assert appended_events[0]["payload"]["task"]["plan_revision"] == 1
    assert result.next_sequence == 11


@pytest.mark.asyncio
async def test_task_create_tool_removes_prior_delivery_markers_not_in_current_user_request(
    monkeypatch,
) -> None:
    stored_tasks = []
    appended_events = []

    async def _create_task(task, *, ttl_seconds, timeout=None):
        stored_tasks.append(task)
        return True

    async def _append_event(**kwargs):
        appended_events.append(kwargs)
        return object()

    async def _read_team(**_kwargs):
        return _team()

    monkeypatch.setattr(agent_tools.team_store, "read_team", _read_team)
    monkeypatch.setattr(agent_tools.task_pool, "create_task", _create_task)
    monkeypatch.setattr(agent_tools.event_log, "append_event", _append_event)

    tools = agent_tools.ShadowCloneV2AgentTools(
        context=_context_with_user_message(
            actor_name="team-lead",
            current_user_message=(
                "Apply feedback IDLE_FEEDBACK_REQUEST_123 and require "
                "WAKE_REUSED_TEAMMATE_OK plus IDLE_FEEDBACK_APPLIED_123. "
                "Peer marker P2P_B_CONSUMED_A_MESSAGE_123 must be produced by "
                "the worker but not copied into its task description."
            ),
        ),
        clock=lambda: "2026-06-02T00:01:00+00:00",
    )

    result = await tools.task_create(
        task_id="follow-up",
        subject="Apply feedback",
        description=(
            "Use /workspace/live-click-subtask.md from prior work containing "
            "IDLE_DRAFT_V1_123. Return WAKE_REUSED_TEAMMATE_OK, "
            "IDLE_FEEDBACK_REQUEST_123, IDLE_FEEDBACK_APPLIED_123, and "
            "P2P_B_CONSUMED_A_MESSAGE_123."
        ),
        metadata={"agent_name": "researcher"},
    )

    assert "IDLE_DRAFT_V1_123" not in result.task.description
    assert "P2P_B_CONSUMED_A_MESSAGE_123" not in result.task.description
    assert "P2P_B_CONSUMED_A_MESSAGE_<unique>" in result.task.description
    assert "IDLE_DRAFT_V1_123" not in appended_events[0]["payload"]["description"]
    assert "WAKE_REUSED_TEAMMATE_OK" in result.task.description
    assert "IDLE_FEEDBACK_REQUEST_123" in result.task.description
    assert "IDLE_FEEDBACK_APPLIED_123" in result.task.description
    assert stored_tasks[0].description == result.task.description




@pytest.mark.asyncio
async def test_task_create_tool_strips_prior_delivery_marker_even_when_user_mentions_it_as_previous_work(
    monkeypatch,
) -> None:
    stored_tasks = []
    appended_events = []

    async def _create_task(task, *, ttl_seconds, timeout=None):
        stored_tasks.append(task)
        return True

    async def _append_event(**kwargs):
        appended_events.append(kwargs)
        return object()

    async def _read_team(**_kwargs):
        return _team()

    monkeypatch.setattr(agent_tools.team_store, "read_team", _read_team)
    monkeypatch.setattr(agent_tools.task_pool, "create_task", _create_task)
    monkeypatch.setattr(agent_tools.event_log, "append_event", _append_event)

    tools = agent_tools.ShadowCloneV2AgentTools(
        context=_context_with_user_message(
            actor_name="team-lead",
            current_user_message=(
                "Reuse teammate-1. The previous delivery had marker "
                "IDLE_DRAFT_V1_12345, but do not copy that prior marker into "
                "the new task description. The new result must contain "
                "WAKE_REUSED_TEAMMATE_OK, IDLE_FEEDBACK_REQUEST_12345, and "
                "IDLE_FEEDBACK_APPLIED_12345."
            ),
        ),
        clock=lambda: "2026-06-02T00:01:00+00:00",
    )

    result = await tools.task_create(
        task_id="follow-up-prior-marker-mentioned",
        subject="Apply idle feedback",
        description=(
            "Use prior artifact /workspace/live-click-subtask.md containing "
            "IDLE_DRAFT_V1_12345. Apply IDLE_FEEDBACK_REQUEST_12345 and return "
            "WAKE_REUSED_TEAMMATE_OK plus IDLE_FEEDBACK_APPLIED_12345."
        ),
        metadata={"agent_name": "researcher"},
    )

    assert "IDLE_DRAFT_V1_12345" not in result.task.description
    assert "IDLE_DRAFT_V1_12345" not in appended_events[0]["payload"]["description"]
    assert "WAKE_REUSED_TEAMMATE_OK" in result.task.description
    assert "IDLE_FEEDBACK_REQUEST_12345" in result.task.description
    assert "IDLE_FEEDBACK_APPLIED_12345" in result.task.description
    assert stored_tasks[0].description == result.task.description




@pytest.mark.asyncio
async def test_task_create_tool_keeps_newly_requested_prior_prefix_marker_in_follow_up(
    monkeypatch,
) -> None:
    stored_tasks = []
    appended_events = []

    async def _create_task(task, *, ttl_seconds, timeout=None):
        stored_tasks.append(task)
        return True

    async def _append_event(**kwargs):
        appended_events.append(kwargs)
        return object()

    async def _read_team(**_kwargs):
        return _team()

    monkeypatch.setattr(agent_tools.team_store, "read_team", _read_team)
    monkeypatch.setattr(agent_tools.task_pool, "create_task", _create_task)
    monkeypatch.setattr(agent_tools.event_log, "append_event", _append_event)

    tools = agent_tools.ShadowCloneV2AgentTools(
        context=_context_with_user_message(
            actor_name="team-lead",
            current_user_message=(
                "Reuse teammate-1. The previous delivery had marker "
                "IDLE_DRAFT_V1_12345. The new result must contain "
                "IDLE_DRAFT_V2_12345, WAKE_REUSED_TEAMMATE_OK, and "
                "IDLE_FEEDBACK_APPLIED_12345."
            ),
        ),
        clock=lambda: "2026-06-02T00:01:00+00:00",
    )

    result = await tools.task_create(
        task_id="follow-up-new-prior-prefix-marker",
        subject="Apply idle feedback",
        description=(
            "Use prior artifact /workspace/live-click-subtask.md containing "
            "IDLE_DRAFT_V1_12345. Return IDLE_DRAFT_V2_12345, "
            "WAKE_REUSED_TEAMMATE_OK, and IDLE_FEEDBACK_APPLIED_12345."
        ),
        metadata={"agent_name": "researcher"},
    )

    assert "IDLE_DRAFT_V1_12345" not in result.task.description
    assert "IDLE_DRAFT_V2_12345" in result.task.description
    assert "WAKE_REUSED_TEAMMATE_OK" in result.task.description
    assert "IDLE_FEEDBACK_APPLIED_12345" in result.task.description
    assert stored_tasks[0].description == result.task.description
    assert appended_events[0]["payload"]["description"] == result.task.description




@pytest.mark.asyncio
async def test_task_create_tool_strips_chinese_prior_marker_when_new_marker_is_requested_without_space(
    monkeypatch,
) -> None:
    stored_tasks = []
    appended_events = []

    async def _create_task(task, *, ttl_seconds, timeout=None):
        stored_tasks.append(task)
        return True

    async def _append_event(**kwargs):
        appended_events.append(kwargs)
        return object()

    async def _read_team(**_kwargs):
        return _team()

    monkeypatch.setattr(agent_tools.team_store, "read_team", _read_team)
    monkeypatch.setattr(agent_tools.task_pool, "create_task", _create_task)
    monkeypatch.setattr(agent_tools.event_log, "append_event", _append_event)

    tools = agent_tools.ShadowCloneV2AgentTools(
        context=_context_with_user_message(
            actor_name="team-lead",
            current_user_message=(
                "上一轮交付标记是 IDLE_DRAFT_V1_12345。"
                "子任务结果必须包含 IDLE_DRAFT_V2_12345、WAKE_REUSED_TEAMMATE_OK。"
            ),
        ),
        clock=lambda: "2026-06-02T00:01:00+00:00",
    )

    result = await tools.task_create(
        task_id="follow-up-chinese-no-space",
        subject="Apply idle feedback",
        description=(
            "引用上一轮 artifact /workspace/live-click-subtask.md，旧标记 "
            "IDLE_DRAFT_V1_12345。输出 IDLE_DRAFT_V2_12345 和 "
            "WAKE_REUSED_TEAMMATE_OK。"
        ),
        metadata={"agent_name": "researcher"},
    )

    assert "IDLE_DRAFT_V1_12345" not in result.task.description
    assert "IDLE_DRAFT_V2_12345" in result.task.description
    assert "WAKE_REUSED_TEAMMATE_OK" in result.task.description
    assert appended_events[0]["payload"]["description"] == result.task.description
    assert stored_tasks[0].description == result.task.description


@pytest.mark.asyncio
async def test_task_create_tool_placeholderizes_peer_consumed_marker_even_when_requested(
    monkeypatch,
) -> None:
    stored_tasks = []
    appended_events = []

    async def _create_task(task, *, ttl_seconds, timeout=None):
        stored_tasks.append(task)
        return True

    async def _append_event(**kwargs):
        appended_events.append(kwargs)
        return object()

    async def _read_team(**_kwargs):
        return TeamConfig(
            team_id="shadow-clone-v2:project-1:thread-1",
            thread_id="thread-1",
            project_id="project-1",
            account_id="account-1",
            facilitator_id="team-lead",
            members=[
                AgentIdentity(
                    agent_id="comm-agent-a@thread-1",
                    agent_name="comm-agent-a",
                    thread_id="thread-1",
                    project_id="project-1",
                    account_id="account-1",
                    role="Sender",
                ),
                AgentIdentity(
                    agent_id="comm-agent-b@thread-1",
                    agent_name="comm-agent-b",
                    thread_id="thread-1",
                    project_id="project-1",
                    account_id="account-1",
                    role="Recipient",
                ),
            ],
        )

    monkeypatch.setattr(agent_tools.team_store, "read_team", _read_team)
    monkeypatch.setattr(agent_tools.task_pool, "create_task", _create_task)
    monkeypatch.setattr(agent_tools.event_log, "append_event", _append_event)

    tools = agent_tools.ShadowCloneV2AgentTools(
        context=_context_with_user_message(
            actor_name="team-lead",
            current_user_message=(
                "B must output P2P_B_CONSUMED_A_MESSAGE_456 after reading inbox, "
                "and send P2P_B_TO_MAIN_456, but B task description must not "
                "directly contain the consumed marker."
            ),
        ),
        clock=lambda: "2026-06-02T00:01:00+00:00",
    )

    result = await tools.task_create(
        task_id="task-comm-b",
        subject="Recipient task",
        description=(
            "After unblocked, read inbox. Output "
            "P2P_B_CONSUMED_A_MESSAGE_456 and send P2P_B_TO_MAIN_456."
        ),
        metadata={"agent_name": "comm-agent-b"},
    )

    assert "P2P_B_CONSUMED_A_MESSAGE_456" not in result.task.description
    assert "P2P_B_CONSUMED_A_MESSAGE_<unique>" in result.task.description
    assert "P2P_B_TO_MAIN_456" in result.task.description
    assert stored_tasks[0].description == result.task.description
    assert "P2P_B_CONSUMED_A_MESSAGE_456" not in appended_events[0]["payload"]["description"]
    assert "P2P_B_CONSUMED_A_MESSAGE_456" not in appended_events[0]["payload"]["task"]["description"]


@pytest.mark.asyncio
async def test_task_create_tool_placeholderizes_peer_consumed_marker_without_user_context(
    monkeypatch,
) -> None:
    stored_tasks = []

    async def _create_task(task, *, ttl_seconds, timeout=None):
        stored_tasks.append(task)
        return True

    async def _append_event(**_kwargs):
        return object()

    async def _read_team(**_kwargs):
        return TeamConfig(
            team_id="shadow-clone-v2:project-1:thread-1",
            thread_id="thread-1",
            project_id="project-1",
            account_id="account-1",
            facilitator_id="team-lead",
            members=[
                AgentIdentity(
                    agent_id="comm-agent-b@thread-1",
                    agent_name="comm-agent-b",
                    thread_id="thread-1",
                    project_id="project-1",
                    account_id="account-1",
                    role="Recipient",
                ),
            ],
        )

    monkeypatch.setattr(agent_tools.team_store, "read_team", _read_team)
    monkeypatch.setattr(agent_tools.task_pool, "create_task", _create_task)
    monkeypatch.setattr(agent_tools.event_log, "append_event", _append_event)

    tools = agent_tools.ShadowCloneV2AgentTools(
        context=_context(actor_name="team-lead"),
        clock=lambda: "2026-06-02T00:01:00+00:00",
    )

    result = await tools.task_create(
        task_id="task-comm-b",
        subject="Recipient task",
        description=(
            "After unblocked, read inbox. Output "
            "P2P_B_CONSUMED_A_MESSAGE_456 after receiving A's message."
        ),
        metadata={"agent_name": "comm-agent-b"},
    )

    assert "P2P_B_CONSUMED_A_MESSAGE_456" not in result.task.description
    assert "P2P_B_CONSUMED_A_MESSAGE_<unique>" in result.task.description
    assert stored_tasks[0].description == result.task.description


@pytest.mark.asyncio
async def test_task_create_tool_rejects_unknown_metadata_agent_name(
    monkeypatch,
) -> None:
    created_tasks = []
    appended_events = []

    async def _read_team(**_kwargs):
        return _team()

    async def _create_task(task, *, ttl_seconds, timeout=None):
        created_tasks.append(task)
        return True

    async def _append_event(**kwargs):
        appended_events.append(kwargs)
        return object()

    monkeypatch.setattr(agent_tools.team_store, "read_team", _read_team)
    monkeypatch.setattr(agent_tools.task_pool, "create_task", _create_task)
    monkeypatch.setattr(agent_tools.event_log, "append_event", _append_event)

    tools = agent_tools.ShadowCloneV2AgentTools(
        context=_context(actor_name="team-lead")
    )

    with pytest.raises(ValueError, match="unknown metadata.agent_name: main"):
        await tools.task_create(
            task_id="main-coordination",
            subject="Main coordination",
            description="This task should not target an unknown main agent.",
            metadata={"agent_name": "main"},
        )

    assert created_tasks == []
    assert appended_events == []


@pytest.mark.asyncio
async def test_task_create_tool_rejects_dependencies_until_task_update_links_graph(
    monkeypatch,
) -> None:
    created_tasks = []
    appended_events = []

    async def _create_task(task, *, ttl_seconds, timeout=None):
        created_tasks.append(task)
        return True

    async def _append_event(**kwargs):
        appended_events.append(kwargs)
        return object()

    monkeypatch.setattr(agent_tools.task_pool, "create_task", _create_task)
    monkeypatch.setattr(agent_tools.event_log, "append_event", _append_event)

    tools = agent_tools.ShadowCloneV2AgentTools(
        context=_context(actor_name="researcher")
    )

    with pytest.raises(ValueError, match="TaskUpdate"):
        await tools.task_create(
            task_id="task-1",
            subject="Dependent task",
            description="Should link dependencies through TaskUpdate.",
            blocked_by=["task-0"],
        )

    assert created_tasks == []
    assert appended_events == []


@pytest.mark.asyncio
async def test_task_list_tool_reads_current_run_tasks(monkeypatch) -> None:
    listed_calls = []

    async def _list_tasks(*, run_id, timeout=None):
        listed_calls.append((run_id, timeout))
        return [
            agent_tools.Task(
                id="task-1",
                run_id="run-1",
                thread_id="thread-1",
                subject="Research",
                description="Find evidence.",
                created_at="2026-06-02T00:00:00+00:00",
                updated_at="2026-06-02T00:00:00+00:00",
            )
        ]

    monkeypatch.setattr(agent_tools.task_pool, "list_tasks", _list_tasks)

    tools = agent_tools.ShadowCloneV2AgentTools(context=_context())

    result = await tools.task_list()

    assert [task.id for task in result.tasks] == ["task-1"]
    assert listed_calls == [("run-1", None)]
    assert result.next_sequence == 10


@pytest.mark.asyncio
async def test_task_get_tool_returns_full_versioned_task(monkeypatch) -> None:
    read_calls = []
    task = agent_tools.Task(
        id="task-1",
        run_id="run-1",
        thread_id="thread-1",
        subject="Research",
        description="Find evidence.",
        plan_revision=3,
        metadata={"progress": "50%"},
        created_at="2026-06-02T00:00:00+00:00",
        updated_at="2026-06-02T00:03:00+00:00",
    )

    async def _read_task(**kwargs):
        read_calls.append(kwargs)
        return task

    monkeypatch.setattr(agent_tools.task_pool, "read_task", _read_task)

    tools = agent_tools.ShadowCloneV2AgentTools(context=_context())

    result = await tools.task_get(task_id="task-1")

    assert result.task == task
    assert result.next_sequence == 10
    assert read_calls == [{"run_id": "run-1", "task_id": "task-1"}]


@pytest.mark.asyncio
async def test_task_get_tool_rejects_missing_task(monkeypatch) -> None:
    async def _read_task(**_kwargs):
        return None

    monkeypatch.setattr(agent_tools.task_pool, "read_task", _read_task)

    tools = agent_tools.ShadowCloneV2AgentTools(context=_context())

    with pytest.raises(ValueError, match="task not found"):
        await tools.task_get(task_id="missing")


@pytest.mark.asyncio
async def test_task_update_tool_requires_expected_version() -> None:
    tools = agent_tools.ShadowCloneV2AgentTools(context=_context())

    with pytest.raises(ValueError, match="expected_version is required"):
        await tools.task_update(task_id="task-1", metadata={"progress": "25%"})


@pytest.mark.asyncio
async def test_task_update_tool_persists_patch_and_appends_task_updated_event(
    monkeypatch,
) -> None:
    updated_tasks = []
    appended_events = []
    current_task = agent_tools.Task(
        id="task-1",
        run_id="run-1",
        thread_id="thread-1",
        subject="Research",
        description="Find evidence.",
        status=TaskStatus.PENDING,
        plan_revision=1,
        metadata={"agent_name": "researcher"},
        created_at="2026-06-02T00:00:00+00:00",
        updated_at="2026-06-02T00:00:00+00:00",
    )

    async def _read_task(**_kwargs):
        return current_task

    async def _list_tasks(**_kwargs):
        return [current_task]

    async def _update_task(task, *, expected_plan_revision, ttl_seconds, timeout=None):
        updated_tasks.append((task, expected_plan_revision, ttl_seconds, timeout))
        return True

    async def _append_event(**kwargs):
        appended_events.append(kwargs)
        return object()

    async def _read_team(**_kwargs):
        return _team()

    monkeypatch.setattr(agent_tools.task_pool, "read_task", _read_task)
    monkeypatch.setattr(agent_tools.task_pool, "list_tasks", _list_tasks)
    monkeypatch.setattr(agent_tools.task_pool, "update_task", _update_task)
    monkeypatch.setattr(agent_tools.event_log, "append_event", _append_event)
    monkeypatch.setattr(agent_tools.team_store, "read_team", _read_team)

    tools = agent_tools.ShadowCloneV2AgentTools(
        context=_context(actor_name="researcher"),
        clock=lambda: "2026-06-02T00:04:00+00:00",
    )

    result = await tools.task_update(
        task_id="task-1",
        expected_version=1,
        status=TaskStatus.IN_PROGRESS,
        owner_agent="researcher",
        metadata={"progress": "25%"},
    )

    updated_task = result.task
    assert updated_task.plan_revision == 2
    assert updated_task.status == TaskStatus.IN_PROGRESS
    assert updated_task.owner_agent == "researcher"
    assert updated_task.metadata == {"agent_name": "researcher", "progress": "25%"}
    assert updated_task.updated_at == "2026-06-02T00:04:00+00:00"
    assert updated_tasks == [(updated_task, 1, 3600, None)]
    assert appended_events[0]["event_type"] == EventType.TASK_UPDATED
    payload = appended_events[0]["payload"]
    assert payload["task_id"] == "task-1"
    assert payload["actor"] == "researcher"
    assert payload["previous_version"] == 1
    assert payload["new_version"] == 2
    assert payload["changed_fields"] == ["metadata", "owner_agent", "status"]
    assert payload["previous_status"] == "pending"
    assert payload["new_status"] == "in_progress"
    assert payload["affected_task_ids"] == []
    assert payload["affected_tasks"] == []
    assert payload["task"]["status"] == "in_progress"
    assert payload["task"]["owner_agent"] == "researcher"
    assert payload["task"]["metadata"] == {
        "agent_name": "researcher",
        "progress": "25%",
    }
    assert result.next_sequence == 11


@pytest.mark.asyncio
async def test_task_update_tool_stale_version_rejects_without_event(
    monkeypatch,
) -> None:
    appended_events = []
    current_task = agent_tools.Task(
        id="task-1",
        run_id="run-1",
        thread_id="thread-1",
        subject="Research",
        description="Find evidence.",
        plan_revision=2,
        created_at="2026-06-02T00:00:00+00:00",
        updated_at="2026-06-02T00:01:00+00:00",
    )

    async def _read_task(**_kwargs):
        return current_task

    async def _list_tasks(**_kwargs):
        return [current_task]

    async def _update_task(*_args, **_kwargs):
        return False

    async def _append_event(**kwargs):
        appended_events.append(kwargs)
        return object()

    monkeypatch.setattr(agent_tools.task_pool, "read_task", _read_task)
    monkeypatch.setattr(agent_tools.task_pool, "list_tasks", _list_tasks)
    monkeypatch.setattr(agent_tools.task_pool, "update_task", _update_task)
    monkeypatch.setattr(agent_tools.event_log, "append_event", _append_event)

    tools = agent_tools.ShadowCloneV2AgentTools(
        context=_context(actor_name="researcher")
    )

    with pytest.raises(ValueError, match="stale task version"):
        await tools.task_update(
            task_id="task-1",
            expected_version=1,
            metadata={"progress": "25%"},
        )

    assert appended_events == []


@pytest.mark.asyncio
async def test_task_update_tool_rejects_dependency_cycle_without_event(
    monkeypatch,
) -> None:
    appended_events = []
    task_a = agent_tools.Task(
        id="task-a",
        run_id="run-1",
        thread_id="thread-1",
        subject="A",
        description="A",
        plan_revision=1,
        blocks=["task-b"],
        created_at="2026-06-02T00:00:00+00:00",
        updated_at="2026-06-02T00:00:00+00:00",
    )
    task_b = agent_tools.Task(
        id="task-b",
        run_id="run-1",
        thread_id="thread-1",
        subject="B",
        description="B",
        plan_revision=1,
        blocked_by=["task-a"],
        created_at="2026-06-02T00:00:00+00:00",
        updated_at="2026-06-02T00:00:00+00:00",
    )

    async def _read_task(*, run_id, task_id, timeout=None):
        return {"task-a": task_a, "task-b": task_b}.get(task_id)

    async def _list_tasks(**_kwargs):
        return [task_a, task_b]

    async def _append_event(**kwargs):
        appended_events.append(kwargs)

    async def _read_team(**_kwargs):
        return _team()

    monkeypatch.setattr(agent_tools.task_pool, "read_task", _read_task)
    monkeypatch.setattr(agent_tools.task_pool, "list_tasks", _list_tasks)
    monkeypatch.setattr(agent_tools.event_log, "append_event", _append_event)
    monkeypatch.setattr(agent_tools.team_store, "read_team", _read_team)

    tools = agent_tools.ShadowCloneV2AgentTools(
        context=_context(actor_name="researcher")
    )

    with pytest.raises(ValueError, match="dependency cycle"):
        await tools.task_update(
            task_id="task-b",
            expected_version=1,
            add_blocks=["task-a"],
        )

    assert appended_events == []


@pytest.mark.asyncio
async def test_task_update_tool_rejects_in_progress_with_unresolved_blockers(
    monkeypatch,
) -> None:
    task = agent_tools.Task(
        id="task-a",
        run_id="run-1",
        thread_id="thread-1",
        subject="A",
        description="A",
        plan_revision=1,
        blocked_by=["task-b"],
        created_at="2026-06-02T00:00:00+00:00",
        updated_at="2026-06-02T00:00:00+00:00",
    )
    blocker = agent_tools.Task(
        id="task-b",
        run_id="run-1",
        thread_id="thread-1",
        subject="B",
        description="B",
        status=TaskStatus.PENDING,
        plan_revision=1,
        blocks=["task-a"],
        created_at="2026-06-02T00:00:00+00:00",
        updated_at="2026-06-02T00:00:00+00:00",
    )

    async def _read_task(*, run_id, task_id, timeout=None):
        return {"task-a": task, "task-b": blocker}.get(task_id)

    async def _list_tasks(**_kwargs):
        return [task, blocker]

    async def _read_team(**_kwargs):
        return _team()

    monkeypatch.setattr(agent_tools.task_pool, "read_task", _read_task)
    monkeypatch.setattr(agent_tools.task_pool, "list_tasks", _list_tasks)
    monkeypatch.setattr(agent_tools.team_store, "read_team", _read_team)

    tools = agent_tools.ShadowCloneV2AgentTools(
        context=_context(actor_name="researcher")
    )

    with pytest.raises(ValueError, match="unresolved blockers"):
        await tools.task_update(
            task_id="task-a",
            expected_version=1,
            status=TaskStatus.IN_PROGRESS,
        )


@pytest.mark.asyncio
async def test_task_update_tool_rejects_pending_with_unresolved_blockers(
    monkeypatch,
) -> None:
    task = agent_tools.Task(
        id="task-a",
        run_id="run-1",
        thread_id="thread-1",
        subject="A",
        description="A",
        status=TaskStatus.BLOCKED,
        plan_revision=1,
        blocked_by=["task-b"],
        created_at="2026-06-02T00:00:00+00:00",
        updated_at="2026-06-02T00:00:00+00:00",
    )
    blocker = agent_tools.Task(
        id="task-b",
        run_id="run-1",
        thread_id="thread-1",
        subject="B",
        description="B",
        status=TaskStatus.PENDING,
        plan_revision=1,
        blocks=["task-a"],
        created_at="2026-06-02T00:00:00+00:00",
        updated_at="2026-06-02T00:00:00+00:00",
    )

    async def _read_task(*, run_id, task_id, timeout=None):
        return {"task-a": task, "task-b": blocker}.get(task_id)

    async def _list_tasks(**_kwargs):
        return [task, blocker]

    async def _read_team(**_kwargs):
        return _team()

    monkeypatch.setattr(agent_tools.task_pool, "read_task", _read_task)
    monkeypatch.setattr(agent_tools.task_pool, "list_tasks", _list_tasks)
    monkeypatch.setattr(agent_tools.team_store, "read_team", _read_team)

    tools = agent_tools.ShadowCloneV2AgentTools(
        context=_context(actor_name="researcher")
    )

    with pytest.raises(ValueError, match="unresolved blockers"):
        await tools.task_update(
            task_id="task-a",
            expected_version=1,
            status=TaskStatus.PENDING,
        )


@pytest.mark.asyncio
async def test_task_update_tool_rejects_terminal_task_mutation(monkeypatch) -> None:
    completed_task = agent_tools.Task(
        id="task-1",
        run_id="run-1",
        thread_id="thread-1",
        subject="Done",
        description="Completed task.",
        status=TaskStatus.COMPLETED,
        plan_revision=2,
        metadata={"agent_name": "researcher"},
        created_at="2026-06-02T00:00:00+00:00",
        updated_at="2026-06-02T00:10:00+00:00",
    )

    async def _read_task(**_kwargs):
        return completed_task

    monkeypatch.setattr(agent_tools.task_pool, "read_task", _read_task)

    tools = agent_tools.ShadowCloneV2AgentTools(
        context=_context(actor_name="researcher")
    )

    with pytest.raises(ValueError, match="terminal task"):
        await tools.task_update(
            task_id="task-1",
            expected_version=2,
            metadata={"progress": "reopened"},
        )


@pytest.mark.asyncio
async def test_task_update_tool_rejects_non_member_actor(monkeypatch) -> None:
    current_task = agent_tools.Task(
        id="task-1",
        run_id="run-1",
        thread_id="thread-1",
        subject="Research",
        description="Find evidence.",
        plan_revision=1,
        metadata={"agent_name": "researcher"},
        created_at="2026-06-02T00:00:00+00:00",
        updated_at="2026-06-02T00:00:00+00:00",
    )

    async def _read_task(**_kwargs):
        return current_task

    async def _read_team(**_kwargs):
        return _team()

    monkeypatch.setattr(agent_tools.task_pool, "read_task", _read_task)
    monkeypatch.setattr(agent_tools.team_store, "read_team", _read_team)

    tools = agent_tools.ShadowCloneV2AgentTools(context=_context(actor_name="intruder"))

    with pytest.raises(ValueError, match="not a member"):
        await tools.task_update(
            task_id="task-1",
            expected_version=1,
            metadata={"progress": "25%"},
        )


@pytest.mark.asyncio
async def test_task_update_tool_rejects_unknown_owner_assignment(monkeypatch) -> None:
    current_task = agent_tools.Task(
        id="task-1",
        run_id="run-1",
        thread_id="thread-1",
        subject="Research",
        description="Find evidence.",
        plan_revision=1,
        metadata={"agent_name": "researcher"},
        created_at="2026-06-02T00:00:00+00:00",
        updated_at="2026-06-02T00:00:00+00:00",
    )

    async def _read_task(**_kwargs):
        return current_task

    async def _read_team(**_kwargs):
        return _team()

    monkeypatch.setattr(agent_tools.task_pool, "read_task", _read_task)
    monkeypatch.setattr(agent_tools.team_store, "read_team", _read_team)

    tools = agent_tools.ShadowCloneV2AgentTools(
        context=_context(actor_name="team-lead")
    )

    with pytest.raises(ValueError, match="unknown owner_agent"):
        await tools.task_update(
            task_id="task-1",
            expected_version=1,
            owner_agent="not-on-team",
        )


@pytest.mark.asyncio
async def test_task_update_tool_rejects_unknown_metadata_agent_assignment(
    monkeypatch,
) -> None:
    current_task = agent_tools.Task(
        id="task-1",
        run_id="run-1",
        thread_id="thread-1",
        subject="Research",
        description="Find evidence.",
        plan_revision=1,
        metadata={"agent_name": "researcher"},
        created_at="2026-06-02T00:00:00+00:00",
        updated_at="2026-06-02T00:00:00+00:00",
    )

    async def _read_task(**_kwargs):
        return current_task

    async def _read_team(**_kwargs):
        return _team()

    monkeypatch.setattr(agent_tools.task_pool, "read_task", _read_task)
    monkeypatch.setattr(agent_tools.team_store, "read_team", _read_team)

    tools = agent_tools.ShadowCloneV2AgentTools(
        context=_context(actor_name="team-lead")
    )

    with pytest.raises(ValueError, match="unknown metadata.agent_name"):
        await tools.task_update(
            task_id="task-1",
            expected_version=1,
            metadata={"agent_name": "not-on-team"},
        )


@pytest.mark.asyncio
async def test_task_update_tool_maintains_bidirectional_dependency_edges(
    monkeypatch,
) -> None:
    updated_batches = []
    appended_events = []
    task_a = agent_tools.Task(
        id="task-a",
        run_id="run-1",
        thread_id="thread-1",
        subject="A",
        description="A",
        plan_revision=1,
        created_at="2026-06-02T00:00:00+00:00",
        updated_at="2026-06-02T00:00:00+00:00",
    )
    task_b = agent_tools.Task(
        id="task-b",
        run_id="run-1",
        thread_id="thread-1",
        subject="B",
        description="B",
        plan_revision=1,
        created_at="2026-06-02T00:00:00+00:00",
        updated_at="2026-06-02T00:00:00+00:00",
    )

    async def _read_task(*, run_id, task_id, timeout=None):
        return {"task-a": task_a, "task-b": task_b}.get(task_id)

    async def _list_tasks(**_kwargs):
        return [task_a, task_b]

    async def _update_tasks(
        *, tasks, expected_plan_revisions, ttl_seconds, timeout=None
    ):
        updated_batches.append((tasks, expected_plan_revisions, ttl_seconds, timeout))
        return True

    async def _append_event(**kwargs):
        appended_events.append(kwargs)

    async def _read_team(**_kwargs):
        return _team()

    monkeypatch.setattr(agent_tools.task_pool, "read_task", _read_task)
    monkeypatch.setattr(agent_tools.task_pool, "list_tasks", _list_tasks)
    monkeypatch.setattr(agent_tools.task_pool, "update_tasks", _update_tasks)
    monkeypatch.setattr(agent_tools.event_log, "append_event", _append_event)
    monkeypatch.setattr(agent_tools.team_store, "read_team", _read_team)

    tools = agent_tools.ShadowCloneV2AgentTools(
        context=_context(actor_name="researcher"),
        clock=lambda: "2026-06-02T00:06:00+00:00",
    )

    result = await tools.task_update(
        task_id="task-a",
        expected_version=1,
        add_blocked_by=["task-b"],
    )

    updated_tasks = {task.id: task for task in updated_batches[0][0]}
    assert result.task == updated_tasks["task-a"]
    assert updated_tasks["task-a"].blocked_by == ["task-b"]
    assert updated_tasks["task-a"].status == TaskStatus.BLOCKED
    assert updated_tasks["task-b"].blocks == ["task-a"]
    assert updated_tasks["task-a"].plan_revision == 2
    assert updated_tasks["task-b"].plan_revision == 2
    assert updated_batches[0][1] == {"task-a": 1, "task-b": 1}
    assert appended_events[0]["payload"]["changed_fields"] == ["blocked_by", "status"]
    assert appended_events[0]["payload"]["affected_task_ids"] == ["task-b"]
    affected_snapshot = appended_events[0]["payload"]["affected_tasks"][0]
    assert affected_snapshot["task_id"] == "task-b"
    assert affected_snapshot["blocks"] == ["task-a"]
    assert affected_snapshot["plan_revision"] == 2
    assert appended_events[0]["payload"]["task"]["blocked_by"] == ["task-b"]
    assert appended_events[0]["payload"]["task"]["status"] == "blocked"


@pytest.mark.asyncio
async def test_send_message_tool_is_p2p_and_evented(monkeypatch) -> None:
    sent_messages = []
    appended_events = []

    async def _read_team(**_kwargs):
        return _team()

    async def _send_message(**kwargs):
        assert kwargs["account_id"] == "account-1"
        sent_messages.append(kwargs)
        return agent_tools.MailboxMessage(
            id="1-0",
            run_id=kwargs["run_id"],
            thread_id=kwargs["thread_id"],
            project_id=kwargs["project_id"],
            sender=kwargs["sender"],
            recipient=kwargs["recipient"],
            kind=kwargs["kind"],
            type=kwargs["message_type"],
            idempotency_key=kwargs["idempotency_key"],
            summary=kwargs["summary"],
            text=kwargs["text"],
            payload=kwargs["payload"],
            created_at=kwargs["created_at"],
        )

    async def _append_event(**kwargs):
        appended_events.append(kwargs)
        return object()

    monkeypatch.setattr(agent_tools.team_store, "read_team", _read_team)
    monkeypatch.setattr(agent_tools.mailbox, "send_message", _send_message)
    monkeypatch.setattr(agent_tools.event_log, "append_event", _append_event)

    tools = agent_tools.ShadowCloneV2AgentTools(
        context=_context(actor_name="researcher"),
        clock=lambda: "2026-06-02T00:02:00+00:00",
    )

    result = await tools.send_message(
        recipient="reviewer",
        text="Please check the source list directly.",
        summary="Review sources",
        idempotency_key="researcher-reviewer-sources-1",
    )

    assert result.message.id == "1-0"
    assert sent_messages[0]["sender"] == "researcher"
    assert sent_messages[0]["recipient"] == "reviewer"
    assert sent_messages[0]["kind"] == MessageKind.TEXT
    assert sent_messages[0]["message_type"] == MessageType.TEXT
    assert appended_events[0]["event_type"] == EventType.MAILBOX_SENT
    assert appended_events[0]["payload"] == {
        "message_id": "1-0",
        "sender": "researcher",
        "recipient": "reviewer",
        "kind": "text",
        "message_type": "text",
        "idempotency_key": "researcher-reviewer-sources-1",
        "summary": "Review sources",
        "text": "Please check the source list directly.",
        "payload": {},
    }
    assert result.next_sequence == 11


@pytest.mark.asyncio
async def test_send_message_tool_wakes_idle_recipient_once(monkeypatch) -> None:
    stored_teams = []
    appended_events = []
    team = _team().model_copy(
        update={
            "members": [
                _team().members[0],
                _team()
                .members[1]
                .model_copy(
                    update={
                        "status": AgentLifecycleStatus.IDLE,
                        "idle_since": "2026-06-02T00:00:00+00:00",
                        "idle_expires_at": "2026-06-02T00:20:00+00:00",
                    }
                ),
            ]
        }
    )

    async def _read_team(**_kwargs):
        return team

    async def _store_team(team, *, ttl_seconds, timeout=None):
        stored_teams.append((team, ttl_seconds, timeout))

    async def _send_message(**kwargs):
        message = agent_tools.MailboxMessage(
            id="1-0",
            run_id=kwargs["run_id"],
            thread_id=kwargs["thread_id"],
            project_id=kwargs["project_id"],
            sender=kwargs["sender"],
            recipient=kwargs["recipient"],
            kind=kwargs["kind"],
            type=kwargs["message_type"],
            idempotency_key=kwargs["idempotency_key"],
            summary=kwargs["summary"],
            text=kwargs["text"],
            payload=kwargs["payload"],
            created_at=kwargs["created_at"],
        )
        object.__setattr__(message, "_was_created", True)
        return message

    async def _append_event(**kwargs):
        appended_events.append(kwargs)
        return object()

    monkeypatch.setattr(agent_tools.team_store, "read_team", _read_team)
    monkeypatch.setattr(agent_tools.team_store, "store_team", _store_team)
    monkeypatch.setattr(agent_tools.mailbox, "send_message", _send_message)
    monkeypatch.setattr(agent_tools.event_log, "append_event", _append_event)

    tools = agent_tools.ShadowCloneV2AgentTools(
        context=_context(actor_name="researcher"),
        clock=lambda: "2026-06-02T00:02:00+00:00",
    )

    result = await tools.send_message(
        recipient="reviewer",
        text="Wake up for review.",
        summary="Review wake",
        idempotency_key="wake-reviewer",
    )

    assert result.next_sequence == 12
    assert [event["event_type"] for event in appended_events] == [
        EventType.MAILBOX_SENT,
        EventType.AGENT_WAKE,
    ]
    assert appended_events[1]["payload"] == {
        "account_id": "account-1",
        "agent_name": "reviewer",
        "wake_reason": "mailbox_unread",
        "message_id": "1-0",
        "status": "working",
    }
    assert stored_teams[0][0].members[1].status == AgentLifecycleStatus.WORKING
    assert stored_teams[0][0].members[1].idle_since is None


@pytest.mark.asyncio
async def test_send_message_tool_does_not_reemit_event_or_wake_for_duplicate(
    monkeypatch,
) -> None:
    stored_teams = []
    appended_events = []

    async def _read_team(**_kwargs):
        return _team()

    async def _store_team(team, *, ttl_seconds, timeout=None):
        stored_teams.append(team)

    async def _send_message(**kwargs):
        message = agent_tools.MailboxMessage(
            id="1-0",
            run_id=kwargs["run_id"],
            thread_id=kwargs["thread_id"],
            project_id=kwargs["project_id"],
            sender=kwargs["sender"],
            recipient=kwargs["recipient"],
            kind=kwargs["kind"],
            type=kwargs["message_type"],
            idempotency_key=kwargs["idempotency_key"],
            summary=kwargs["summary"],
            text=kwargs["text"],
            payload=kwargs["payload"],
            created_at="2026-06-02T00:01:00+00:00",
        )
        object.__setattr__(message, "_was_created", False)
        return message

    async def _append_event(**kwargs):
        appended_events.append(kwargs)
        return object()

    monkeypatch.setattr(agent_tools.team_store, "read_team", _read_team)
    monkeypatch.setattr(agent_tools.team_store, "store_team", _store_team)
    monkeypatch.setattr(agent_tools.mailbox, "send_message", _send_message)
    monkeypatch.setattr(agent_tools.event_log, "append_event", _append_event)

    tools = agent_tools.ShadowCloneV2AgentTools(
        context=_context(actor_name="researcher"),
        clock=lambda: "2026-06-02T00:02:00+00:00",
    )

    result = await tools.send_message(
        recipient="reviewer",
        text="Duplicate.",
        summary="Duplicate",
        idempotency_key="duplicate",
    )

    assert result.next_sequence == 10
    assert appended_events == []
    assert stored_teams == []


@pytest.mark.asyncio
async def test_send_message_tool_recovers_missing_projection_for_duplicate(
    monkeypatch,
) -> None:
    appended_events = []
    marked_projected = []

    async def _read_team(**_kwargs):
        return _team()

    async def _send_message(**kwargs):
        message = agent_tools.MailboxMessage(
            id="1-0",
            run_id=kwargs["run_id"],
            thread_id=kwargs["thread_id"],
            project_id=kwargs["project_id"],
            sender=kwargs["sender"],
            recipient=kwargs["recipient"],
            kind=kwargs["kind"],
            type=kwargs["message_type"],
            idempotency_key=kwargs["idempotency_key"],
            summary=kwargs["summary"],
            text=kwargs["text"],
            payload=kwargs["payload"],
            created_at="2026-06-02T00:01:00+00:00",
        )
        object.__setattr__(message, "_was_created", False)
        object.__setattr__(message, "_sent_projected", False)
        return message

    async def _append_event(**kwargs):
        appended_events.append(kwargs)
        return object()

    async def _mark_sent_projected(message):
        marked_projected.append(message.id)

    monkeypatch.setattr(agent_tools.team_store, "read_team", _read_team)
    monkeypatch.setattr(agent_tools.mailbox, "send_message", _send_message)
    monkeypatch.setattr(
        agent_tools.mailbox, "mark_sent_projected", _mark_sent_projected
    )
    monkeypatch.setattr(agent_tools.event_log, "append_event", _append_event)

    tools = agent_tools.ShadowCloneV2AgentTools(
        context=_context(actor_name="researcher"),
        clock=lambda: "2026-06-02T00:02:00+00:00",
    )

    result = await tools.send_message(
        recipient="reviewer",
        text="Recover missing projection.",
        summary="Recover",
        idempotency_key="needs-projection",
    )

    assert result.next_sequence == 11
    assert appended_events[0]["event_type"] == EventType.MAILBOX_SENT
    assert marked_projected == ["1-0"]


@pytest.mark.asyncio
async def test_send_message_tool_projection_claim_is_atomic_for_concurrent_duplicates(
    monkeypatch,
) -> None:
    appended_events = []
    projected = []
    released = []
    claim_barrier = _AsyncBarrier(2)
    claim_lock = asyncio.Lock()
    claim_taken = False

    async def _read_team(**_kwargs):
        team = _team()
        return team.model_copy(
            update={
                "members": [
                    team.members[0],
                    team.members[1].model_copy(
                        update={
                            "status": AgentLifecycleStatus.IDLE,
                            "idle_since": "2026-06-02T00:00:00+00:00",
                            "idle_expires_at": "2026-06-02T00:20:00+00:00",
                        }
                    ),
                ]
            }
        )

    async def _store_team(*_args, **_kwargs):
        return None

    async def _send_message(**kwargs):
        message = agent_tools.MailboxMessage(
            id="1-0",
            run_id=kwargs["run_id"],
            thread_id=kwargs["thread_id"],
            project_id=kwargs["project_id"],
            sender=kwargs["sender"],
            recipient=kwargs["recipient"],
            kind=kwargs["kind"],
            type=kwargs["message_type"],
            idempotency_key=kwargs["idempotency_key"],
            summary=kwargs["summary"],
            text=kwargs["text"],
            payload=kwargs["payload"],
            created_at="2026-06-02T00:01:00+00:00",
        )
        object.__setattr__(message, "_was_created", False)
        object.__setattr__(message, "_sent_projected", False)
        return message

    async def _claim_sent_projection(message):
        nonlocal claim_taken
        await claim_barrier.wait()
        async with claim_lock:
            if claim_taken:
                return False
            claim_taken = True
            return True

    async def _append_event(**kwargs):
        appended_events.append(kwargs)
        return object()

    async def _mark_sent_projected(message):
        projected.append(message.id)

    async def _release_sent_projection(message):
        released.append(message.id)

    monkeypatch.setattr(agent_tools.team_store, "read_team", _read_team)
    monkeypatch.setattr(agent_tools.team_store, "store_team", _store_team)
    monkeypatch.setattr(agent_tools.mailbox, "send_message", _send_message)
    monkeypatch.setattr(
        agent_tools.mailbox, "claim_sent_projection", _claim_sent_projection
    )
    monkeypatch.setattr(
        agent_tools.mailbox, "mark_sent_projected", _mark_sent_projected
    )
    monkeypatch.setattr(
        agent_tools.mailbox, "release_sent_projection", _release_sent_projection
    )
    monkeypatch.setattr(agent_tools.event_log, "append_event", _append_event)

    first_tools = agent_tools.ShadowCloneV2AgentTools(
        context=_context(actor_name="researcher"),
        clock=lambda: "2026-06-02T00:02:00+00:00",
    )
    second_tools = agent_tools.ShadowCloneV2AgentTools(
        context=_context(actor_name="researcher"),
        clock=lambda: "2026-06-02T00:02:00+00:00",
    )

    await asyncio.gather(
        first_tools.send_message(
            recipient="reviewer",
            text="Duplicate.",
            summary="Duplicate",
            idempotency_key="same",
        ),
        second_tools.send_message(
            recipient="reviewer",
            text="Duplicate.",
            summary="Duplicate",
            idempotency_key="same",
        ),
    )

    assert [event["event_type"] for event in appended_events] == [
        EventType.MAILBOX_SENT,
        EventType.AGENT_WAKE,
    ]
    assert projected == ["1-0"]
    assert released == []
    assert first_tools._messages_sent + second_tools._messages_sent == 1


@pytest.mark.asyncio
async def test_send_message_tool_retries_wake_after_store_failure_without_resending(
    monkeypatch,
) -> None:
    appended_events = []
    stored_teams = []
    projection_state = {"value": None}
    store_attempts = {"count": 0}

    async def _read_team(**_kwargs):
        team = _team()
        return team.model_copy(
            update={
                "members": [
                    team.members[0],
                    team.members[1].model_copy(
                        update={
                            "status": AgentLifecycleStatus.IDLE,
                            "idle_since": "2026-06-02T00:00:00+00:00",
                            "idle_expires_at": "2026-06-02T00:20:00+00:00",
                        }
                    ),
                ]
            }
        )

    async def _send_message(**kwargs):
        message = agent_tools.MailboxMessage(
            id="1-0",
            run_id=kwargs["run_id"],
            thread_id=kwargs["thread_id"],
            project_id=kwargs["project_id"],
            sender=kwargs["sender"],
            recipient=kwargs["recipient"],
            kind=kwargs["kind"],
            type=kwargs["message_type"],
            idempotency_key=kwargs["idempotency_key"],
            summary=kwargs["summary"],
            text=kwargs["text"],
            payload=kwargs["payload"],
            created_at="2026-06-02T00:01:00+00:00",
        )
        object.__setattr__(message, "_was_created", False)
        object.__setattr__(
            message, "_sent_projected", projection_state["value"] == "sent_done"
        )
        object.__setattr__(message, "_sent_projection_state", projection_state["value"])
        return message

    async def _claim_sent_projection(_message):
        return True

    async def _mark_sent_projection_state(_message, state):
        projection_state["value"] = state

    async def _mark_sent_projected(_message):
        projection_state["value"] = "sent_done"

    async def _release_sent_projection(_message):
        return None

    async def _store_team(team, *, ttl_seconds, timeout=None):
        store_attempts["count"] += 1
        if store_attempts["count"] == 1:
            raise RuntimeError("store failed")
        stored_teams.append(team)

    async def _append_event(**kwargs):
        appended_events.append(kwargs)
        return object()

    monkeypatch.setattr(agent_tools.team_store, "read_team", _read_team)
    monkeypatch.setattr(agent_tools.team_store, "store_team", _store_team)
    monkeypatch.setattr(agent_tools.mailbox, "send_message", _send_message)
    monkeypatch.setattr(
        agent_tools.mailbox, "claim_sent_projection", _claim_sent_projection
    )
    monkeypatch.setattr(
        agent_tools.mailbox,
        "mark_sent_projection_state",
        _mark_sent_projection_state,
        raising=False,
    )
    monkeypatch.setattr(
        agent_tools.mailbox, "mark_sent_projected", _mark_sent_projected
    )
    monkeypatch.setattr(
        agent_tools.mailbox, "release_sent_projection", _release_sent_projection
    )
    monkeypatch.setattr(agent_tools.event_log, "append_event", _append_event)

    first_tools = agent_tools.ShadowCloneV2AgentTools(
        context=_context(actor_name="researcher"),
        clock=lambda: "2026-06-02T00:02:00+00:00",
    )
    with pytest.raises(RuntimeError, match="store failed"):
        await first_tools.send_message(
            recipient="reviewer",
            text="Wake.",
            summary="Wake",
            idempotency_key="wake-retry",
        )

    second_tools = agent_tools.ShadowCloneV2AgentTools(
        context=_context(actor_name="researcher"),
        clock=lambda: "2026-06-02T00:03:00+00:00",
    )
    await second_tools.send_message(
        recipient="reviewer",
        text="Wake.",
        summary="Wake",
        idempotency_key="wake-retry",
    )

    assert [event["event_type"] for event in appended_events] == [
        EventType.MAILBOX_SENT,
        EventType.AGENT_WAKE,
    ]
    assert len(stored_teams) == 1
    assert projection_state["value"] == "sent_done"


@pytest.mark.asyncio
async def test_send_message_tool_retries_wake_event_after_append_failure(
    monkeypatch,
) -> None:
    appended_events = []
    stored_teams = []
    projection_state = {"value": None}
    wake_event_attempts = {"count": 0}
    persisted_team = {"value": None}

    async def _read_team(**_kwargs):
        if persisted_team["value"] is not None:
            return persisted_team["value"]
        team = _team()
        return team.model_copy(
            update={
                "members": [
                    team.members[0],
                    team.members[1].model_copy(
                        update={
                            "status": AgentLifecycleStatus.IDLE,
                            "idle_since": "2026-06-02T00:00:00+00:00",
                            "idle_expires_at": "2026-06-02T00:20:00+00:00",
                        }
                    ),
                ]
            }
        )

    async def _send_message(**kwargs):
        message = agent_tools.MailboxMessage(
            id="1-0",
            run_id=kwargs["run_id"],
            thread_id=kwargs["thread_id"],
            project_id=kwargs["project_id"],
            sender=kwargs["sender"],
            recipient=kwargs["recipient"],
            kind=kwargs["kind"],
            type=kwargs["message_type"],
            idempotency_key=kwargs["idempotency_key"],
            summary=kwargs["summary"],
            text=kwargs["text"],
            payload=kwargs["payload"],
            created_at="2026-06-02T00:01:00+00:00",
        )
        object.__setattr__(message, "_was_created", False)
        object.__setattr__(
            message, "_sent_projected", projection_state["value"] == "sent_done"
        )
        object.__setattr__(message, "_sent_projection_state", projection_state["value"])
        return message

    async def _claim_sent_projection(_message):
        return True

    async def _mark_sent_projection_state(_message, state):
        projection_state["value"] = state

    async def _mark_sent_projected(_message):
        projection_state["value"] = "sent_done"

    async def _release_sent_projection(_message):
        return None

    async def _store_team(team, *, ttl_seconds, timeout=None):
        stored_teams.append(team)
        persisted_team["value"] = team

    async def _append_event(**kwargs):
        if kwargs["event_type"] == EventType.AGENT_WAKE:
            wake_event_attempts["count"] += 1
            if wake_event_attempts["count"] == 1:
                raise RuntimeError("wake event failed")
        appended_events.append(kwargs)
        return object()

    monkeypatch.setattr(agent_tools.team_store, "read_team", _read_team)
    monkeypatch.setattr(agent_tools.team_store, "store_team", _store_team)
    monkeypatch.setattr(agent_tools.mailbox, "send_message", _send_message)
    monkeypatch.setattr(
        agent_tools.mailbox, "claim_sent_projection", _claim_sent_projection
    )
    monkeypatch.setattr(
        agent_tools.mailbox,
        "mark_sent_projection_state",
        _mark_sent_projection_state,
        raising=False,
    )
    monkeypatch.setattr(
        agent_tools.mailbox, "mark_sent_projected", _mark_sent_projected
    )
    monkeypatch.setattr(
        agent_tools.mailbox, "release_sent_projection", _release_sent_projection
    )
    monkeypatch.setattr(agent_tools.event_log, "append_event", _append_event)

    first_tools = agent_tools.ShadowCloneV2AgentTools(
        context=_context(actor_name="researcher"),
        clock=lambda: "2026-06-02T00:02:00+00:00",
    )
    with pytest.raises(RuntimeError, match="wake event failed"):
        await first_tools.send_message(
            recipient="reviewer",
            text="Wake.",
            summary="Wake",
            idempotency_key="wake-event-retry",
        )

    second_tools = agent_tools.ShadowCloneV2AgentTools(
        context=_context(actor_name="researcher"),
        clock=lambda: "2026-06-02T00:03:00+00:00",
    )
    await second_tools.send_message(
        recipient="reviewer",
        text="Wake.",
        summary="Wake",
        idempotency_key="wake-event-retry",
    )

    assert [event["event_type"] for event in appended_events] == [
        EventType.MAILBOX_SENT,
        EventType.AGENT_WAKE,
    ]
    assert len(stored_teams) == 1
    assert projection_state["value"] == "sent_done"


@pytest.mark.asyncio
async def test_message_read_tool_passes_account_scope_to_mailbox(monkeypatch) -> None:
    read_calls = []

    async def _read_next_messages_with_dead_letters(**kwargs):
        read_calls.append(kwargs)
        return agent_tools.mailbox.MailboxReadResult(messages=[], dead_lettered=[])

    monkeypatch.setattr(
        agent_tools.mailbox,
        "read_next_messages_with_dead_letters",
        _read_next_messages_with_dead_letters,
    )

    tools = agent_tools.ShadowCloneV2AgentTools(
        context=_context(actor_name="reviewer"),
        clock=lambda: "2026-06-02T00:03:00+00:00",
    )

    result = await tools.message_read(count=3)

    assert result.messages == []
    assert read_calls == [
        {
            "project_id": "project-1",
            "thread_id": "thread-1",
            "account_id": "account-1",
            "agent_name": "reviewer",
            "count": 3,
        }
    ]


@pytest.mark.asyncio
async def test_message_ack_tool_appends_projection_event(monkeypatch) -> None:
    appended_events = []

    async def _ack_message(**kwargs):
        assert kwargs["account_id"] == "account-1"
        return agent_tools.mailbox.MailboxAckResult(
            project_id=kwargs["project_id"],
            thread_id=kwargs["thread_id"],
            agent_name=kwargs["agent_name"],
            message_id=kwargs["message_id"],
            acked_by=kwargs["acked_by"],
            acked_at=kwargs["acked_at"],
            read_at=kwargs["acked_at"],
            already_acked=False,
        )

    async def _append_event(**kwargs):
        appended_events.append(kwargs)
        return object()

    monkeypatch.setattr(agent_tools.mailbox, "ack_message", _ack_message)
    monkeypatch.setattr(agent_tools.event_log, "append_event", _append_event)

    tools = agent_tools.ShadowCloneV2AgentTools(
        context=_context(actor_name="reviewer"),
        clock=lambda: "2026-06-02T00:03:00+00:00",
    )

    result = await tools.message_ack(message_id="1-0")

    assert result.ack.message_id == "1-0"
    assert result.next_sequence == 11
    assert appended_events[0]["event_type"] == EventType.MAILBOX_ACKED
    assert appended_events[0]["payload"] == {
        "message_id": "1-0",
        "agent_name": "reviewer",
        "acked_by": "reviewer",
        "already_acked": False,
    }


@pytest.mark.asyncio
async def test_message_dead_letter_tool_appends_projection_event(monkeypatch) -> None:
    appended_events = []
    message = agent_tools.MailboxMessage(
        id="1-0",
        run_id="run-1",
        thread_id="thread-1",
        project_id="project-1",
        sender="researcher",
        recipient="reviewer",
        kind=MessageKind.TEXT,
        type=MessageType.TEXT,
        idempotency_key="researcher-reviewer-1",
        text="Poison.",
        created_at="2026-06-02T00:01:00+00:00",
    )

    async def _get_message(**kwargs):
        assert kwargs["account_id"] == "account-1"
        assert kwargs["agent_name"] == "reviewer"
        assert kwargs["message_id"] == "1-0"
        return message

    async def _dead_letter_message(**kwargs):
        return kwargs["message"].model_copy(
            update={
                "dead_letter_reason": kwargs["reason"],
                "dead_lettered_at": kwargs["dead_lettered_at"],
            }
        )

    async def _append_event(**kwargs):
        appended_events.append(kwargs)
        return object()

    monkeypatch.setattr(agent_tools.mailbox, "get_message", _get_message)
    monkeypatch.setattr(
        agent_tools.mailbox, "dead_letter_message", _dead_letter_message
    )
    monkeypatch.setattr(agent_tools.event_log, "append_event", _append_event)

    tools = agent_tools.ShadowCloneV2AgentTools(
        context=_context(actor_name="reviewer"),
        clock=lambda: "2026-06-02T00:03:00+00:00",
    )

    result = await tools.message_dead_letter(
        message_id="1-0",
        reason="handler_error",
    )

    assert result.message.dead_letter_reason == "handler_error"
    assert result.next_sequence == 11
    assert appended_events[0]["event_type"] == EventType.MAILBOX_DEAD_LETTERED
    assert appended_events[0]["payload"] == {
        "message_id": "1-0",
        "agent_name": "reviewer",
        "reason": "handler_error",
    }


@pytest.mark.asyncio
async def test_message_dead_letter_tool_does_not_duplicate_event(monkeypatch) -> None:
    appended_events = []
    message = agent_tools.MailboxMessage(
        id="1-0",
        run_id="run-1",
        thread_id="thread-1",
        project_id="project-1",
        sender="researcher",
        recipient="reviewer",
        kind=MessageKind.TEXT,
        type=MessageType.TEXT,
        idempotency_key="researcher-reviewer-1",
        text="Poison.",
        created_at="2026-06-02T00:01:00+00:00",
    )
    already_dead_lettered = message.model_copy(
        update={
            "dead_letter_reason": "handler_error",
            "dead_lettered_at": "2026-06-02T00:02:00+00:00",
        }
    )
    object.__setattr__(already_dead_lettered, "_was_dead_lettered", False)

    async def _get_message(**_kwargs):
        return message

    async def _dead_letter_message(**_kwargs):
        return already_dead_lettered

    async def _append_event(**kwargs):
        appended_events.append(kwargs)
        return object()

    monkeypatch.setattr(agent_tools.mailbox, "get_message", _get_message)
    monkeypatch.setattr(
        agent_tools.mailbox, "dead_letter_message", _dead_letter_message
    )
    monkeypatch.setattr(agent_tools.event_log, "append_event", _append_event)

    tools = agent_tools.ShadowCloneV2AgentTools(
        context=_context(actor_name="reviewer"),
        clock=lambda: "2026-06-02T00:03:00+00:00",
    )

    result = await tools.message_dead_letter(
        message_id="1-0",
        reason="handler_error",
    )

    assert result.message.dead_letter_reason == "handler_error"
    assert result.next_sequence == 10
    assert appended_events == []


@pytest.mark.asyncio
async def test_send_message_tool_rejects_unknown_recipient_before_mailbox_write(
    monkeypatch,
) -> None:
    sent_messages = []

    async def _read_team(**_kwargs):
        return _team()

    async def _send_message(**kwargs):
        sent_messages.append(kwargs)
        raise AssertionError("mailbox write should not be reached")

    monkeypatch.setattr(agent_tools.team_store, "read_team", _read_team)
    monkeypatch.setattr(agent_tools.mailbox, "send_message", _send_message)

    tools = agent_tools.ShadowCloneV2AgentTools(
        context=_context(actor_name="researcher")
    )

    with pytest.raises(ValueError, match="recipient is not a member"):
        await tools.send_message(
            recipient="not-on-team",
            text="Can you see this?",
            summary="Invalid peer",
            idempotency_key="invalid-recipient-1",
        )

    assert sent_messages == []


@pytest.mark.asyncio
async def test_team_create_tool_rejects_existing_team_facilitator_takeover(
    monkeypatch,
) -> None:
    stored_teams = []

    async def _read_team(**_kwargs):
        return _team()

    async def _store_team(team, *, ttl_seconds, timeout=None):
        stored_teams.append(team)

    monkeypatch.setattr(agent_tools.team_store, "read_team", _read_team)
    monkeypatch.setattr(agent_tools.team_store, "store_team", _store_team)

    tools = agent_tools.ShadowCloneV2AgentTools(
        context=_context(actor_name="researcher")
    )

    with pytest.raises(ValueError, match="only the team facilitator can replace"):
        await tools.team_create(
            members=[{"agent_name": "new-reviewer", "role": "quality reviewer"}]
        )

    assert stored_teams == []


@pytest.mark.asyncio
async def test_team_create_tool_rejects_cross_account_existing_team_reuse(
    monkeypatch,
) -> None:
    stored_teams = []
    appended_events = []
    existing_team = _team().model_copy(update={"account_id": "account-other"})

    async def _read_team(**_kwargs):
        return existing_team

    async def _store_team(team, *, ttl_seconds, timeout=None):
        stored_teams.append((team, ttl_seconds, timeout))

    async def _append_event(**kwargs):
        appended_events.append(kwargs)
        return object()

    monkeypatch.setattr(agent_tools.team_store, "read_team", _read_team)
    monkeypatch.setattr(agent_tools.team_store, "store_team", _store_team)
    monkeypatch.setattr(agent_tools.event_log, "append_event", _append_event)

    tools = agent_tools.ShadowCloneV2AgentTools(context=_context())

    with pytest.raises(
        ValueError, match="existing team belongs to a different account"
    ):
        await tools.team_create(
            members=[{"agent_name": "researcher", "role": "research analyst"}]
        )

    assert stored_teams == []
    assert appended_events == []


@pytest.mark.asyncio
async def test_task_create_tool_rejects_duplicate_task_without_overwrite(
    monkeypatch,
) -> None:
    created_tasks = []
    appended_events = []

    async def _create_task(task, *, ttl_seconds, timeout=None):
        created_tasks.append(task)
        return False

    async def _append_event(**kwargs):
        appended_events.append(kwargs)
        return object()

    monkeypatch.setattr(agent_tools.task_pool, "create_task", _create_task)
    monkeypatch.setattr(agent_tools.event_log, "append_event", _append_event)

    tools = agent_tools.ShadowCloneV2AgentTools(
        context=_context(actor_name="researcher")
    )

    with pytest.raises(ValueError, match="task already exists"):
        await tools.task_create(
            task_id="task-1",
            subject="Duplicate",
            description="Should not overwrite existing task.",
        )

    assert len(created_tasks) == 1
    assert appended_events == []


@pytest.mark.asyncio
async def test_send_message_tool_broadcast_fans_out_to_all_other_team_members(
    monkeypatch,
) -> None:
    sent_messages = []
    appended_events = []
    team = _team().model_copy(
        update={
            "members": [
                *_team().members,
                AgentIdentity(
                    agent_id="writer@thread-1",
                    agent_name="writer",
                    thread_id="thread-1",
                    project_id="project-1",
                    account_id="account-1",
                    role="markdown writer",
                    status=AgentLifecycleStatus.IDLE,
                    idle_since="2026-06-02T00:00:00+00:00",
                    idle_expires_at="2026-06-02T00:20:00+00:00",
                ),
            ]
        }
    )

    async def _read_team(**_kwargs):
        return team

    async def _store_team(*_args, **_kwargs):
        return None

    async def _send_message(**kwargs):
        sent_messages.append(kwargs)
        return agent_tools.MailboxMessage(
            id=f"{len(sent_messages)}-0",
            run_id=kwargs["run_id"],
            thread_id=kwargs["thread_id"],
            project_id=kwargs["project_id"],
            sender=kwargs["sender"],
            recipient=kwargs["recipient"],
            kind=kwargs["kind"],
            type=kwargs["message_type"],
            idempotency_key=kwargs["idempotency_key"],
            summary=kwargs["summary"],
            text=kwargs["text"],
            payload=kwargs["payload"],
            created_at=kwargs["created_at"],
        )

    async def _append_event(**kwargs):
        appended_events.append(kwargs)
        return object()

    monkeypatch.setattr(agent_tools.team_store, "read_team", _read_team)
    monkeypatch.setattr(agent_tools.team_store, "store_team", _store_team)
    monkeypatch.setattr(agent_tools.mailbox, "send_message", _send_message)
    monkeypatch.setattr(agent_tools.event_log, "append_event", _append_event)

    tools = agent_tools.ShadowCloneV2AgentTools(
        context=_context(actor_name="researcher"),
        clock=lambda: "2026-06-02T00:02:00+00:00",
    )

    result = await tools.send_message(
        recipient="*",
        text="Everybody pivot to the GPU-memory story angle.",
        summary="Broadcast pivot",
        idempotency_key="broadcast-pivot-1",
        message_type=MessageType.BROADCAST,
    )

    assert result.message.recipient == "team-lead"
    assert [message["recipient"] for message in sent_messages] == [
        "reviewer",
        "writer",
        "team-lead",
    ]
    assert [message["message_type"] for message in sent_messages] == [
        MessageType.BROADCAST,
        MessageType.BROADCAST,
        MessageType.BROADCAST,
    ]
    assert [message["idempotency_key"] for message in sent_messages] == [
        "broadcast-pivot-1__reviewer",
        "broadcast-pivot-1__writer",
        "broadcast-pivot-1__team-lead",
    ]
    sent_events = [
        event for event in appended_events if event["event_type"] == EventType.MAILBOX_SENT
    ]
    wake_events = [
        event for event in appended_events if event["event_type"] == EventType.AGENT_WAKE
    ]
    assert [event["payload"]["recipient"] for event in sent_events] == [
        "reviewer",
        "writer",
        "team-lead",
    ]
    assert [event["payload"]["message_type"] for event in sent_events] == [
        "broadcast",
        "broadcast",
        "broadcast",
    ]
    assert [event["payload"]["text"] for event in sent_events] == [
        "Everybody pivot to the GPU-memory story angle.",
        "Everybody pivot to the GPU-memory story angle.",
        "Everybody pivot to the GPU-memory story angle.",
    ]
    assert [event["payload"].get("payload") for event in sent_events] == [{}, {}, {}]
    assert [event["payload"]["agent_name"] for event in wake_events] == ["writer"]
    assert result.next_sequence == 14


@pytest.mark.asyncio
async def test_send_message_broadcast_persists_all_idle_recipient_wakes(
    monkeypatch,
) -> None:
    stored_teams = []
    base_team = _team()
    team = base_team.model_copy(
        update={
            "members": [
                member.model_copy(
                    update={
                        "status": AgentLifecycleStatus.IDLE,
                        "idle_since": "2026-06-02T00:00:00+00:00",
                        "idle_expires_at": "2026-06-02T00:20:00+00:00",
                    }
                )
                for member in base_team.members
            ]
        }
    )

    async def _read_team(**_kwargs):
        return team

    async def _store_team(next_team, *, ttl_seconds, timeout=None):
        stored_teams.append(next_team)

    async def _send_message(**kwargs):
        return agent_tools.MailboxMessage(
            id=f"{kwargs['recipient']}-1",
            run_id=kwargs["run_id"],
            thread_id=kwargs["thread_id"],
            project_id=kwargs["project_id"],
            account_id=kwargs["account_id"],
            sender=kwargs["sender"],
            recipient=kwargs["recipient"],
            kind=kwargs["kind"],
            type=kwargs["message_type"],
            idempotency_key=kwargs["idempotency_key"],
            summary=kwargs["summary"],
            text=kwargs["text"],
            payload=kwargs["payload"],
            created_at=kwargs["created_at"],
        )

    async def _append_event(**_kwargs):
        return object()

    monkeypatch.setattr(agent_tools.team_store, "read_team", _read_team)
    monkeypatch.setattr(agent_tools.team_store, "store_team", _store_team)
    monkeypatch.setattr(agent_tools.mailbox, "send_message", _send_message)
    monkeypatch.setattr(agent_tools.event_log, "append_event", _append_event)

    tools = agent_tools.ShadowCloneV2AgentTools(
        context=_context(actor_name="team-lead"),
        clock=lambda: "2026-06-02T00:02:00+00:00",
    )

    await tools.send_message(
        recipient="*",
        text="All agents continue with the revised direction.",
        summary="Broadcast revision",
        idempotency_key="broadcast-all-idle-1",
        message_type=MessageType.BROADCAST,
    )

    assert stored_teams
    final_members = {member.agent_name: member for member in stored_teams[-1].members}
    assert final_members["researcher"].status == AgentLifecycleStatus.WORKING
    assert final_members["reviewer"].status == AgentLifecycleStatus.WORKING
    assert final_members["researcher"].idle_since is None
    assert final_members["reviewer"].idle_since is None


@pytest.mark.asyncio
async def test_registered_send_message_tool_accepts_broadcast_message_type(
    monkeypatch,
) -> None:
    captured = {}

    class _FakeToolkit:
        def __init__(self) -> None:
            self.tools = {}

        def register_tool_function(self, func):
            self.tools[func.__name__] = func

    async def _send_message(**kwargs):
        captured["send_message_kwargs"] = kwargs
        return agent_tools.MessageToolResult(
            message=agent_tools.MailboxMessage(
                id="broadcast-last",
                run_id="run-1",
                thread_id="thread-1",
                project_id="project-1",
                account_id="account-1",
                sender="team-lead",
                recipient="reviewer",
                kind=MessageKind.TEXT,
                type=MessageType.BROADCAST,
                idempotency_key="broadcast-wrapper-1__reviewer",
                summary="Broadcast",
                text="Broadcast this.",
                payload={},
                created_at="2026-06-02T00:02:00+00:00",
            ),
            next_sequence=11,
        )

    tools = agent_tools.ShadowCloneV2AgentTools(context=_context())
    monkeypatch.setattr(tools, "send_message", _send_message)

    toolkit = _FakeToolkit()
    agent_tools.register_agent_tools(
        toolkit,
        context=_context(),
        tools=tools,
    )

    result = await toolkit.tools["send_message"](
        recipient="*",
        text="Broadcast this.",
        summary="Broadcast",
        idempotency_key="broadcast-wrapper-1",
        message_type="broadcast",
    )

    assert captured["send_message_kwargs"]["message_type"] == MessageType.BROADCAST
    assert "broadcast-last" in str(result)


@pytest.mark.asyncio
async def test_send_message_tool_enforces_boundary_sizes_and_json_payload(
    monkeypatch,
) -> None:
    async def _read_team(**_kwargs):
        return _team()

    monkeypatch.setattr(agent_tools.team_store, "read_team", _read_team)
    tools = agent_tools.ShadowCloneV2AgentTools(
        context=_context(actor_name="researcher")
    )

    with pytest.raises(ValueError, match="text exceeds"):
        await tools.send_message(
            recipient="reviewer",
            text="x" * (agent_tools.MAX_MESSAGE_TEXT_CHARS + 1),
            summary="Too large",
            idempotency_key="too-large-1",
        )

    with pytest.raises(ValueError, match="payload must be JSON serializable"):
        await tools.send_message(
            recipient="reviewer",
            text="Normal text",
            summary="Bad payload",
            idempotency_key="bad-payload-1",
            payload={"bad": object()},
        )


@pytest.mark.asyncio
async def test_tool_ids_reject_redis_key_delimiters_and_globs(monkeypatch) -> None:
    async def _store_task(*_args, **_kwargs):
        raise AssertionError("invalid task id should fail before persistence")

    monkeypatch.setattr(
        agent_tools.task_pool, "create_task", _store_task, raising=False
    )
    tools = agent_tools.ShadowCloneV2AgentTools(
        context=_context(actor_name="researcher")
    )

    with pytest.raises(ValueError, match="task_id must not contain"):
        await tools.task_create(
            task_id="task:bad",
            subject="Invalid",
            description="Invalid key delimiter.",
        )

    with pytest.raises(ValueError, match="agent_name must not contain"):
        await tools.team_create(
            members=[{"agent_name": "bad*agent", "role": "reviewer"}]
        )


@pytest.mark.asyncio
async def test_concurrent_task_create_tools_allocate_unique_event_sequences(
    monkeypatch,
) -> None:
    import asyncio

    appended_events = []
    created_tasks = []

    async def _create_task(task, *, ttl_seconds, timeout=None):
        await asyncio.sleep(0)
        created_tasks.append(task)
        return True

    async def _append_event(**kwargs):
        await asyncio.sleep(0)
        appended_events.append(kwargs)
        return object()

    monkeypatch.setattr(agent_tools.task_pool, "create_task", _create_task)
    monkeypatch.setattr(agent_tools.event_log, "append_event", _append_event)

    tools = agent_tools.ShadowCloneV2AgentTools(
        context=_context(actor_name="researcher").__class__(
            run_id="run-1",
            thread_id="thread-1",
            project_id="project-1",
            actor_name="researcher",
            sequence_start=10,
            state_ttl_seconds=3600,
        ),
        clock=lambda: "2026-06-03T00:00:00+00:00",
    )

    await asyncio.gather(
        tools.task_create(
            task_id="task-a",
            subject="A",
            description="First concurrent task.",
        ),
        tools.task_create(
            task_id="task-b",
            subject="B",
            description="Second concurrent task.",
        ),
    )

    assert sorted(task.id for task in created_tasks) == ["task-a", "task-b"]
    assert [event["sequence"] for event in appended_events] == [10, 11]
    assert tools.next_sequence == 12


def test_register_agent_tools_can_use_existing_tool_state_for_sequence_cursor() -> None:
    registered = {}
    tools = agent_tools.ShadowCloneV2AgentTools(context=_context())

    class _Toolkit:
        def register_tool_function(self, func):
            registered[func.__name__] = func

    returned = agent_tools.register_agent_tools(
        _Toolkit(),
        context=_context(),
        tools=tools,
    )

    assert returned is tools
    assert set(registered) == {
        "team_create",
        "team_shutdown",
        "task_create",
        "task_get",
        "task_update",
        "task_list",
        "send_message",
        "message_read",
        "message_ack",
        "message_dead_letter",
    }
