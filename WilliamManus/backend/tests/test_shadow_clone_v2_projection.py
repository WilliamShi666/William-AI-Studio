from __future__ import annotations

from types import SimpleNamespace

from agentscope_integration.shadow_clone_v2.models import EventType
from agentscope_integration.shadow_clone_v2.projection import (
    project_shadow_clone_v2_full_result,
    project_shadow_clone_v2_public_state,
    project_shadow_clone_v2_results,
)


def _event(
    sequence: int,
    event_type: EventType | str,
    payload: dict,
    *,
    created_at: str | None = None,
    event_id: str | None = None,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=event_id or f"{sequence}-0",
        sequence=sequence,
        type=event_type,
        payload=payload,
        created_at=created_at or f"2026-06-02T00:{sequence:02d}:00+00:00",
    )


def test_v2_projection_replay_is_deterministic_and_sorts_by_sequence() -> None:
    events = [
        _event(
            3, EventType.TASK_COMPLETED, {"task_id": "task-1", "result_ref": "done"}
        ),
        _event(
            1,
            EventType.TASK_CREATED,
            {
                "id": "task-1",
                "subject": "Analyst",
                "description": "Analyze.",
                "metadata": {"agent_name": "analyst"},
            },
        ),
        _event(
            2,
            EventType.TASK_CLAIMED,
            {
                "task_id": "task-1",
                "agent_name": "analyst",
                "lease_expires_at": "2026-06-02T00:20:00+00:00",
                "attempt": 1,
                "new_version": 2,
            },
        ),
    ]

    first = project_shadow_clone_v2_public_state(
        agent_run_id="run-1", events=events, parent_status="running"
    )
    second = project_shadow_clone_v2_public_state(
        agent_run_id="run-1", events=list(reversed(events)), parent_status="running"
    )

    assert first == second
    assert first["tasks"]["task-1"]["status"] == "completed"
    assert first["subagents"]["task-1"]["status"] == "completed"


def test_v2_projection_projects_completed_task_output_as_subagent_transcript() -> None:
    events = [
        _event(1, EventType.RUN_STARTED, {"shadow_clone_mode": "v2"}),
        _event(
            2,
            EventType.TEAM_CREATED,
            {
                "team_id": "team-1",
                "members": [
                    {
                        "agent_id": "storyteller@thread-1",
                        "agent_name": "storyteller",
                        "role": "GPU storyteller",
                        "status": "starting",
                    }
                ],
            },
        ),
        _event(
            3,
            EventType.TASK_CREATED,
            {
                "id": "gpu-story-1",
                "subject": "Write GPU story",
                "description": "Write the markdown file.",
                "metadata": {"agent_name": "storyteller"},
            },
        ),
        _event(
            4,
            EventType.TASK_COMPLETED,
            {
                "task_id": "gpu-story-1",
                "agent_name": "storyteller",
                "result_ref": "TEAM_MEMBER_GPU_STORY_MARKER raw markdown content",
            },
        ),
    ]

    state = project_shadow_clone_v2_public_state(
        agent_run_id="run-1", events=events, parent_status="completed"
    )

    assert state["transcripts"] == [
        {
            "message_id": "task-completed:gpu-story-1:4",
            "task_id": "gpu-story-1",
            "agent_name": "storyteller",
            "role": "assistant",
            "content": "TEAM_MEMBER_GPU_STORY_MARKER raw markdown content",
            "stream_status": "complete",
            "source_event_type": "task_completed",
            "created_at": "2026-06-02T00:04:00+00:00",
        }
    ]


def test_v2_projection_projects_team_task_update_agent_wake_and_mailbox() -> None:
    events = [
        _event(
            1,
            EventType.RUN_STARTED,
            {
                "model_key": "frontend-model",
                "effective_model": "frontend-model",
                "shadow_clone_mode": "v2",
            },
        ),
        _event(
            2,
            EventType.TEAM_CREATED,
            {
                "team_id": "team-1",
                "facilitator_id": "facilitator@thread-1",
                "members": [
                    {
                        "agent_id": "analyst@thread-1",
                        "agent_name": "analyst",
                        "role": "Analyst",
                        "status": "starting",
                    }
                ],
            },
        ),
        _event(
            3,
            EventType.TASK_CREATED,
            {
                "id": "task-1",
                "subject": "Analyst",
                "description": "Analyze.",
                "status": "pending",
                "metadata": {"agent_name": "analyst", "phase": "draft"},
                "blocked_by": [],
                "blocks": [],
                "plan_revision": 1,
            },
        ),
        _event(
            4,
            EventType.TASK_UPDATED,
            {
                "task_id": "task-1",
                "status": "blocked",
                "description": "Wait for source document.",
                "metadata": {"phase": "waiting"},
                "blocked_by": ["task-0"],
                "previous_version": 1,
                "new_version": 2,
                "change_reason": "dependency_added",
            },
        ),
        _event(
            5,
            EventType.MAILBOX_SENT,
            {
                "message_id": "msg-1",
                "sender": "facilitator",
                "recipient": "analyst",
                "kind": "control",
                "message_type": "wake",
                "summary": "Source document is ready.",
                "text": "Read the private source-ready token SOURCE_READY_ONLY_IN_MAILBOX.",
                "payload": {"handoff": "source-ready"},
            },
        ),
        _event(
            6,
            EventType.AGENT_WAKE,
            {
                "agent_name": "analyst",
                "wake_reason": "mailbox_unread",
                "message_id": "msg-1",
            },
        ),
        _event(
            7,
            EventType.MAILBOX_ACKED,
            {
                "message_id": "msg-1",
                "agent_name": "analyst",
                "acked_by": "analyst",
            },
        ),
    ]

    state = project_shadow_clone_v2_public_state(
        agent_run_id="run-1", events=events, parent_status="running"
    )

    assert state["team"]["team_id"] == "team-1"
    assert state["agents"]["analyst"]["status"] == "working"
    assert state["agents"]["analyst"]["wake_reason"] == "mailbox_unread"
    assert state["tasks"]["task-1"]["status"] == "blocked"
    assert state["tasks"]["task-1"]["description"] == "Wait for source document."
    assert state["tasks"]["task-1"]["blocked_by"] == ["task-0"]
    assert state["tasks"]["task-1"]["version"] == 2
    assert state["messages"]["msg-1"]["status"] == "acked"
    assert state["messages"]["msg-1"]["recipient"] == "analyst"
    assert (
        state["messages"]["msg-1"]["text"]
        == "Read the private source-ready token SOURCE_READY_ONLY_IN_MAILBOX."
    )
    assert state["messages"]["msg-1"]["payload"] == {"handoff": "source-ready"}
    assert state["model"] == {
        "requested": "frontend-model",
        "effective": "frontend-model",
    }


def test_v2_projection_team_created_replays_idle_member_lifecycle_snapshot() -> None:
    events = [
        _event(
            1,
            EventType.AGENT_SPAWNED,
            {
                "agent_id": "reviewer@thread-1",
                "agent_name": "reviewer",
                "role": "old reviewer role",
                "status": "starting",
            },
        ),
        _event(
            2,
            EventType.TEAM_CREATED,
            {
                "team_id": "team-1",
                "facilitator_id": "facilitator@thread-1",
                "account_id": "account-1",
                "members": [
                    {
                        "agent_id": "reviewer@thread-1",
                        "agent_name": "reviewer",
                        "account_id": "account-1",
                        "role": "quality reviewer",
                        "status": "idle",
                        "current_run_id": "previous-run",
                        "idle_since": "2026-06-02T00:00:00+00:00",
                        "idle_expires_at": "2026-06-02T00:20:00+00:00",
                        "last_handoff_summary": "Waiting for follow-up.",
                        "metadata": {"preserved": True},
                    }
                ],
            },
        ),
    ]

    state = project_shadow_clone_v2_public_state(
        agent_run_id="run-1", events=events, parent_status="running"
    )

    agent = state["agents"]["reviewer"]
    assert agent["role"] == "quality reviewer"
    assert agent["status"] == "idle"
    assert agent["account_id"] == "account-1"
    assert agent["current_run_id"] == "previous-run"
    assert agent["idle_since"] == "2026-06-02T00:00:00+00:00"
    assert agent["idle_expires_at"] == "2026-06-02T00:20:00+00:00"
    assert agent["last_handoff_summary"] == "Waiting for follow-up."
    assert agent["metadata"] == {"preserved": True}
    assert state["team"]["members"]["reviewer"]["idle_expires_at"] == (
        "2026-06-02T00:20:00+00:00"
    )
    assert state["team"]["account_id"] == "account-1"


def test_v2_projection_agent_wake_replays_stable_identity_and_role() -> None:
    events = [
        _event(1, EventType.RUN_STARTED, {"shadow_clone_mode": "v2"}),
        _event(
            2,
            EventType.TEAM_CREATED,
            {
                "team_id": "team-1",
                "facilitator_id": "facilitator@thread-1",
                "member_count": 1,
            },
        ),
        _event(
            3,
            EventType.AGENT_WAKE,
            {
                "agent_id": "researcher@thread-1",
                "agent_name": "researcher",
                "role": "research analyst",
                "status": "working",
                "wake_reason": "team_create_reuse",
                "previous_status": "idle",
                "previous_run_id": "previous-run",
                "idle_since": "2026-06-02T00:00:00+00:00",
                "idle_expires_at": "2026-06-02T00:20:00+00:00",
            },
        ),
    ]

    state = project_shadow_clone_v2_public_state(
        agent_run_id="run-1", events=events, parent_status="running"
    )

    agent = state["agents"]["researcher"]
    assert agent["agent_id"] == "researcher@thread-1"
    assert agent["role"] == "research analyst"
    assert agent["status"] == "working"
    assert agent["wake_reason"] == "team_create_reuse"
    assert agent["previous_status"] == "idle"
    assert agent["previous_run_id"] == "previous-run"
    assert agent["previous_idle_since"] == "2026-06-02T00:00:00+00:00"
    assert agent["previous_idle_expires_at"] == "2026-06-02T00:20:00+00:00"
    assert agent["idle_since"] is None
    assert agent["idle_expires_at"] is None


def test_v2_projection_agent_wake_overwrites_existing_role() -> None:
    events = [
        _event(
            1,
            EventType.TEAM_CREATED,
            {
                "team_id": "team-1",
                "members": [
                    {
                        "agent_id": "researcher@thread-1",
                        "agent_name": "researcher",
                        "role": "old role",
                        "status": "idle",
                    }
                ],
            },
        ),
        _event(
            2,
            EventType.AGENT_WAKE,
            {
                "agent_id": "researcher@thread-1",
                "agent_name": "researcher",
                "role": "new research role",
                "status": "working",
                "wake_reason": "team_create_reuse",
            },
        ),
    ]

    state = project_shadow_clone_v2_public_state(
        agent_run_id="run-1", events=events, parent_status="running"
    )

    assert state["agents"]["researcher"]["role"] == "new research role"


def test_v2_projection_agent_spawn_replaces_expired_identity_lifecycle() -> None:
    events = [
        _event(
            1,
            EventType.AGENT_SHUTDOWN,
            {
                "agent_id": "old-researcher@thread-1",
                "agent_name": "researcher",
                "role": "research analyst",
                "status": "closed",
                "shutdown_reason": "idle_ttl_expired",
            },
        ),
        _event(
            2,
            EventType.AGENT_SPAWNED,
            {
                "agent_id": "researcher@thread-1",
                "agent_name": "researcher",
                "role": "research analyst",
                "status": "starting",
            },
        ),
    ]

    state = project_shadow_clone_v2_public_state(
        agent_run_id="run-1", events=events, parent_status="running"
    )

    agent = state["agents"]["researcher"]
    assert agent["agent_id"] == "researcher@thread-1"
    assert agent["role"] == "research analyst"
    assert agent["status"] == "starting"
    assert "shutdown_reason" not in agent
    assert "shutdown_at" not in agent
    assert "agent_status" not in agent


def test_v2_projection_agent_spawn_overwrites_existing_role() -> None:
    events = [
        _event(
            1,
            EventType.AGENT_SPAWNED,
            {
                "agent_id": "old-researcher@thread-1",
                "agent_name": "researcher",
                "role": "old role",
                "status": "starting",
            },
        ),
        _event(
            2,
            EventType.AGENT_SPAWNED,
            {
                "agent_id": "researcher@thread-1",
                "agent_name": "researcher",
                "role": "new research role",
                "status": "starting",
            },
        ),
    ]

    state = project_shadow_clone_v2_public_state(
        agent_run_id="run-1", events=events, parent_status="running"
    )

    assert state["agents"]["researcher"]["role"] == "new research role"


def test_v2_projection_projects_dead_letter_run_failure_and_final_output() -> None:
    events = [
        _event(
            1,
            EventType.MAILBOX_SENT,
            {"message_id": "msg-1", "sender": "analyst", "recipient": "reviewer"},
        ),
        _event(
            2,
            EventType.MAILBOX_DEAD_LETTERED,
            {
                "message_id": "msg-1",
                "agent_name": "reviewer",
                "reason": "poison",
                "attempts": 3,
            },
        ),
        _event(
            3,
            EventType.RUN_COMPLETED,
            {
                "executed_task_count": 1,
                "final_output": "Main agent synthesized answer.",
            },
        ),
    ]

    state = project_shadow_clone_v2_public_state(
        agent_run_id="run-1", events=events, parent_status="completed"
    )

    assert state["messages"]["msg-1"]["status"] == "dead_lettered"
    assert state["messages"]["msg-1"]["dead_letter_reason"] == "poison"
    assert state["final_output"] == {
        "content": "Main agent synthesized answer.",
        "source": "run_completed",
        "created_at": "2026-06-02T00:03:00+00:00",
    }
    assert state["status"] == "completed"


def test_v2_projection_results_and_full_result_are_event_derived() -> None:
    events = [
        _event(
            1,
            EventType.TASK_CREATED,
            {
                "id": "task-1",
                "subject": "Analyst",
                "metadata": {"agent_name": "analyst"},
            },
        ),
        _event(
            2,
            EventType.TASK_UPDATED,
            {
                "task_id": "task-1",
                "metadata": {"progress": "drafted"},
                "new_version": 2,
            },
        ),
        _event(
            3,
            EventType.TASK_COMPLETED,
            {
                "task_id": "task-1",
                "agent_name": "analyst",
                "result_ref": "Task result text.",
            },
        ),
        _event(
            4,
            EventType.RUN_COMPLETED,
            {"final_output": "Main answer is separate."},
        ),
    ]

    assert project_shadow_clone_v2_results(events) == [
        {
            "subtask_id": "task-1",
            "role": "Analyst",
            "status": "completed",
            "summary": "Task result text.",
            "submitted_at": "2026-06-02T00:03:00+00:00",
        }
    ]
    assert (
        project_shadow_clone_v2_full_result(events=events, subtask_id="task-1")
        == "Task result text."
    )
    assert project_shadow_clone_v2_full_result(events=events, subtask_id="main") is None


def test_v2_projection_replays_real_tool_task_create_and_update_payloads() -> None:
    events = [
        _event(
            1,
            EventType.TASK_CREATED,
            {
                "task_id": "task-1",
                "subject": "Research",
                "description": "Find evidence.",
                "created_by": "researcher",
                "status": "pending",
                "blocked_by": [],
                "blocks": [],
                "metadata": {"agent_name": "researcher"},
                "owner_agent": None,
                "plan_revision": 1,
                "attempt": 0,
                "max_attempts": 2,
                "task": {
                    "id": "task-1",
                    "task_id": "task-1",
                    "run_id": "run-1",
                    "thread_id": "thread-1",
                    "subject": "Research",
                    "description": "Find evidence.",
                    "status": "pending",
                    "priority": 100,
                    "blocked_by": [],
                    "blocks": [],
                    "owner_agent": None,
                    "lease_expires_at": None,
                    "attempt": 0,
                    "max_attempts": 2,
                    "created_by": "researcher",
                    "plan_revision": 1,
                    "version": 1,
                    "result_ref": None,
                    "error": {},
                    "metadata": {"agent_name": "researcher"},
                    "created_at": "2026-06-02T00:00:00+00:00",
                    "updated_at": "2026-06-02T00:00:00+00:00",
                },
            },
        ),
        _event(
            2,
            EventType.TASK_UPDATED,
            {
                "task_id": "task-1",
                "actor": "researcher",
                "previous_version": 1,
                "new_version": 2,
                "changed_fields": ["description", "metadata", "status"],
                "previous_status": "pending",
                "new_status": "in_progress",
                "blocked_by": [],
                "blocks": [],
                "affected_task_ids": [],
                "affected_tasks": [],
                "task": {
                    "id": "task-1",
                    "task_id": "task-1",
                    "run_id": "run-1",
                    "thread_id": "thread-1",
                    "subject": "Research",
                    "description": "Find stronger evidence.",
                    "status": "in_progress",
                    "priority": 100,
                    "blocked_by": [],
                    "blocks": [],
                    "owner_agent": "researcher",
                    "lease_expires_at": None,
                    "attempt": 0,
                    "max_attempts": 2,
                    "created_by": "researcher",
                    "plan_revision": 2,
                    "version": 2,
                    "result_ref": None,
                    "error": {},
                    "metadata": {"agent_name": "researcher", "progress": "25%"},
                    "created_at": "2026-06-02T00:00:00+00:00",
                    "updated_at": "2026-06-02T00:02:00+00:00",
                },
            },
        ),
    ]

    state = project_shadow_clone_v2_public_state(
        agent_run_id="run-1", events=events, parent_status="running"
    )

    assert state["tasks"]["task-1"]["status"] == "in_progress"
    assert state["tasks"]["task-1"]["description"] == "Find stronger evidence."
    assert state["tasks"]["task-1"]["metadata"]["progress"] == "25%"
    assert state["tasks"]["task-1"]["owner_agent"] == "researcher"
    assert state["tasks"]["task-1"]["version"] == 2
    assert state["subagents"]["task-1"]["status"] == "running"


def test_v2_projection_replays_worker_tool_call_lifecycle() -> None:
    events = [
        _event(
            1,
            EventType.TASK_CREATED,
            {
                "task_id": "task-1",
                "subject": "Write file",
                "description": "Write markdown.",
                "metadata": {"agent_name": "writer"},
            },
        ),
        _event(
            2,
            EventType.TOOL_CALL_STARTED,
            {
                "tool_call_id": "tool-1",
                "tool_name": "write_file",
                "agent_name": "writer",
                "task_id": "task-1",
                "arguments": {"path": "/workspace/story.md"},
            },
        ),
        _event(
            3,
            EventType.TOOL_CALL_COMPLETED,
            {
                "tool_call_id": "tool-1",
                "tool_name": "write_file",
                "agent_name": "writer",
                "task_id": "task-1",
                "result_summary": "wrote /workspace/story.md",
            },
        ),
    ]

    state = project_shadow_clone_v2_public_state(
        agent_run_id="run-1",
        events=events,
        parent_status="running",
    )

    tool_call = state["tool_calls"]["tool-1"]
    assert tool_call["status"] == "completed"
    assert tool_call["tool_name"] == "write_file"
    assert tool_call["agent_name"] == "writer"
    assert tool_call["task_id"] == "task-1"
    assert tool_call["arguments"]["path"] == "/workspace/story.md"
    assert tool_call["result_summary"] == "wrote /workspace/story.md"
    assert state["tasks"]["task-1"]["tool_call_ids"] == ["tool-1"]
    assert state["agents"]["writer"]["tool_call_ids"] == ["tool-1"]


def test_v2_projection_replays_worker_tool_call_argument_summary() -> None:
    events = [
        _event(
            1,
            EventType.TOOL_CALL_STARTED,
            {
                "tool_call_id": "tool-1",
                "tool_name": "write_file",
                "agent_name": "writer",
                "task_id": "task-1",
                "arguments_summary": {"path": "/workspace/story.md"},
                "arguments_redacted": True,
                "arguments_size_bytes": 1234,
            },
        ),
    ]

    state = project_shadow_clone_v2_public_state(
        agent_run_id="run-1",
        events=events,
        parent_status="running",
    )

    tool_call = state["tool_calls"]["tool-1"]
    assert "arguments" not in tool_call
    assert tool_call["arguments_summary"] == {"path": "/workspace/story.md"}
    assert tool_call["arguments_redacted"] is True
    assert tool_call["arguments_size_bytes"] == 1234


def test_v2_projection_replays_non_empty_affected_task_snapshots() -> None:
    events = [
        _event(
            1,
            EventType.TASK_CREATED,
            {
                "task": {
                    "id": "task-a",
                    "task_id": "task-a",
                    "run_id": "run-1",
                    "thread_id": "thread-1",
                    "subject": "A",
                    "description": "A",
                    "status": "pending",
                    "blocked_by": [],
                    "blocks": [],
                    "owner_agent": None,
                    "attempt": 0,
                    "max_attempts": 2,
                    "created_by": "researcher",
                    "plan_revision": 1,
                    "version": 1,
                    "metadata": {"agent_name": "researcher"},
                    "error": {},
                    "created_at": "2026-06-02T00:00:00+00:00",
                    "updated_at": "2026-06-02T00:00:00+00:00",
                }
            },
        ),
        _event(
            2,
            EventType.TASK_CREATED,
            {
                "task": {
                    "id": "task-b",
                    "task_id": "task-b",
                    "run_id": "run-1",
                    "thread_id": "thread-1",
                    "subject": "B",
                    "description": "B",
                    "status": "pending",
                    "blocked_by": [],
                    "blocks": [],
                    "owner_agent": None,
                    "attempt": 0,
                    "max_attempts": 2,
                    "created_by": "researcher",
                    "plan_revision": 1,
                    "version": 1,
                    "metadata": {"agent_name": "reviewer"},
                    "error": {},
                    "created_at": "2026-06-02T00:00:00+00:00",
                    "updated_at": "2026-06-02T00:00:00+00:00",
                }
            },
        ),
        _event(
            3,
            EventType.TASK_UPDATED,
            {
                "task_id": "task-a",
                "new_version": 2,
                "task": {
                    "id": "task-a",
                    "task_id": "task-a",
                    "run_id": "run-1",
                    "thread_id": "thread-1",
                    "subject": "A",
                    "description": "A",
                    "status": "blocked",
                    "blocked_by": ["task-b"],
                    "blocks": [],
                    "owner_agent": None,
                    "attempt": 0,
                    "max_attempts": 2,
                    "created_by": "researcher",
                    "plan_revision": 2,
                    "version": 2,
                    "metadata": {"agent_name": "researcher"},
                    "error": {},
                    "created_at": "2026-06-02T00:00:00+00:00",
                    "updated_at": "2026-06-02T00:01:00+00:00",
                },
                "affected_task_ids": ["task-b"],
                "affected_tasks": [
                    {
                        "id": "task-b",
                        "task_id": "task-b",
                        "run_id": "run-1",
                        "thread_id": "thread-1",
                        "subject": "B",
                        "description": "B",
                        "status": "pending",
                        "blocked_by": [],
                        "blocks": ["task-a"],
                        "owner_agent": None,
                        "attempt": 0,
                        "max_attempts": 2,
                        "created_by": "researcher",
                        "plan_revision": 2,
                        "version": 2,
                        "metadata": {"agent_name": "reviewer"},
                        "error": {},
                        "created_at": "2026-06-02T00:00:00+00:00",
                        "updated_at": "2026-06-02T00:01:00+00:00",
                    }
                ],
            },
        ),
    ]

    state = project_shadow_clone_v2_public_state(
        agent_run_id="run-1", events=events, parent_status="running"
    )

    assert state["tasks"]["task-a"]["blocked_by"] == ["task-b"]
    assert state["tasks"]["task-a"]["version"] == 2
    assert state["tasks"]["task-b"]["blocks"] == ["task-a"]
    assert state["tasks"]["task-b"]["version"] == 2
