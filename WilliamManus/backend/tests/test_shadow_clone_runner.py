import inspect
import json

import pytest
from agentscope.message import Msg

from agentscope_integration.shadow_clone.constants import ShadowCloneMode
import agentscope_integration.shadow_clone.runner as shadow_runner_module


class _FakeCoordinator:
    close_calls = 0

    def __init__(self, **kwargs):
        self.kwargs = kwargs

    async def run(self, **kwargs):
        yield {
            "type": "shadow_clone_planning_started",
            "reason": "planning_started",
            "activity_owner": "shadow_clone",
            "ui_phase": "planning",
            "phase_reason": "planning_started",
            "live_activity": {
                "scope": "shadow_clone_main",
                "phase": "planning",
                "reason": "planning_started",
            },
        }
        yield {
            "type": "passthrough_message",
            "msg": Msg(name="assistant", content="hel", role="assistant"),
            "is_last": False,
            "route_metadata": {
                "shadow_clone_phase": "planning",
                "live_activity": {
                    "scope": "shadow_clone_main",
                    "phase": "planning",
                    "reason": "planning_started",
                },
            },
        }
        yield {
            "type": "passthrough_message",
            "msg": Msg(name="assistant", content="hello", role="assistant"),
            "is_last": True,
            "route_metadata": {
                "shadow_clone_phase": "planning",
                "live_activity": {
                    "scope": "shadow_clone_main",
                    "phase": "planning",
                    "reason": "planning_started",
                },
            },
        }
        yield {
            "type": "shadow_clone_proposed",
            "subtasks": [{"id": "task-1"}],
            "dependencies": [],
            "activity_owner": "shadow_clone",
            "ui_phase": "confirming",
            "phase_reason": "proposal_created",
        }
        yield {"type": "passthrough_sse", "payload": {"type": "assistant", "content": "{}"}}
        yield {
            "type": "status",
            "status": "cancelled",
            "message": "cancelled",
            "terminal_reason": "confirmation_cancelled",
            "activity_owner": "none",
            "ui_phase": "cancelled",
            "phase_reason": "confirmation_cancelled",
        }

    async def close(self):
        type(self).close_calls += 1


@pytest.mark.asyncio
async def test_shadow_clone_runner_signature_compatible():
    shadow_sig = inspect.signature(shadow_runner_module.ShadowCloneRunner.run)
    shadow_params = list(shadow_sig.parameters.keys())
    assert shadow_params == [
        "self",
        "user_message",
        "thread_run_id",
        "user_media_refs",
        "resume_strategy",
        "resume_window_minutes",
    ]


@pytest.mark.asyncio
async def test_shadow_clone_runner_emits_sse_and_passthrough(monkeypatch):
    monkeypatch.setattr(shadow_runner_module, "ShadowCloneCoordinator", _FakeCoordinator)
    _FakeCoordinator.close_calls = 0
    runner = shadow_runner_module.ShadowCloneRunner(
        thread_id="thread-1",
        project_id="project-1",
        agent_run_id="run-1",
        model_key="kimi-k2.5",
        db_client=object(),
        mode=ShadowCloneMode.AUTO,
    )

    events = []
    async for chunk in runner.run(user_message="x", thread_run_id="tr-1"):
        events.append(chunk)

    assert events[0]["type"] == "assistant"
    assert json.loads(events[0]["metadata"])["stream_status"] == "shadow_clone_planning_started"
    assert json.loads(events[1]["metadata"])["stream_status"] == "chunk"
    assert json.loads(events[1]["metadata"])["shadow_clone_phase"] == "planning"
    assert json.loads(events[1]["metadata"])["activity_owner"] == "shadow_clone"
    assert json.loads(events[1]["metadata"])["ui_phase"] == "planning"
    assert json.loads(events[1]["metadata"])["phase_reason"] == "planning_started"
    assert json.loads(events[2]["metadata"])["stream_status"] == "complete"
    assert json.loads(events[2]["metadata"])["shadow_clone_phase"] == "planning"
    assert json.loads(events[3]["metadata"])["stream_status"] == "shadow_clone_proposed"
    assert json.loads(events[3]["content"])["activity_owner"] == "shadow_clone"
    assert json.loads(events[3]["content"])["ui_phase"] == "confirming"
    assert json.loads(events[3]["content"])["phase_reason"] == "proposal_created"
    assert events[4]["type"] == "assistant"  # passthrough
    assert events[5]["type"] == "status"
    assert events[5]["status"] == "cancelled"
    assert events[5]["activity_owner"] == "none"
    assert events[5]["ui_phase"] == "cancelled"
    assert events[5]["phase_reason"] == "confirmation_cancelled"
    await runner.close()
    assert _FakeCoordinator.close_calls == 1


@pytest.mark.asyncio
async def test_shadow_clone_runner_preserves_visible_planning_text_content(monkeypatch):
    monkeypatch.setattr(shadow_runner_module, "ShadowCloneCoordinator", _FakeCoordinator)
    runner = shadow_runner_module.ShadowCloneRunner(
        thread_id="thread-1",
        project_id="project-1",
        agent_run_id="run-1",
        model_key="kimi-k2.5",
        db_client=object(),
        mode=ShadowCloneMode.AUTO,
    )

    events = []
    async for chunk in runner.run(user_message="x", thread_run_id="tr-1"):
        events.append(chunk)

    planning_started_payload = json.loads(events[0]["content"])
    first_planning_chunk = json.loads(events[1]["content"])
    final_planning_chunk = json.loads(events[2]["content"])

    assert planning_started_payload["reason"] == "planning_started"
    assert planning_started_payload["activity_owner"] == "shadow_clone"
    assert planning_started_payload["ui_phase"] == "planning"
    assert planning_started_payload["phase_reason"] == "planning_started"
    assert planning_started_payload["live_activity"]["phase"] == "planning"
    assert first_planning_chunk == {"role": "assistant", "content": "hel"}
    assert final_planning_chunk == {"role": "assistant", "content": "hello"}


@pytest.mark.asyncio
async def test_shadow_clone_runner_emits_environment_event_payloads(monkeypatch):
    class _EnvironmentCoordinator:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def run(self, **kwargs):
            yield {
                "type": "shadow_clone_environment_preparing",
                "reason": "initial",
                "live_activity": {
                    "scope": "shadow_clone_main",
                    "phase": "confirming",
                    "reason": "proposal_created",
                },
            }
            yield {
                "type": "shadow_clone_environment_ready",
                "reason": "initial",
                "environment": {
                    "sandbox": {"id": "sbx-1"},
                },
                "live_activity": {
                    "scope": "shadow_clone_main",
                    "phase": "confirming",
                    "reason": "proposal_created",
                },
            }
            yield {
                "type": "shadow_clone_environment_recovering",
                "recovery_kind": "clone",
                "layer_index": 0,
                "message": "reattaching",
                "live_activity": {
                    "scope": "shadow_clone_main",
                    "phase": "execution",
                    "reason": "subagent_recovering",
                },
            }

    monkeypatch.setattr(shadow_runner_module, "ShadowCloneCoordinator", _EnvironmentCoordinator)
    runner = shadow_runner_module.ShadowCloneRunner(
        thread_id="thread-1",
        project_id="project-1",
        agent_run_id="run-1",
        model_key="kimi-k2.5",
        db_client=object(),
        mode=ShadowCloneMode.AUTO,
    )

    events = []
    async for chunk in runner.run(user_message="x", thread_run_id="tr-1"):
        events.append(chunk)

    assert json.loads(events[0]["metadata"])["stream_status"] == "shadow_clone_environment_preparing"
    assert json.loads(events[0]["content"])["reason"] == "initial"
    assert json.loads(events[0]["content"])["activity_owner"] == "shadow_clone"
    assert json.loads(events[0]["content"])["ui_phase"] == "preparing_environment"
    assert json.loads(events[0]["content"])["phase_reason"] == "initial"
    assert json.loads(events[1]["metadata"])["stream_status"] == "shadow_clone_environment_ready"
    assert json.loads(events[1]["content"])["environment"]["sandbox"]["id"] == "sbx-1"
    assert json.loads(events[1]["content"])["ui_phase"] == "confirming"
    assert json.loads(events[2]["metadata"])["stream_status"] == "shadow_clone_environment_recovering"
    assert json.loads(events[2]["content"])["recovery_kind"] == "clone"
    assert json.loads(events[2]["content"])["activity_owner"] == "shadow_clone"
    assert json.loads(events[2]["content"])["ui_phase"] == "recovering"
    assert json.loads(events[2]["content"])["phase_reason"] == "reattaching"


def test_shadow_clone_runner_drops_internal_spawn_subagents_tool_payload(monkeypatch):
    monkeypatch.setattr(shadow_runner_module, "ShadowCloneCoordinator", _FakeCoordinator)
    runner = shadow_runner_module.ShadowCloneRunner(
        thread_id="thread-1",
        project_id="project-1",
        agent_run_id="run-1",
        model_key="kimi-k2.5",
        db_client=object(),
        mode=ShadowCloneMode.AUTO,
    )

    payload = {
        "type": "tool",
        "content": json.dumps({"tool_name": "spawn_subagents"}),
        "metadata": json.dumps({"stream_status": "complete"}),
    }

    assert runner._normalize_planning_payload(payload) is None


def test_shadow_clone_runner_rewrites_spawn_subagents_assistant_payload_to_visible_text(monkeypatch):
    monkeypatch.setattr(shadow_runner_module, "ShadowCloneCoordinator", _FakeCoordinator)
    runner = shadow_runner_module.ShadowCloneRunner(
        thread_id="thread-1",
        project_id="project-1",
        agent_run_id="run-1",
        model_key="kimi-k2.5",
        db_client=object(),
        mode=ShadowCloneMode.AUTO,
    )

    payload = {
        "type": "assistant",
        "content": json.dumps(
            {
                "role": "assistant",
                "content": "planning in progress",
                "tool_calls": [
                    {
                        "type": "function",
                        "function": {"name": "spawn_subagents", "arguments": "{}"},
                    }
                ],
            }
        ),
        "metadata": json.dumps(
            {
                "stream_status": "complete",
                "tool_calls": [
                    {
                        "type": "function",
                        "function": {"name": "spawn_subagents", "arguments": "{}"},
                    }
                ],
            }
        ),
    }

    normalized = runner._normalize_planning_payload(payload)

    assert normalized is not None
    assert normalized["type"] == "assistant"
    assert json.loads(normalized["content"])["content"] == "planning in progress"
    metadata = json.loads(normalized["metadata"])
    assert metadata["stream_status"] == "complete"
    assert "tool_calls" not in metadata


@pytest.mark.asyncio
async def test_shadow_clone_runner_emits_subagent_activity_payload(monkeypatch):
    class _ActivityCoordinator:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def run(self, **kwargs):
            yield {
                "type": "subagent_activity",
                "subtask_id": "task-9",
                "sequence": 3,
                "role": "researcher",
                "message_type": "assistant",
                "content": {
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {
                                "name": "web_search",
                                "arguments": '{"query":"openai"}',
                            },
                        }
                    ]
                },
                "metadata": {"stream_status": "tool_call_chunk"},
                "created_at": "2026-03-06T00:00:00Z",
                "updated_at": "2026-03-06T00:00:00Z",
            }

    monkeypatch.setattr(shadow_runner_module, "ShadowCloneCoordinator", _ActivityCoordinator)
    runner = shadow_runner_module.ShadowCloneRunner(
        thread_id="thread-1",
        project_id="project-1",
        agent_run_id="run-1",
        model_key="kimi-k2.5",
        db_client=object(),
        mode=ShadowCloneMode.AUTO,
    )

    events = []
    async for chunk in runner.run(user_message="x", thread_run_id="tr-1"):
        events.append(chunk)

    assert len(events) == 1
    assert json.loads(events[0]["metadata"])["stream_status"] == "subagent_activity"
    payload = json.loads(events[0]["content"])
    assert payload["subtask_id"] == "task-9"
    assert payload["metadata"]["stream_status"] == "tool_call_chunk"
    assert payload["content"]["tool_calls"][0]["function"]["name"] == "web_search"
    assert payload["activity_owner"] == "shadow_clone"
    assert payload["ui_phase"] == "subagents_running"
    assert payload["phase_reason"] == "subagent_activity"


@pytest.mark.asyncio
async def test_shadow_clone_runner_keeps_planning_text_visible_when_status_interleaves(
    monkeypatch,
):
    class _PlanningInterleavedCoordinator:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

        async def run(self, **kwargs):
            yield {
                "type": "shadow_clone_planning_started",
                "reason": "planning_started",
                "activity_owner": "shadow_clone",
                "ui_phase": "planning",
                "phase_reason": "planning_started",
                "live_activity": {
                    "scope": "shadow_clone_main",
                    "phase": "planning",
                    "reason": "planning_started",
                },
            }
            yield {
                "type": "status",
                "status": "pending",
                "message": "Planning shadow clone proposal",
                "live_activity": {
                    "scope": "shadow_clone_main",
                    "phase": "planning",
                    "reason": "planning_started",
                },
                "activity_owner": "shadow_clone",
                "ui_phase": "planning",
                "phase_reason": "planning_started",
            }

            class _PlanningMsg:
                role = "assistant"

                def get_content_blocks(self):
                    return [
                        {
                            "type": "tool_use",
                            "id": "call_1",
                            "name": "spawn_subagents",
                            "input": {"subtasks": [{"id": "task-1"}]},
                        },
                        {"type": "text", "text": "planning in progress"},
                    ]

                def get_text_content(self):
                    return "planning in progress"

            yield {
                "type": "passthrough_message",
                "msg": _PlanningMsg(),
                "is_last": True,
                "route_metadata": {
                    "shadow_clone_phase": "planning",
                    "live_activity": {
                        "scope": "shadow_clone_main",
                        "phase": "planning",
                        "reason": "planning_started",
                    },
                },
            }

        async def close(self):
            return None

    monkeypatch.setattr(
        shadow_runner_module,
        "ShadowCloneCoordinator",
        _PlanningInterleavedCoordinator,
    )
    runner = shadow_runner_module.ShadowCloneRunner(
        thread_id="thread-1",
        project_id="project-1",
        agent_run_id="run-1",
        model_key="kimi-k2.5",
        db_client=object(),
        mode=ShadowCloneMode.AUTO,
    )

    events = []
    async for chunk in runner.run(user_message="x", thread_run_id="tr-1"):
        events.append(chunk)

    assert [event["type"] for event in events] == ["assistant", "status", "assistant"]
    assert events[1]["status"] == "pending"
    assert events[1]["ui_phase"] == "planning"
    assert events[1]["phase_reason"] == "planning_started"

    planning_chunk = json.loads(events[2]["content"])
    planning_metadata = json.loads(events[2]["metadata"])
    assert planning_metadata["stream_status"] == "complete"
    assert planning_metadata["shadow_clone_phase"] == "planning"
    assert planning_chunk == {"role": "assistant", "content": "planning in progress"}
