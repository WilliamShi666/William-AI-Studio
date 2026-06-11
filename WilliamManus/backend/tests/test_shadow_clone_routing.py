from __future__ import annotations

import sys
import types
from dataclasses import dataclass
from typing import Any, Dict, List

import pytest

# Some tests inject a minimal dramatiq stub without middleware classes.
_dramatiq_mod = sys.modules.get("dramatiq")
_middleware = getattr(_dramatiq_mod, "middleware", None) if _dramatiq_mod else None
if _middleware is not None:
    for _name in ("AsyncIO", "Retries", "TimeLimit"):
        if not hasattr(_middleware, _name):
            setattr(_middleware, _name, lambda *args, **kwargs: object())

import run_agent_background as run_agent_background_module
import agent.shadow_clone_routes as shadow_clone_routes_module
from agentscope_integration.shadow_clone.constants import (
    SHADOW_CLONE_DEFAULT_MODE,
    ShadowCloneMode,
)
from agentscope_integration.shadow_clone_v2.models import EventType
import agent.run as run_module
from utils import dramatiq_queue_names as dramatiq_queue_names_module
from agentscope_integration.claude_sdk_runner import ClaudeSDKRunner


@dataclass
class _FakeResult:
    data: List[Dict[str, Any]]


class _FakeProjectsQuery:
    def __init__(self):
        self._filters: Dict[str, Any] = {}

    def select(self, *_args, **_kwargs):
        return self

    def eq(self, key: str, value: Any):
        self._filters[key] = value
        return self

    async def execute(self):
        project_id = self._filters.get("project_id", "project-1")
        return _FakeResult(data=[{"project_id": project_id, "sandbox": {}}])


class _FakeClient:
    def table(self, table_name: str):
        if table_name == "events":
            return _FakeEventsQuery()
        return _FakeProjectsQuery()


class _FakeDBConnection:
    @property
    async def client(self):
        return _FakeClient()


class _FakeAgentScopeRunner:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class _FakeShadowCloneRunner:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


class _FakeShadowCloneV2Runner:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    async def run(self, **kwargs):
        self.run_kwargs = kwargs
        yield {"type": "status", "status": "completed"}


def _patch_setup_dependencies(monkeypatch):
    monkeypatch.setattr(run_module.langfuse, "trace", lambda **_kwargs: object())

    class _FakeAuthUtils:
        @staticmethod
        async def get_account_id_from_thread(_client, _thread_id: str):
            return "account-1"

    auth_mod = __import__("utils.auth_utils", fromlist=["AuthUtils"])
    monkeypatch.setattr(auth_mod, "AuthUtils", _FakeAuthUtils)

    pg_mod = __import__("services.postgresql", fromlist=["DBConnection"])
    monkeypatch.setattr(pg_mod, "DBConnection", _FakeDBConnection)

    monkeypatch.setattr(run_module, "AgentScopeRunner", _FakeAgentScopeRunner)

    fake_shadow_module = types.ModuleType("agentscope_integration.shadow_clone.runner")
    fake_shadow_module.ShadowCloneRunner = _FakeShadowCloneRunner
    monkeypatch.setitem(
        sys.modules,
        "agentscope_integration.shadow_clone.runner",
        fake_shadow_module,
    )

    fake_shadow_v2_module = types.ModuleType(
        "agentscope_integration.shadow_clone_v2.runner"
    )
    fake_shadow_v2_module.ShadowCloneV2Runner = _FakeShadowCloneV2Runner
    monkeypatch.setitem(
        sys.modules,
        "agentscope_integration.shadow_clone_v2.runner",
        fake_shadow_v2_module,
    )


@pytest.mark.asyncio
async def test_run_agent_background_mode_resolution_defaults_to_default_mode():
    resolved_mode, origin = (
        run_agent_background_module._resolve_effective_shadow_clone_mode(
            shadow_clone_mode=None,
            agent_config=None,
        )
    )
    assert origin == "interactive"
    assert resolved_mode == SHADOW_CLONE_DEFAULT_MODE


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "requested, expected",
    [
        ("off", ShadowCloneMode.OFF),
        ("on", ShadowCloneMode.ON),
        ("auto", ShadowCloneMode.AUTO),
        ("v2", ShadowCloneMode.V2),
    ],
)
async def test_run_agent_background_mode_resolution_honors_explicit_mode_for_interactive(
    requested,
    expected,
):
    resolved_mode, origin = (
        run_agent_background_module._resolve_effective_shadow_clone_mode(
            shadow_clone_mode=requested,
            agent_config={"execution_origin": "interactive"},
        )
    )
    assert origin == "interactive"
    assert resolved_mode == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "agent_config, expected_origin",
    [
        ({"trigger_execution": True}, "trigger"),
        ({"workflow_execution": True}, "workflow"),
        ({"execution_origin": "automation"}, "automation"),
    ],
)
async def test_run_agent_background_mode_resolution_forces_off_for_automation_like_origins(
    agent_config,
    expected_origin,
):
    resolved_mode, origin = (
        run_agent_background_module._resolve_effective_shadow_clone_mode(
            shadow_clone_mode="on",
            agent_config=agent_config,
        )
    )
    assert origin == expected_origin
    assert resolved_mode == ShadowCloneMode.OFF


@pytest.mark.asyncio
async def test_agent_runner_setup_uses_agentscope_runner_when_off(monkeypatch):
    _patch_setup_dependencies(monkeypatch)

    config = run_module.AgentConfig(
        thread_id="thread-1",
        project_id="project-1",
        stream=True,
        model_name="qwen3.5-plus",
        shadow_clone_mode=ShadowCloneMode.OFF,
    )
    runner = run_module.AgentRunner(config)
    await runner.setup()

    assert isinstance(runner.agentscope_runner, _FakeAgentScopeRunner)


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [ShadowCloneMode.ON, ShadowCloneMode.AUTO])
async def test_agent_runner_setup_uses_shadow_clone_v2_runner_when_on_or_auto(
    monkeypatch, mode
):
    _patch_setup_dependencies(monkeypatch)

    config = run_module.AgentConfig(
        thread_id="thread-2",
        project_id="project-2",
        stream=True,
        model_name="qwen3.5-plus",
        agent_run_id="run-2",
        shadow_clone_mode=mode,
    )
    runner = run_module.AgentRunner(config)
    await runner.setup()

    assert isinstance(runner.agentscope_runner, _FakeShadowCloneV2Runner)
    assert runner.agentscope_runner.kwargs["agent_run_id"] == "run-2"
    assert runner.agentscope_runner.kwargs["mode"] == mode


@pytest.mark.asyncio
async def test_agent_runner_setup_uses_shadow_clone_v2_runner_when_v2(monkeypatch):
    _patch_setup_dependencies(monkeypatch)

    config = run_module.AgentConfig(
        thread_id="thread-v2",
        project_id="project-v2",
        stream=True,
        model_name="qwen3.5-plus",
        agent_run_id="run-v2",
        shadow_clone_mode=ShadowCloneMode.V2,
    )
    runner = run_module.AgentRunner(config)
    await runner.setup()

    assert isinstance(runner.agentscope_runner, _FakeShadowCloneV2Runner)
    assert runner.agentscope_runner.kwargs["agent_run_id"] == "run-v2"
    assert runner.agentscope_runner.kwargs["mode"] == ShadowCloneMode.V2


@pytest.mark.asyncio
async def test_run_agent_carries_shadow_clone_mode_into_runner_config(monkeypatch):
    captured: Dict[str, Any] = {}

    class _FakeRunner:
        def __init__(self, config):
            captured["config"] = config

        async def run(self):
            yield {"type": "status", "status": "completed"}

    monkeypatch.setattr(run_module, "AgentRunner", _FakeRunner)

    events = []
    async for chunk in run_module.run_agent(
        thread_id="thread-3",
        project_id="project-3",
        stream=True,
        model_name="qwen3.5-plus",
        shadow_clone_mode=ShadowCloneMode.ON,
        agent_run_id="run-3",
    ):
        events.append(chunk)

    assert events == [{"type": "status", "status": "completed"}]
    assert captured["config"].shadow_clone_mode == ShadowCloneMode.ON
    assert captured["config"].agent_run_id == "run-3"


def test_delete_sandbox_background_actor_has_explicit_time_limit():
    assert run_agent_background_module.delete_sandbox_background.options[
        "time_limit"
    ] == (run_agent_background_module.SANDBOX_CLEANUP_ACTOR_TIME_LIMIT_SECONDS * 1000)


def test_run_agent_background_actor_uses_configured_queue_name():
    assert (
        run_agent_background_module.run_agent_background.options["queue_name"]
        == dramatiq_queue_names_module.RUN_AGENT_BACKGROUND_QUEUE
    )


def test_delete_sandbox_background_actor_uses_configured_queue_name():
    assert (
        run_agent_background_module.delete_sandbox_background.options["queue_name"]
        == dramatiq_queue_names_module.SANDBOX_CLEANUP_QUEUE
    )


@pytest.mark.asyncio
async def test_shadow_clone_status_exposes_main_agent_continuation_contract(
    monkeypatch,
):
    async def _fake_get_agent_run_with_access_check(_agent_run_id: str, _user_id: str):
        return {
            "agent_run_id": "run-status-main-agent",
            "status": "running",
            "metadata": {"shadow_clone_mode": "on"},
        }

    async def _fake_get_state(_agent_run_id: str):
        return {
            "status": "running",
            "mode": "auto",
            "updated_at": "2026-04-02T00:00:00+00:00",
            "live_activity": {
                "scope": "main_agent",
                "phase": "execution",
                "reason": "replan_continue",
                "epoch": 2,
                "updated_at": "2026-04-02T00:00:00+00:00",
            },
            "environment": {
                "status": "ready",
                "ready": True,
            },
        }

    async def _fake_get_lease(_agent_run_id: str):
        return {
            "binding_state": "locked",
            "environment_status": "ready",
            "environment_ready": True,
            "environment_manifest": {},
            "sandbox_id": "sandbox-1",
            "sandbox_type": "code",
        }

    monkeypatch.setattr(
        shadow_clone_routes_module,
        "_get_agent_run_with_access_check",
        _fake_get_agent_run_with_access_check,
    )
    monkeypatch.setattr(shadow_clone_routes_module, "get_state", _fake_get_state)
    monkeypatch.setattr(
        shadow_clone_routes_module, "get_run_sandbox_lease", _fake_get_lease
    )
    monkeypatch.setattr(
        shadow_clone_routes_module,
        "build_run_file_delivery_source",
        lambda **_kwargs: {"kind": "shadow-clone"},
    )

    result = await shadow_clone_routes_module.get_shadow_clone_status(
        "run-status-main-agent",
        user_id="user-1",
    )

    assert result["activity_owner"] == "main_agent"
    assert result["ui_phase"] == "main_agent_continuation"
    assert result["phase_reason"] == "replan_continue"


@pytest.mark.asyncio
async def test_shadow_clone_status_exposes_completed_denial_contract(monkeypatch):
    async def _fake_get_agent_run_with_access_check(_agent_run_id: str, _user_id: str):
        return {
            "agent_run_id": "run-status-denied",
            "status": "running",
            "metadata": {"shadow_clone_mode": "on"},
        }

    async def _fake_get_state(_agent_run_id: str):
        return {
            "status": "denied",
            "mode": "auto",
            "updated_at": "2026-04-02T00:00:00+00:00",
            "live_activity": {
                "scope": "main_agent",
                "phase": "completed",
                "reason": "denied_complete",
                "epoch": 0,
                "updated_at": "2026-04-02T00:00:00+00:00",
            },
            "environment": {
                "status": "ready",
                "ready": False,
            },
        }

    async def _fake_get_lease(_agent_run_id: str):
        return None

    monkeypatch.setattr(
        shadow_clone_routes_module,
        "_get_agent_run_with_access_check",
        _fake_get_agent_run_with_access_check,
    )
    monkeypatch.setattr(shadow_clone_routes_module, "get_state", _fake_get_state)
    monkeypatch.setattr(
        shadow_clone_routes_module, "get_run_sandbox_lease", _fake_get_lease
    )
    monkeypatch.setattr(
        shadow_clone_routes_module,
        "build_run_file_delivery_source",
        lambda **_kwargs: {"kind": "shadow-clone"},
    )

    result = await shadow_clone_routes_module.get_shadow_clone_status(
        "run-status-denied",
        user_id="user-1",
    )

    assert result["status"] == "denied"
    assert result["activity_owner"] == "none"
    assert result["ui_phase"] == "completed"
    assert result["phase_reason"] == "denied_complete"


@pytest.mark.asyncio
async def test_shadow_clone_status_projects_v2_event_stream(monkeypatch):
    async def _fake_get_agent_run_with_access_check(_agent_run_id: str, _user_id: str):
        return {
            "agent_run_id": "run-v2-status",
            "status": "completed",
            "metadata": {"shadow_clone_mode": "v2"},
        }

    async def _fake_get_lease(_agent_run_id: str):
        return None

    async def _fake_get_state(_agent_run_id: str):
        return None

    async def _fake_read_v2_events(_agent_run_id: str):
        return [
            types.SimpleNamespace(
                type=EventType.AGENT_SPAWNED,
                payload={"agent_name": "document-analyst", "role": "document analyst"},
                created_at="2026-05-31T12:00:00+00:00",
            ),
            types.SimpleNamespace(
                type=EventType.TASK_CREATED,
                payload={
                    "id": "memo",
                    "subject": "document analyst",
                    "description": "Summarize the memo.",
                    "metadata": {"agent_name": "document-analyst"},
                },
                created_at="2026-05-31T12:00:01+00:00",
            ),
            types.SimpleNamespace(
                type=EventType.TASK_COMPLETED,
                payload={
                    "task_id": "memo",
                    "agent_name": "document-analyst",
                    "result_ref": "Memo summary completed.",
                },
                created_at="2026-05-31T12:05:00+00:00",
            ),
            types.SimpleNamespace(
                type=EventType.AGENT_IDLE,
                payload={
                    "agent_name": "document-analyst",
                    "idle_since": "2026-05-31T12:05:00+00:00",
                    "idle_expires_at": "2026-05-31T12:25:00+00:00",
                },
                created_at="2026-05-31T12:05:00+00:00",
            ),
            types.SimpleNamespace(
                type=EventType.RUN_COMPLETED,
                payload={"executed_task_count": 1},
                created_at="2026-05-31T12:05:01+00:00",
            ),
        ]

    monkeypatch.setattr(
        shadow_clone_routes_module,
        "_get_agent_run_with_access_check",
        _fake_get_agent_run_with_access_check,
    )
    monkeypatch.setattr(shadow_clone_routes_module, "get_state", _fake_get_state)
    monkeypatch.setattr(
        shadow_clone_routes_module, "get_run_sandbox_lease", _fake_get_lease
    )
    monkeypatch.setattr(
        shadow_clone_routes_module, "_read_shadow_clone_v2_events", _fake_read_v2_events
    )
    monkeypatch.setattr(
        shadow_clone_routes_module,
        "build_run_file_delivery_source",
        lambda **_kwargs: {"kind": "shadow-clone-v2"},
    )

    result = await shadow_clone_routes_module.get_shadow_clone_status(
        "run-v2-status",
        user_id="user-1",
    )

    assert result["status"] == "completed"
    assert result["mode"] == "v2"
    assert result["total"] == 1
    assert result["completed"] == 1
    assert result["subagents"]["memo"]["status"] == "completed"
    assert result["subagents"]["memo"]["role"] == "document analyst"
    assert result["subagents"]["memo"]["result_summary"] == "Memo summary completed."
    assert result["subagents"]["memo"]["idle_expires_at"] == "2026-05-31T12:25:00+00:00"
    assert result["ui_phase"] == "completed"
    assert result["activity_owner"] == "none"


@pytest.mark.asyncio
async def test_shadow_clone_status_projects_auto_mode_when_runtime_is_v2(monkeypatch):
    async def _fake_get_agent_run_with_access_check(_agent_run_id: str, _user_id: str):
        return {
            "agent_run_id": "run-v2-auto-status",
            "status": "running",
            "metadata": {
                "requested_shadow_clone_mode": "auto",
                "shadow_clone_mode": "auto",
                "shadow_clone_runtime": "v2",
            },
        }

    async def _fake_get_lease(_agent_run_id: str):
        return None

    async def _fake_get_state(_agent_run_id: str):
        return None

    async def _fake_read_v2_events(_agent_run_id: str):
        return [
            types.SimpleNamespace(
                type=EventType.TASK_CREATED,
                payload={
                    "id": "task-1",
                    "subject": "analyst",
                    "description": "Execute assigned task.",
                    "metadata": {"agent_name": "analyst"},
                },
                created_at="2026-05-31T12:00:01+00:00",
            ),
        ]

    monkeypatch.setattr(
        shadow_clone_routes_module,
        "_get_agent_run_with_access_check",
        _fake_get_agent_run_with_access_check,
    )
    monkeypatch.setattr(shadow_clone_routes_module, "get_state", _fake_get_state)
    monkeypatch.setattr(
        shadow_clone_routes_module, "get_run_sandbox_lease", _fake_get_lease
    )
    monkeypatch.setattr(
        shadow_clone_routes_module, "_read_shadow_clone_v2_events", _fake_read_v2_events
    )
    monkeypatch.setattr(
        shadow_clone_routes_module,
        "build_run_file_delivery_source",
        lambda **_kwargs: {"kind": "shadow-clone-v2"},
    )

    result = await shadow_clone_routes_module.get_shadow_clone_status(
        "run-v2-auto-status",
        user_id="user-1",
    )

    assert result["mode"] == "v2"
    assert result["status"] == "running"
    assert result["proposal"]["subtasks"][0]["id"] == "task-1"
    assert result["subagents"]["task-1"]["role"] == "analyst"
    assert result["subagents"]["task-1"]["status"] == "pending"
    assert result["running"] == 0


@pytest.mark.asyncio
async def test_shadow_clone_status_projects_v2_claimed_tasks_as_running(monkeypatch):
    async def _fake_get_agent_run_with_access_check(_agent_run_id: str, _user_id: str):
        return {
            "agent_run_id": "run-v2-claimed-status",
            "status": "running",
            "metadata": {"shadow_clone_mode": "v2"},
        }

    async def _fake_get_lease(_agent_run_id: str):
        return None

    async def _fake_get_state(_agent_run_id: str):
        return None

    async def _fake_read_v2_events(_agent_run_id: str):
        return [
            types.SimpleNamespace(
                type=EventType.TASK_CREATED,
                payload={
                    "id": "task-1",
                    "subject": "analyst",
                    "description": "Execute assigned task.",
                    "metadata": {"agent_name": "analyst"},
                },
                created_at="2026-05-31T12:00:01+00:00",
            ),
            types.SimpleNamespace(
                type=EventType.TASK_CLAIMED,
                payload={
                    "task_id": "task-1",
                    "agent_name": "analyst",
                    "lease_expires_at": "2026-05-31T12:20:00+00:00",
                },
                created_at="2026-05-31T12:01:00+00:00",
            ),
        ]

    monkeypatch.setattr(
        shadow_clone_routes_module,
        "_get_agent_run_with_access_check",
        _fake_get_agent_run_with_access_check,
    )
    monkeypatch.setattr(shadow_clone_routes_module, "get_state", _fake_get_state)
    monkeypatch.setattr(
        shadow_clone_routes_module, "get_run_sandbox_lease", _fake_get_lease
    )
    monkeypatch.setattr(
        shadow_clone_routes_module, "_read_shadow_clone_v2_events", _fake_read_v2_events
    )
    monkeypatch.setattr(
        shadow_clone_routes_module,
        "build_run_file_delivery_source",
        lambda **_kwargs: {"kind": "shadow-clone-v2"},
    )

    result = await shadow_clone_routes_module.get_shadow_clone_status(
        "run-v2-claimed-status",
        user_id="user-1",
    )

    assert result["status"] == "running"
    assert result["subagents"]["task-1"]["status"] == "running"
    assert result["subagents"]["task-1"]["claimed_at"] == "2026-05-31T12:01:00+00:00"
    assert (
        result["subagents"]["task-1"]["lease_expires_at"] == "2026-05-31T12:20:00+00:00"
    )
    assert result["running"] == 1


@pytest.mark.asyncio
async def test_shadow_clone_status_projects_v2_shutdown_events(monkeypatch):
    async def _fake_get_agent_run_with_access_check(_agent_run_id: str, _user_id: str):
        return {
            "agent_run_id": "run-v2-shutdown-status",
            "status": "completed",
            "metadata": {"shadow_clone_mode": "v2"},
        }

    async def _fake_get_lease(_agent_run_id: str):
        return None

    async def _fake_get_state(_agent_run_id: str):
        return None

    async def _fake_read_v2_events(_agent_run_id: str):
        return [
            types.SimpleNamespace(
                type=EventType.AGENT_SPAWNED,
                payload={"agent_name": "analyst", "role": "analyst"},
                created_at="2026-05-31T12:00:00+00:00",
            ),
            types.SimpleNamespace(
                type=EventType.TASK_CREATED,
                payload={
                    "id": "task-1",
                    "subject": "analyst",
                    "description": "Execute assigned task.",
                    "metadata": {"agent_name": "analyst"},
                },
                created_at="2026-05-31T12:00:01+00:00",
            ),
            types.SimpleNamespace(
                type=EventType.AGENT_SHUTDOWN,
                payload={
                    "agent_name": "analyst",
                    "shutdown_reason": "work_complete",
                    "status": "closed",
                },
                created_at="2026-05-31T12:05:00+00:00",
            ),
            types.SimpleNamespace(
                type=EventType.TEAM_DELETED,
                payload={
                    "team_id": "shadow-clone-v2:project-1:thread-1",
                    "shutdown_reason": "work_complete",
                },
                created_at="2026-05-31T12:05:01+00:00",
            ),
        ]

    monkeypatch.setattr(
        shadow_clone_routes_module,
        "_get_agent_run_with_access_check",
        _fake_get_agent_run_with_access_check,
    )
    monkeypatch.setattr(shadow_clone_routes_module, "get_state", _fake_get_state)
    monkeypatch.setattr(
        shadow_clone_routes_module, "get_run_sandbox_lease", _fake_get_lease
    )
    monkeypatch.setattr(
        shadow_clone_routes_module, "_read_shadow_clone_v2_events", _fake_read_v2_events
    )
    monkeypatch.setattr(
        shadow_clone_routes_module,
        "build_run_file_delivery_source",
        lambda **_kwargs: {"kind": "shadow-clone-v2"},
    )

    result = await shadow_clone_routes_module.get_shadow_clone_status(
        "run-v2-shutdown-status",
        user_id="user-1",
    )

    assert result["subagents"]["task-1"]["agent_status"] == "closed"
    assert result["subagents"]["task-1"]["shutdown_reason"] == "work_complete"
    assert result["team_shutdown"] == {
        "team_id": "shadow-clone-v2:project-1:thread-1",
        "shutdown_reason": "work_complete",
    }
    assert result["status"] == "completed"
    assert result["live_activity"]["phase"] == "completed"
    assert result["live_activity"]["reason"] == "shadow_clone_v2_team_shutdown"


@pytest.mark.asyncio
async def test_shadow_clone_status_v2_exposes_event_derived_canonical_projection(
    monkeypatch,
):
    async def _fake_get_agent_run_with_access_check(_agent_run_id: str, _user_id: str):
        return {
            "agent_run_id": "run-v2-canonical-status",
            "status": "completed",
            "metadata": {"shadow_clone_mode": "v2"},
        }

    async def _fake_get_lease(_agent_run_id: str):
        return None

    async def _forbidden_get_state(_agent_run_id: str):
        raise AssertionError("V2 event projection must not read legacy get_state")

    async def _fake_read_v2_events(_agent_run_id: str):
        return [
            types.SimpleNamespace(
                sequence=1,
                type=EventType.RUN_STARTED,
                payload={"model_key": "frontend-model"},
                created_at="2026-06-02T00:00:00+00:00",
            ),
            types.SimpleNamespace(
                sequence=2,
                type=EventType.TEAM_CREATED,
                payload={
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
                created_at="2026-06-02T00:00:01+00:00",
            ),
            types.SimpleNamespace(
                sequence=3,
                type=EventType.TASK_CREATED,
                payload={
                    "id": "task-1",
                    "subject": "Analyst",
                    "description": "Analyze.",
                    "metadata": {"agent_name": "analyst"},
                    "plan_revision": 1,
                },
                created_at="2026-06-02T00:00:02+00:00",
            ),
            types.SimpleNamespace(
                sequence=4,
                type=EventType.TASK_UPDATED,
                payload={
                    "task_id": "task-1",
                    "status": "pending",
                    "description": "Analyze updated document.",
                    "metadata": {"progress": "ready"},
                    "new_version": 2,
                },
                created_at="2026-06-02T00:00:03+00:00",
            ),
            types.SimpleNamespace(
                sequence=5,
                type=EventType.MAILBOX_SENT,
                payload={
                    "message_id": "msg-1",
                    "sender": "facilitator",
                    "recipient": "analyst",
                    "summary": "Please continue.",
                },
                created_at="2026-06-02T00:00:04+00:00",
            ),
            types.SimpleNamespace(
                sequence=6,
                type=EventType.AGENT_WAKE,
                payload={
                    "agent_name": "analyst",
                    "wake_reason": "mailbox_unread",
                    "message_id": "msg-1",
                },
                created_at="2026-06-02T00:00:05+00:00",
            ),
            types.SimpleNamespace(
                sequence=7,
                type=EventType.RUN_COMPLETED,
                payload={"final_output": "Final main answer."},
                created_at="2026-06-02T00:00:06+00:00",
            ),
        ]

    monkeypatch.setattr(
        shadow_clone_routes_module,
        "_get_agent_run_with_access_check",
        _fake_get_agent_run_with_access_check,
    )
    monkeypatch.setattr(shadow_clone_routes_module, "get_state", _forbidden_get_state)
    monkeypatch.setattr(
        shadow_clone_routes_module, "get_run_sandbox_lease", _fake_get_lease
    )
    monkeypatch.setattr(
        shadow_clone_routes_module, "_read_shadow_clone_v2_events", _fake_read_v2_events
    )
    monkeypatch.setattr(
        shadow_clone_routes_module,
        "build_run_file_delivery_source",
        lambda **_kwargs: {"kind": "shadow-clone-v2"},
    )

    result = await shadow_clone_routes_module.get_shadow_clone_status(
        "run-v2-canonical-status",
        user_id="user-1",
    )

    assert result["team"]["team_id"] == "team-1"
    assert result["agents"]["analyst"]["wake_reason"] == "mailbox_unread"
    assert result["tasks"]["task-1"]["description"] == "Analyze updated document."
    assert result["tasks"]["task-1"]["metadata"]["progress"] == "ready"
    assert result["messages"]["msg-1"]["status"] == "unread"
    assert result["model"]["requested"] == "frontend-model"
    assert result["model"]["effective"] == "frontend-model"
    assert result["final_output"]["content"] == "Final main answer."


@pytest.mark.asyncio
async def test_shadow_clone_results_projects_v2_task_results(monkeypatch):
    async def _fake_get_agent_run_with_access_check(_agent_run_id: str, _user_id: str):
        return {
            "agent_run_id": "run-v2-results",
            "status": "completed",
            "metadata": {"shadow_clone_mode": "v2"},
        }

    async def _fake_read_v2_events(_agent_run_id: str):
        return [
            types.SimpleNamespace(
                type=EventType.TASK_CREATED,
                payload={
                    "id": "memo",
                    "subject": "document analyst",
                    "metadata": {"agent_name": "document-analyst"},
                },
                created_at="2026-05-31T12:00:00+00:00",
            ),
            types.SimpleNamespace(
                type=EventType.TASK_COMPLETED,
                payload={
                    "task_id": "memo",
                    "agent_name": "document-analyst",
                    "result_ref": "Memo summary completed.",
                },
                created_at="2026-05-31T12:05:00+00:00",
            ),
        ]

    monkeypatch.setattr(
        shadow_clone_routes_module,
        "_get_agent_run_with_access_check",
        _fake_get_agent_run_with_access_check,
    )
    monkeypatch.setattr(
        shadow_clone_routes_module, "_read_shadow_clone_v2_events", _fake_read_v2_events
    )

    result = await shadow_clone_routes_module.get_shadow_clone_results(
        "run-v2-results",
        user_id="user-1",
    )

    assert result["count"] == 1
    assert result["results"] == [
        {
            "subtask_id": "memo",
            "role": "document analyst",
            "status": "completed",
            "summary": "Memo summary completed.",
            "submitted_at": "2026-05-31T12:05:00+00:00",
        }
    ]


@pytest.mark.asyncio
async def test_shadow_clone_full_result_projects_v2_task_result(monkeypatch):
    async def _fake_get_agent_run_with_access_check(_agent_run_id: str, _user_id: str):
        return {
            "agent_run_id": "run-v2-full-result",
            "status": "completed",
            "metadata": {"shadow_clone_mode": "v2"},
        }

    async def _fake_read_v2_events(_agent_run_id: str):
        return [
            types.SimpleNamespace(
                type=EventType.TASK_COMPLETED,
                payload={
                    "task_id": "memo",
                    "agent_name": "document-analyst",
                    "result_ref": "Full memo summary text.",
                },
                created_at="2026-05-31T12:05:00+00:00",
            ),
        ]

    monkeypatch.setattr(
        shadow_clone_routes_module,
        "_get_agent_run_with_access_check",
        _fake_get_agent_run_with_access_check,
    )
    monkeypatch.setattr(
        shadow_clone_routes_module, "_read_shadow_clone_v2_events", _fake_read_v2_events
    )

    result = await shadow_clone_routes_module.get_shadow_clone_full_result(
        "run-v2-full-result",
        "memo",
        user_id="user-1",
    )

    assert result == {"subtask_id": "memo", "result": "Full memo summary text."}


class _FakeClaudeSDKRunner:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    async def run(self, **kwargs):
        self.run_kwargs = kwargs
        yield {"type": "status", "status": "completed"}


@pytest.mark.asyncio
async def test_agent_runner_setup_defaults_to_agentscope_when_only_agent_backend_is_claude_sdk(
    monkeypatch,
):
    """AGENT_BACKEND=claude_sdk alone no longer overrides the AgentScope default."""
    monkeypatch.setenv("AGENT_BACKEND", "claude_sdk")
    _patch_setup_dependencies(monkeypatch)

    monkeypatch.setattr(run_module, "ClaudeSDKRunner", _FakeClaudeSDKRunner)

    config = run_module.AgentConfig(
        thread_id="thread-cs",
        project_id="project-cs",
        stream=True,
        model_name="deepseek-v4-pro-high",
        shadow_clone_mode=run_module.ShadowCloneMode.OFF,
    )
    runner = run_module.AgentRunner(config)
    await runner.setup()

    assert isinstance(runner.agentscope_runner, _FakeAgentScopeRunner)
    assert runner.agentscope_runner.kwargs["thread_id"] == "thread-cs"
    assert runner.agentscope_runner.kwargs["project_id"] == "project-cs"


@pytest.mark.asyncio
async def test_agent_runner_setup_passes_orchestrator_prompt_to_claude_sdk_runner(
    monkeypatch,
):
    """Claude SDK backend should reuse AgentScope orchestrator prompt, including skills guidance."""
    monkeypatch.setenv("AGENT_BACKEND", "claude_sdk")
    monkeypatch.setenv("ALLOW_CLAUDE_SDK_BACKEND", "1")
    _patch_setup_dependencies(monkeypatch)

    monkeypatch.setattr(run_module, "ClaudeSDKRunner", _FakeClaudeSDKRunner)
    monkeypatch.setattr(
        "agentscope_integration.prompts.orchestrator_prompt.get_orchestrator_prompt",
        lambda: "ORCHESTRATOR SYSTEM PROMPT\nget_available_skills()\nload_skill(skill_name)",
    )

    config = run_module.AgentConfig(
        thread_id="thread-cs",
        project_id="project-cs",
        stream=True,
        model_name="deepseek-v4-pro-high",
        agent_config={"agent_backend": "claude_sdk"},
        shadow_clone_mode=run_module.ShadowCloneMode.OFF,
    )
    runner = run_module.AgentRunner(config)
    await runner.setup()

    assert (
        runner.agentscope_runner.kwargs["system_prompt"]
        == "ORCHESTRATOR SYSTEM PROMPT\nget_available_skills()\nload_skill(skill_name)"
    )


@pytest.mark.asyncio
async def test_agent_runner_setup_keeps_real_orchestrator_skill_awareness_for_claude_sdk(
    monkeypatch,
):
    """Regression guard: the real prompt passed to Claude SDK must name skill discovery tools."""
    monkeypatch.setenv("AGENT_BACKEND", "claude_sdk")
    monkeypatch.setenv("ALLOW_CLAUDE_SDK_BACKEND", "1")
    _patch_setup_dependencies(monkeypatch)

    monkeypatch.setattr(run_module, "ClaudeSDKRunner", _FakeClaudeSDKRunner)

    config = run_module.AgentConfig(
        thread_id="thread-cs",
        project_id="project-cs",
        stream=True,
        model_name="deepseek-v4-pro-high",
        agent_config={"agent_backend": "claude_sdk"},
        shadow_clone_mode=run_module.ShadowCloneMode.OFF,
    )
    runner = run_module.AgentRunner(config)
    await runner.setup()

    system_prompt = runner.agentscope_runner.kwargs["system_prompt"]
    assert "get_available_skills()" in system_prompt
    assert "load_skill(skill_name)" in system_prompt
    assert "ppt-generator-image-model" in system_prompt


@pytest.mark.asyncio
async def test_agent_runner_setup_prefers_agent_identity_prompt_for_claude_sdk(
    monkeypatch,
):
    """Default FuFanManus identity says Roys Alpha; Claude SDK must receive that identity first."""
    monkeypatch.setenv("AGENT_BACKEND", "claude_sdk")
    monkeypatch.setenv("ALLOW_CLAUDE_SDK_BACKEND", "1")
    _patch_setup_dependencies(monkeypatch)

    monkeypatch.setattr(run_module, "ClaudeSDKRunner", _FakeClaudeSDKRunner)
    monkeypatch.setattr(
        "agentscope_integration.prompts.orchestrator_prompt.get_orchestrator_prompt",
        lambda: "You are the Orchestrator of Roys Alpha.\nget_available_skills()",
    )

    config = run_module.AgentConfig(
        thread_id="thread-cs",
        project_id="project-cs",
        stream=True,
        model_name="deepseek-v4-pro-high",
        agent_config={
            "agent_backend": "claude_sdk",
            "system_prompt": "You are Roys Alpha, an autonomous AI Worker created by Roys乐亦思.",
        },
        shadow_clone_mode=run_module.ShadowCloneMode.OFF,
    )
    runner = run_module.AgentRunner(config)
    await runner.setup()

    system_prompt = runner.agentscope_runner.kwargs["system_prompt"]
    assert system_prompt.startswith("You are Roys Alpha, an autonomous AI Worker")
    assert "get_available_skills()" in system_prompt


@pytest.mark.asyncio
async def test_agent_runner_setup_uses_agentscope_when_env_var_not_set(monkeypatch):
    """When AGENT_BACKEND is not set, setup() uses AgentScopeRunner (default)."""
    monkeypatch.delenv("AGENT_BACKEND", raising=False)
    _patch_setup_dependencies(monkeypatch)

    config = run_module.AgentConfig(
        thread_id="thread-df",
        project_id="project-df",
        stream=True,
        model_name="qwen3.5-plus",
        shadow_clone_mode=run_module.ShadowCloneMode.OFF,
    )
    runner = run_module.AgentRunner(config)
    await runner.setup()

    assert isinstance(runner.agentscope_runner, _FakeAgentScopeRunner)


@pytest.mark.asyncio
async def test_agent_runner_setup_uses_agentscope_when_system_backend_is_agentscope(
    monkeypatch,
):
    """Production default is AgentScope even if the Claude SDK allow gate is enabled."""
    monkeypatch.setenv("AGENT_BACKEND", "agentscope")
    monkeypatch.setenv("ALLOW_CLAUDE_SDK_BACKEND", "1")
    _patch_setup_dependencies(monkeypatch)

    config = run_module.AgentConfig(
        thread_id="thread-env-claude",
        project_id="project-env-claude",
        stream=True,
        model_name="qwen3.5-plus",
        shadow_clone_mode=run_module.ShadowCloneMode.OFF,
    )
    runner = run_module.AgentRunner(config)
    await runner.setup()

    assert isinstance(runner.agentscope_runner, _FakeAgentScopeRunner)


@pytest.mark.asyncio
async def test_agent_runner_setup_allows_shadow_clone_v2_when_system_backend_is_agentscope(
    monkeypatch,
):
    """Shadow Clone V2 routes normally when the system backend is AgentScope."""
    monkeypatch.setenv("AGENT_BACKEND", "agentscope")
    monkeypatch.setenv("ALLOW_CLAUDE_SDK_BACKEND", "1")
    _patch_setup_dependencies(monkeypatch)
    monkeypatch.setattr(run_module, "ClaudeSDKRunner", _FakeClaudeSDKRunner)

    config = run_module.AgentConfig(
        thread_id="thread-env-v2",
        project_id="project-env-v2",
        stream=True,
        model_name="deepseek-v4-pro-max",
        agent_run_id="run-env-v2",
        shadow_clone_mode=run_module.ShadowCloneMode.V2,
    )
    runner = run_module.AgentRunner(config)
    await runner.setup()

    assert isinstance(runner.agentscope_runner, _FakeShadowCloneV2Runner)
    assert runner.agentscope_runner.kwargs["agent_run_id"] == "run-env-v2"
    assert runner.agentscope_runner.kwargs["account_id"] == "account-1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode", [run_module.ShadowCloneMode.ON, run_module.ShadowCloneMode.AUTO]
)
async def test_agent_runner_setup_routes_smart_and_shadow_clone_modes_to_v2(
    monkeypatch, mode
):
    """Frontend 智能/影分身 modes should both execute through Shadow Clone V2."""
    monkeypatch.setenv("AGENT_BACKEND", "agentscope")
    _patch_setup_dependencies(monkeypatch)

    config = run_module.AgentConfig(
        thread_id=f"thread-{mode.value}",
        project_id=f"project-{mode.value}",
        stream=True,
        model_name="deepseek-v4-pro-max",
        agent_run_id=f"run-{mode.value}",
        shadow_clone_mode=mode,
    )
    runner = run_module.AgentRunner(config)
    await runner.setup()

    assert isinstance(runner.agentscope_runner, _FakeShadowCloneV2Runner)
    assert runner.agentscope_runner.kwargs["mode"] == mode
    assert runner.agentscope_runner.kwargs["agent_run_id"] == f"run-{mode.value}"
    assert runner.agentscope_runner.kwargs["account_id"] == "account-1"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "backend", ["claude_sdk", "claude_agent_sdk", "claude-agent-sdk"]
)
@pytest.mark.parametrize(
    "mode", [run_module.ShadowCloneMode.ON, run_module.ShadowCloneMode.AUTO]
)
async def test_agent_runner_setup_routes_multi_agent_modes_to_claude_sdk_when_backend_is_claude(
    monkeypatch, backend, mode
):
    """Claude Agent SDK backend owns Auto/On execution instead of Shadow Clone V2."""
    monkeypatch.setenv("AGENT_BACKEND", backend)
    monkeypatch.setenv("ALLOW_CLAUDE_SDK_BACKEND", "1")
    _patch_setup_dependencies(monkeypatch)
    monkeypatch.setattr(run_module, "ClaudeSDKRunner", _FakeClaudeSDKRunner)

    config = run_module.AgentConfig(
        thread_id=f"thread-{backend}-{mode.value}",
        project_id=f"project-{backend}-{mode.value}",
        stream=True,
        model_name="deepseek-v4-pro-max",
        agent_run_id=f"run-{backend}-{mode.value}",
        shadow_clone_mode=mode,
    )
    runner = run_module.AgentRunner(config)
    await runner.setup()

    assert isinstance(runner.agentscope_runner, _FakeClaudeSDKRunner)


@pytest.mark.asyncio
async def test_agent_runner_setup_claude_sdk_system_backend_overrides_shadow_clone_v2(
    monkeypatch,
):
    """If the system backend is switched back to Claude SDK, it overrides Shadow Clone modes."""
    monkeypatch.setenv("AGENT_BACKEND", "claude_sdk")
    monkeypatch.setenv("ALLOW_CLAUDE_SDK_BACKEND", "1")
    _patch_setup_dependencies(monkeypatch)
    monkeypatch.setattr(run_module, "ClaudeSDKRunner", _FakeClaudeSDKRunner)

    config = run_module.AgentConfig(
        thread_id="thread-system-claude",
        project_id="project-system-claude",
        stream=True,
        model_name="deepseek-v4-pro-max",
        agent_run_id="run-system-claude",
        shadow_clone_mode=run_module.ShadowCloneMode.V2,
    )
    runner = run_module.AgentRunner(config)
    await runner.setup()

    assert isinstance(runner.agentscope_runner, _FakeClaudeSDKRunner)


def test_build_multi_agent_mode_instruction_preserves_selected_mode_intent():
    on_instruction = run_module._build_multi_agent_mode_instruction(
        run_module.ShadowCloneMode.ON
    )
    auto_instruction = run_module._build_multi_agent_mode_instruction(
        run_module.ShadowCloneMode.AUTO
    )

    assert on_instruction
    assert "multiple shadow clones/subagents" in on_instruction
    assert "wants to enable" in on_instruction
    assert auto_instruction
    assert "main agent" in auto_instruction
    assert "decide whether" in auto_instruction
    assert "multiple shadow clones/subagents" in auto_instruction
    assert (
        run_module._build_multi_agent_mode_instruction(run_module.ShadowCloneMode.OFF)
        == ""
    )


def test_augment_user_message_with_multi_agent_mode_appends_instruction_immutably():
    original = "Please research the market."

    augmented = run_module._augment_user_message_with_multi_agent_mode(
        original,
        run_module.ShadowCloneMode.ON,
    )

    assert original == "Please research the market."
    assert augmented.startswith(original)
    assert "multiple shadow clones/subagents" in augmented
    assert (
        run_module._augment_user_message_with_multi_agent_mode(
            original,
            run_module.ShadowCloneMode.OFF,
        )
        == original
    )


def test_augment_user_message_with_multi_agent_mode_preserves_original_text_exactly():
    original = "  Please keep my exact spacing.\n"

    augmented = run_module._augment_user_message_with_multi_agent_mode(
        original,
        run_module.ShadowCloneMode.AUTO,
    )

    assert augmented.startswith(original)
    assert "decide whether" in augmented


class _FakeTrace:
    def update_trace(self, **_kwargs):
        pass

    def update(self, **kwargs):
        self.last_update = kwargs

    def start_observation(self, **_kwargs):
        return None


class _FakeEventsQuery:
    def __init__(self):
        self._table_name = ""

    def select(self, *_args, **_kwargs):
        return self

    def eq(self, *_args, **_kwargs):
        return self

    def order(self, *_args, **_kwargs):
        return self

    def limit(self, *_args, **_kwargs):
        return self

    async def execute(self):
        return _FakeResult(
            data=[
                {
                    "author": "user",
                    "content": {"parts": [{"text": "Please build the report."}]},
                }
            ]
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mode, expected",
    [
        (run_module.ShadowCloneMode.ON, "wants to enable"),
        (run_module.ShadowCloneMode.AUTO, "decide whether"),
    ],
)
async def test_agent_runner_run_passes_multi_agent_mode_instruction_to_selected_runner(
    monkeypatch, mode, expected
):
    """The selected backend runner receives the prompt intent for Auto/On."""
    _patch_setup_dependencies(monkeypatch)

    config = run_module.AgentConfig(
        thread_id=f"thread-prompt-{mode.value}",
        project_id=f"project-prompt-{mode.value}",
        stream=True,
        model_name="deepseek-v4-pro-max",
        agent_run_id=f"run-prompt-{mode.value}",
        shadow_clone_mode=mode,
        trace=_FakeTrace(),
    )
    runner = run_module.AgentRunner(config)

    events = []
    async for event in runner.run():
        events.append(event)

    assert events == [{"type": "status", "status": "completed"}]
    user_message = runner.agentscope_runner.run_kwargs["user_message"]
    assert user_message.startswith("Please build the report.")
    assert "multiple shadow clones/subagents" in user_message
    assert expected in user_message


@pytest.mark.asyncio
async def test_agent_runner_run_passes_multi_agent_mode_instruction_to_claude_sdk_runner(
    monkeypatch,
):
    """Claude Agent SDK receives the same frontend multi-agent intent as V2."""
    monkeypatch.setenv("AGENT_BACKEND", "claude_agent_sdk")
    monkeypatch.setenv("ALLOW_CLAUDE_SDK_BACKEND", "1")
    _patch_setup_dependencies(monkeypatch)
    monkeypatch.setattr(run_module, "ClaudeSDKRunner", _FakeClaudeSDKRunner)

    config = run_module.AgentConfig(
        thread_id="thread-prompt-claude",
        project_id="project-prompt-claude",
        stream=True,
        model_name="deepseek-v4-pro-max",
        agent_run_id="run-prompt-claude",
        shadow_clone_mode=run_module.ShadowCloneMode.AUTO,
        trace=_FakeTrace(),
    )
    runner = run_module.AgentRunner(config)

    events = []
    async for event in runner.run():
        events.append(event)

    assert events == [{"type": "status", "status": "completed"}]
    assert isinstance(runner.agentscope_runner, _FakeClaudeSDKRunner)
    user_message = runner.agentscope_runner.run_kwargs["user_message"]
    assert user_message.startswith("Please build the report.")
    assert "main agent" in user_message
    assert "decide whether" in user_message
    assert "multiple shadow clones/subagents" in user_message


@pytest.mark.asyncio
async def test_agent_runner_setup_allows_explicit_claude_sdk_override(monkeypatch):
    """Keep Claude SDK available behind an explicit opt-in for debugging/regression checks."""
    monkeypatch.setenv("AGENT_BACKEND", "claude_sdk")
    monkeypatch.setenv("ALLOW_CLAUDE_SDK_BACKEND", "1")
    _patch_setup_dependencies(monkeypatch)
    monkeypatch.setattr(run_module, "ClaudeSDKRunner", _FakeClaudeSDKRunner)

    config = run_module.AgentConfig(
        thread_id="thread-explicit-claude",
        project_id="project-explicit-claude",
        stream=True,
        model_name="qwen3.5-plus",
        agent_config={"agent_backend": "claude_sdk"},
        shadow_clone_mode=run_module.ShadowCloneMode.OFF,
    )
    runner = run_module.AgentRunner(config)
    await runner.setup()

    assert isinstance(runner.agentscope_runner, _FakeClaudeSDKRunner)
