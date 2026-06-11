from __future__ import annotations

import pytest

from agentscope_integration.shadow_clone_v2.keys import (
    agent_mailbox_key,
    run_events_key,
    thread_team_key,
)
from agentscope_integration.shadow_clone_v2.models import EventRecord, EventType


def test_v2_keys_are_thread_and_run_scoped_without_colliding_with_v1() -> None:
    assert run_events_key("run-1") == "sc_v2:run:run-1:events"
    assert thread_team_key(project_id="project-1", thread_id="thread-1") == (
        "sc_v2:project:project-1:thread:thread-1:team"
    )
    assert agent_mailbox_key(
        project_id="project-1",
        thread_id="thread-1",
        agent_name="reviewer",
        lane="control",
    ) == "sc_v2:project:project-1:thread:thread-1:mailbox:reviewer:control"


def test_key_builders_reject_empty_identity_parts() -> None:
    with pytest.raises(ValueError, match="run_id"):
        run_events_key("")
    with pytest.raises(ValueError, match="agent_name"):
        agent_mailbox_key(
            project_id="project-1",
            thread_id="thread-1",
            agent_name="",
            lane="control",
        )


def test_event_record_uses_safe_payload_defaults() -> None:
    first = EventRecord(
        id="event-1",
        run_id="run-1",
        thread_id="thread-1",
        project_id="project-1",
        sequence=1,
        type=EventType.AGENT_IDLE,
        created_at="2026-05-31T00:00:00Z",
    )
    second = EventRecord(
        id="event-2",
        run_id="run-1",
        thread_id="thread-1",
        project_id="project-1",
        sequence=2,
        type=EventType.AGENT_WAKE,
        created_at="2026-05-31T00:00:01Z",
    )

    first.payload["agent"] = "reviewer"

    assert second.payload == {}

from agentscope_integration.shadow_clone_v2.keys import thread_team_key


def test_thread_team_key_is_project_and_thread_scoped() -> None:
    assert thread_team_key(project_id="project-1", thread_id="thread-1") == (
        "sc_v2:project:project-1:thread:thread-1:team"
    )
