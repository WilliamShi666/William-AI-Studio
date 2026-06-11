import asyncio
import pytest
import json
import sys
import types
from unittest.mock import AsyncMock
from agentscope.message import Msg

import agentscope_integration.shadow_clone.coordinator as coordinator_module
import agentscope_integration.shadow_clone.subagent_actor as subagent_actor_module
import agentscope_integration.shadow_clone.state_machine as state_machine_module
import sandbox.api as sandbox_api
from agentscope_integration.shadow_clone.constants import ConfirmationResult, ShadowCloneMode


_ORIGINAL_WARMUP_PROJECT_SANDBOX = (
    coordinator_module.ShadowCloneCoordinator._warmup_project_sandbox
)
_ORIGINAL_ENSURE_BOOTSTRAP_STANDBY_CLONE = (
    coordinator_module.ShadowCloneCoordinator._ensure_bootstrap_standby_clone
)
_ORIGINAL_CHECKPOINT_COMPLETED_LAYER = (
    coordinator_module.ShadowCloneCoordinator._checkpoint_completed_layer
)
_ORIGINAL_BOOTSTRAP_SHADOW_CLONE_ENVIRONMENT = (
    coordinator_module.ShadowCloneCoordinator._bootstrap_shadow_clone_environment
)
_TEST_LEASES = {}


class _FakeMainAgent:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.on_proposal_captured = kwargs.get("on_proposal_captured")
        self.prepare_calls = []
        self.failure_review_messages = []
        self.decompose_result = kwargs.get(
            "decompose_result",
            {
                "subtasks": [
                    {
                        "id": "task-1",
                        "role": "researcher",
                        "task_description": "find",
                    },
                ],
                "dependencies": [],
            },
        )
        self.direct_answer = kwargs.get("direct_answer", "direct answer")
        self.layer_review_result = kwargs.get(
            "layer_review_result",
            {
                "action": "continue_with_partial_results",
                "subtask_ids": [],
                "rationale": "default review fallback",
            },
        )

    async def decompose(self, _user_message):
        return self.decompose_result

    async def stream_decompose(self, _user_message):
        if self.decompose_result is None:
            yield Msg(
                name="assistant",
                content=self.direct_answer,
                role="assistant",
            ), True
            return

        yield Msg(name="assistant", content="planning...", role="assistant"), False
        if callable(self.on_proposal_captured):
            self.on_proposal_captured(self.decompose_result)
        yield Msg(name="assistant", content="proposal ready", role="assistant"), True

    def get_proposal(self):
        return self.decompose_result

    async def aggregate(self):
        return "final report"

    async def stream_aggregate(self):
        yield Msg(name="assistant", content="partial final report", role="assistant"), False
        yield Msg(name="assistant", content="final report", role="assistant"), True

    async def stream_after_denial(self):
        yield Msg(name="assistant", content="direct after denial", role="assistant"), True

    async def stream_after_sandbox_failure(self, _recovery_message):
        yield Msg(name="assistant", content="recovered directly", role="assistant"), True

    async def review_failed_layer(self, review_message):
        self.failure_review_messages.append(review_message)
        return self.layer_review_result

    def get_last_response_text(self):
        return self.direct_answer

    async def prepare_shadow_clone_environment_commit(self, *, execution_epoch):
        self.prepare_calls.append(execution_epoch)
        return {
            "prepared_by": "fake_main_agent",
            "environment_commit_id": f"commit-{execution_epoch}",
            "prepared_at": "2026-03-22T00:00:00+00:00",
            "execution_epoch": execution_epoch,
            "sandbox": {
                "id": "sandbox-primary",
                "type": "code",
                "binding_state": "locked",
            },
            "skills": {
                "ready": True,
                "count": 1,
                "workspace_dir": "/workspace/skills",
            },
            "bootstrap": {
                "status": "ready",
                "mode": "copy",
                "target": "/workspace/claude_skills_sandbox_bootstrap.py",
                "metadata_path": "/workspace/skills/skills_metadata.json",
            },
        }


class _FakeSubAgentActor:
    def __init__(self):
        self.sent = []

    def send(self, **kwargs):
        self.sent.append(kwargs)


@pytest.fixture(autouse=True)
def _stub_stage_proposal_state(monkeypatch):
    async def _fake_stage_proposal_state(_run_id, **kwargs):
        return {
            "total": kwargs.get("total", 0),
            "proposal": {
                "subtasks": kwargs.get("subtasks", []),
                "dependencies": kwargs.get("dependencies", []),
            },
        }

    monkeypatch.setattr(
        coordinator_module,
        "stage_proposal_state",
        _fake_stage_proposal_state,
    )


@pytest.mark.asyncio
async def test_warmup_project_sandbox_uses_configured_shadow_clone_sandbox_type(monkeypatch):
    recorded = {}
    lease_reads = {"count": 0}

    class _FakeThreadManagerAdapter:
        def __init__(self, db_client=None):
            async def _client():
                return object()

            self.db = types.SimpleNamespace(client=_client())

    class _FakeSandboxToolsBase:
        def __init__(self, *, project_id, thread_manager, sandbox_type):
            recorded["init"] = {
                "project_id": project_id,
                "thread_manager": thread_manager,
                "sandbox_type": sandbox_type,
            }
            self.project_id = project_id
            self.thread_manager = thread_manager
            self.sandbox_type = sandbox_type
            self._sandbox_id = "sb-code"

        async def _ensure_sandbox(self):
            recorded["ensured"] = True

        async def _load_project_sandbox_info(self, _client):
            return {"id": "sb-code", "type": self.sandbox_type, "state": "running"}

    async def _fake_get_lease(_run_id):
        lease_reads["count"] += 1
        if lease_reads["count"] == 1:
            return None
        return {
            "run_id": _run_id,
            "thread_id": "thread-1",
            "project_id": "project-1",
            "sandbox_id": "sb-code",
            "sandbox_type": "code",
            "binding_state": "locked",
            "execution_epoch": 0,
            "lease_epoch": 0,
            "environment_status": "pending",
            "environment_ready": False,
            "environment_manifest": {},
        }

    async def _fake_save_lease(_run_id, **kwargs):
        recorded["lease"] = kwargs
        return {
            "run_id": _run_id,
            **kwargs,
        }

    monkeypatch.setattr(coordinator_module, "MainAgent", lambda **_kwargs: _FakeMainAgent())
    monkeypatch.setattr(coordinator_module.config, "get_shadow_clone_sandbox_type", lambda: "code")
    monkeypatch.setattr(coordinator_module, "get_run_sandbox_lease", _fake_get_lease)
    monkeypatch.setattr(coordinator_module, "save_run_sandbox_lease", _fake_save_lease)

    import agentscope_integration.adapters.thread_manager_adapter as thread_manager_module
    if "services.workspace_artifacts" not in sys.modules:
        workspace_artifacts_stub = types.SimpleNamespace(
            is_user_visible_workspace_artifact_path=lambda *_args, **_kwargs: False,
            workspace_artifacts=types.SimpleNamespace(),
        )
        sys.modules["services.workspace_artifacts"] = workspace_artifacts_stub
        setattr(sys.modules["services"], "workspace_artifacts", workspace_artifacts_stub)
    import sandbox.tool_base as tool_base_module

    monkeypatch.setattr(thread_manager_module, "ThreadManagerAdapter", _FakeThreadManagerAdapter)
    monkeypatch.setattr(tool_base_module, "SandboxToolsBase", _FakeSandboxToolsBase)

    coordinator = coordinator_module.ShadowCloneCoordinator(
        thread_id="thread-1",
        project_id="project-1",
        agent_run_id="run-1",
        model_key="model-1",
        db_client=object(),
        mode=ShadowCloneMode.AUTO,
    )

    lease = await _ORIGINAL_WARMUP_PROJECT_SANDBOX(coordinator)

    assert recorded["init"]["sandbox_type"] == "code"
    assert recorded["lease"]["sandbox_type"] == "code"
    assert lease["sandbox_type"] == "code"


@pytest.mark.asyncio
async def test_warmup_project_sandbox_fails_when_canonical_lease_does_not_round_trip(monkeypatch):
    class _FakeThreadManagerAdapter:
        def __init__(self, db_client=None):
            async def _client():
                return object()

            self.db = types.SimpleNamespace(client=_client())

    class _FakeSandboxToolsBase:
        def __init__(self, *, project_id, thread_manager, sandbox_type):
            self.project_id = project_id
            self.thread_manager = thread_manager
            self.sandbox_type = sandbox_type
            self._sandbox_id = "sb-missing"

        async def _ensure_sandbox(self):
            return None

        async def _load_project_sandbox_info(self, _client):
            return {"id": "sb-missing", "type": self.sandbox_type}

    async def _fake_get_lease(_run_id):
        return None

    async def _fake_save_lease(_run_id, **kwargs):
        return {"run_id": _run_id, **kwargs}

    monkeypatch.setattr(coordinator_module, "MainAgent", lambda **_kwargs: _FakeMainAgent())
    monkeypatch.setattr(coordinator_module, "get_run_sandbox_lease", _fake_get_lease)
    monkeypatch.setattr(coordinator_module, "save_run_sandbox_lease", _fake_save_lease)

    import agentscope_integration.adapters.thread_manager_adapter as thread_manager_module
    if "services.workspace_artifacts" not in sys.modules:
        workspace_artifacts_stub = types.SimpleNamespace(
            is_user_visible_workspace_artifact_path=lambda *_args, **_kwargs: False,
            workspace_artifacts=types.SimpleNamespace(),
        )
        sys.modules["services.workspace_artifacts"] = workspace_artifacts_stub
        setattr(sys.modules["services"], "workspace_artifacts", workspace_artifacts_stub)
    import sandbox.tool_base as tool_base_module

    monkeypatch.setattr(thread_manager_module, "ThreadManagerAdapter", _FakeThreadManagerAdapter)
    monkeypatch.setattr(tool_base_module, "SandboxToolsBase", _FakeSandboxToolsBase)

    coordinator = coordinator_module.ShadowCloneCoordinator(
        thread_id="thread-missing-lease",
        project_id="project-missing-lease",
        agent_run_id="run-missing-lease",
        model_key="model-1",
        db_client=object(),
        mode=ShadowCloneMode.AUTO,
    )

    with pytest.raises(RuntimeError, match="failed to reread the canonical strict sandbox lease"):
        await _ORIGINAL_WARMUP_PROJECT_SANDBOX(coordinator)


async def _collect(async_gen):
    items = []
    async for item in async_gen:
        items.append(item)
    return items


@pytest.fixture(autouse=True)
def _reset_test_leases():
    _TEST_LEASES.clear()
    yield
    _TEST_LEASES.clear()


@pytest.fixture(autouse=True)
def _stub_warmup(monkeypatch):
    async def _fake_warmup(self):
        lease = {
            "run_id": self.agent_run_id,
            "thread_id": self.thread_id,
            "project_id": self.project_id,
            "sandbox_id": "sandbox-primary",
            "sandbox_type": "code",
            "binding_state": "locked",
            "execution_epoch": 0,
            "lease_epoch": 0,
            "environment_status": "pending",
            "environment_ready": False,
            "environment_manifest": {},
        }
        _TEST_LEASES[self.agent_run_id] = dict(lease)
        return dict(lease)

    async def _fake_bootstrap(self):
        return None

    monkeypatch.setattr(
        coordinator_module.ShadowCloneCoordinator,
        "_warmup_project_sandbox",
        _fake_warmup,
    )
    monkeypatch.setattr(
        coordinator_module.ShadowCloneCoordinator,
        "_ensure_bootstrap_standby_clone",
        _fake_bootstrap,
    )


@pytest.fixture(autouse=True)
def _stub_preconfirm_environment_bootstrap(monkeypatch):
    async def _fake_bootstrap_environment(self, *, lease=None, execution_epoch=0):
        return {
            "prepared_by": "test_stub",
            "environment_commit_id": f"stub-{execution_epoch}",
            "prepared_at": "2026-03-22T00:00:00+00:00",
            "execution_epoch": execution_epoch,
            "sandbox": {
                "id": str((lease or {}).get("sandbox_id") or "sandbox-primary"),
                "type": str((lease or {}).get("sandbox_type") or "code"),
            },
            "skills": {
                "ready": True,
                "count": 1,
            },
        }

    monkeypatch.setattr(
        coordinator_module.ShadowCloneCoordinator,
        "_bootstrap_shadow_clone_environment",
        _fake_bootstrap_environment,
    )


@pytest.fixture(autouse=True)
def _default_subagent_execution_mode(monkeypatch):
    monkeypatch.setenv("SHADOW_CLONE_SUBAGENT_EXECUTION_MODE", "dramatiq")


def test_shadow_clone_subagent_execution_mode_defaults_to_local(monkeypatch):
    monkeypatch.delenv("SHADOW_CLONE_SUBAGENT_EXECUTION_MODE", raising=False)
    assert coordinator_module._shadow_clone_subagent_execution_mode() == "local"


@pytest.mark.parametrize("value", ["", "bogus", " workers "])
def test_shadow_clone_subagent_execution_mode_falls_back_to_local_for_invalid_values(
    monkeypatch,
    value,
):
    monkeypatch.setenv("SHADOW_CLONE_SUBAGENT_EXECUTION_MODE", value)
    assert coordinator_module._shadow_clone_subagent_execution_mode() == "local"


def test_shadow_clone_subagent_execution_mode_allows_explicit_dramatiq(monkeypatch):
    monkeypatch.setenv("SHADOW_CLONE_SUBAGENT_EXECUTION_MODE", "dramatiq")
    assert coordinator_module._shadow_clone_subagent_execution_mode() == "dramatiq"


def test_inject_default_subagent_model_only_fills_missing_models(monkeypatch):
    monkeypatch.setattr(coordinator_module, "MainAgent", lambda **_kwargs: _FakeMainAgent())

    coordinator = coordinator_module.ShadowCloneCoordinator(
        thread_id="thread-models",
        project_id="project-models",
        agent_run_id="run-models",
        model_key="kimi-k2.5",
        db_client=object(),
        mode=ShadowCloneMode.AUTO,
        subagent_model="openrouter/minimax/minimax-m2.5",
    )

    subtasks = [
        {"id": "task-1", "role": "researcher"},
        {"id": "task-2", "role": "coder", "model": "dashscope/qwen3.5-397b-a17b"},
    ]

    result = coordinator._inject_default_subagent_model(subtasks)

    assert result[0]["model"] == "openrouter/minimax/minimax-m2.5"
    assert result[1]["model"] == "dashscope/qwen3.5-397b-a17b"
    assert "model" not in subtasks[0]


def test_shadow_clone_activity_payload_keeps_reasoning_only_final_assistant_message():
    sse_message = {
        "sequence": 7,
        "type": "assistant",
        "content": json.dumps(
            {
                "role": "assistant",
                "content": "",
                "reasoning_content": "Compare the two implementation paths first.",
            }
        ),
        "metadata": json.dumps(
            {
                "stream_status": "complete",
                "shadow_clone_execution_epoch": 3,
            }
        ),
        "created_at": "2026-04-09T00:00:00+00:00",
        "updated_at": "2026-04-09T00:00:00+00:00",
    }

    activity_payload = subagent_actor_module._build_activity_payload(
        subtask_id="task-1",
        role="researcher",
        sse_message=sse_message,
    )

    assert activity_payload is not None
    assert activity_payload["message_type"] == "assistant"
    assert activity_payload["metadata"]["stream_status"] == "complete"
    assert (
        activity_payload["content"]["reasoning_content"]
        == "Compare the two implementation paths first."
    )


@pytest.fixture(autouse=True)
def _stub_runtime_state(monkeypatch):
    async def _fake_get_execution_epoch(_run_id):
        return 0

    async def _fake_get_state(_run_id):
        return {"status": "running", "subagents": {}}

    async def _fake_set_binding_state(*_args, **_kwargs):
        return None

    async def _fake_get_lease(_run_id):
        lease = _TEST_LEASES.get(_run_id)
        if lease is not None:
            return dict(lease)
        return {
            "run_id": _run_id,
            "project_id": "project-1",
            "binding_state": "locked",
            "sandbox_id": "sandbox-primary",
            "sandbox_type": "code",
            "execution_epoch": 0,
            "lease_epoch": 0,
            "environment_status": "ready",
            "environment_ready": True,
            "environment_manifest": {
                "execution_epoch": 0,
                "sandbox": {"id": "sandbox-primary"},
            },
        }

    async def _fake_mark_checkpoint(*_args, **_kwargs):
        return None

    async def _fake_update_lease(*_args, **_kwargs):
        run_id = _args[0] if _args else ""
        lease = dict(
            _TEST_LEASES.get(run_id)
            or {
                "run_id": run_id,
                "project_id": "project-1",
                "sandbox_id": "sandbox-primary",
                "sandbox_type": "code",
                "binding_state": "locked",
                "execution_epoch": 0,
                "lease_epoch": 0,
                "environment_status": "pending",
                "environment_ready": False,
                "environment_manifest": {},
            }
        )
        patch = dict(_kwargs)
        if isinstance(patch.get("environment_manifest"), dict):
            patch["environment_manifest"] = {
                **(lease.get("environment_manifest") or {}),
                **patch["environment_manifest"],
            }
        lease.update(patch)
        _TEST_LEASES[run_id] = dict(lease)
        return dict(lease)

    async def _fake_request_recovery(*_args, **_kwargs):
        return None

    async def _fake_checkpoint_completed_layer(self, *, layer_index, layer):
        return None

    async def _fake_update_environment(*_args, **_kwargs):
        return None

    async def _fake_sync_epoch(*_args, **_kwargs):
        run_id = _args[0] if _args else ""
        execution_epoch = int((_args[1] if len(_args) > 1 else _kwargs.get("execution_epoch")) or 0)
        lease = dict(
            _TEST_LEASES.get(run_id)
            or {
                "run_id": run_id,
                "project_id": "project-1",
                "sandbox_id": "sandbox-primary",
                "sandbox_type": "code",
                "binding_state": "locked",
                "environment_status": "pending",
                "environment_ready": False,
                "environment_manifest": {},
            }
        )
        lease["execution_epoch"] = execution_epoch
        lease["lease_epoch"] = execution_epoch
        _TEST_LEASES[run_id] = dict(lease)
        return dict(lease)

    class _DefaultPubSub:
        async def subscribe(self, *args):
            return None

        async def get_message(self, **kwargs):
            return None

        async def unsubscribe(self, *args):
            return None

        async def close(self):
            return None

    async def _fake_create_pubsub():
        return _DefaultPubSub()

    monkeypatch.setattr(coordinator_module, "get_execution_epoch", _fake_get_execution_epoch)
    monkeypatch.setattr(coordinator_module, "get_state", _fake_get_state)
    monkeypatch.setattr(coordinator_module, "set_run_sandbox_binding_state", _fake_set_binding_state)
    monkeypatch.setattr(coordinator_module, "get_run_sandbox_lease", _fake_get_lease)
    monkeypatch.setattr(coordinator_module, "mark_checkpoint", _fake_mark_checkpoint)
    monkeypatch.setattr(coordinator_module, "update_run_sandbox_lease", _fake_update_lease)
    monkeypatch.setattr(coordinator_module, "request_recovery", _fake_request_recovery)
    monkeypatch.setattr(coordinator_module, "update_environment", _fake_update_environment)
    monkeypatch.setattr(
        coordinator_module,
        "sync_run_sandbox_execution_epoch",
        _fake_sync_epoch,
    )
    monkeypatch.setattr(
        coordinator_module.ShadowCloneCoordinator,
        "_checkpoint_completed_layer",
        _fake_checkpoint_completed_layer,
    )
    monkeypatch.setattr(
        coordinator_module.redis_service,
        "create_pubsub",
        _fake_create_pubsub,
        raising=False,
    )


@pytest.mark.asyncio
async def test_coordinator_happy_path(monkeypatch):
    fake_actor = _FakeSubAgentActor()
    calls = {"transition": [], "init_state": []}
    visible_result_calls = []

    monkeypatch.setattr(coordinator_module, "MainAgent", _FakeMainAgent)
    async def _confirmed(*args, **kwargs):
        return ConfirmationResult.CONFIRMED

    monkeypatch.setattr(coordinator_module, "wait_for_confirmation", _confirmed)
    monkeypatch.setattr(coordinator_module, "run_subagent", fake_actor)
    monkeypatch.setattr(
        coordinator_module,
        "topological_sort_layers",
        lambda subtasks, deps: [subtasks],
    )

    async def _fake_init_state(*args, **kwargs):
        calls["init_state"].append((args, kwargs))
        return None

    async def _fake_transition(*args, **kwargs):
        calls["transition"].append((args, kwargs))
        return True

    async def _fake_update_subagent(*args, **kwargs):
        return None

    async def _fake_cleanup(*args, **kwargs):
        return None

    async def _fake_cleanup_results(*args, **kwargs):
        return 0

    async def _fake_ensure_visible_results(_run_id, expected_results, *, timeout=None):
        visible_result_calls.append(
            {
                "run_id": _run_id,
                "expected_results": expected_results,
                "timeout": timeout,
            }
        )
        return []

    monkeypatch.setattr(coordinator_module, "init_state", _fake_init_state)
    monkeypatch.setattr(coordinator_module, "transition", _fake_transition)
    monkeypatch.setattr(coordinator_module, "update_subagent", _fake_update_subagent)
    monkeypatch.setattr(coordinator_module, "cleanup", _fake_cleanup)
    monkeypatch.setattr(coordinator_module, "cleanup_results", _fake_cleanup_results)
    monkeypatch.setattr(
        coordinator_module,
        "ensure_terminal_result_summaries_visible",
        _fake_ensure_visible_results,
        raising=False,
    )

    coordinator = coordinator_module.ShadowCloneCoordinator(
        thread_id="thread-1",
        project_id="project-1",
        agent_run_id="run-1",
        model_key="kimi-k2.5",
        db_client=object(),
        mode=ShadowCloneMode.AUTO,
    )

    async def _fake_wait_for_layer(layer, timeout=0):
        for item in layer:
            yield {"type": "subagent_completed", "subtask_id": item["id"]}

    monkeypatch.setattr(coordinator, "_wait_for_layer_completion", _fake_wait_for_layer)

    events = await _collect(
        coordinator.run(user_message="complex", thread_run_id="tr-1"),
    )
    event_types = [event["type"] for event in events]
    assert "shadow_clone_planning_started" in event_types
    assert any(
        event["type"] == "status"
        and event.get("status") == "pending"
        and event.get("ui_phase") == "planning"
        and event.get("phase_reason") == "planning_started"
        for event in events
    )
    assert "shadow_clone_environment_preparing" in event_types
    assert "shadow_clone_environment_ready" in event_types
    assert "shadow_clone_proposed" in event_types
    assert any(
        event["type"] == "status"
        and event.get("status") == "confirming"
        and event.get("ui_phase") == "confirming"
        and event.get("phase_reason") == "confirmation_waiting"
        for event in events
    )
    assert "subagent_started" in event_types
    assert "shadow_clone_aggregating" in event_types
    assert "shadow_clone_complete" in event_types
    planning_events = [item for item in events if item["type"] == "passthrough_message"]
    assert planning_events
    assert planning_events[0]["route_metadata"]["shadow_clone_phase"] == "planning"
    assert planning_events[0]["route_metadata"]["live_activity"]["scope"] == "shadow_clone_main"
    assert planning_events[0]["route_metadata"]["live_activity"]["phase"] == "planning"
    assert planning_events[0]["route_metadata"]["activity_owner"] == "shadow_clone"
    assert planning_events[0]["route_metadata"]["ui_phase"] == "planning"
    assert planning_events[0]["route_metadata"]["phase_reason"] == "planning_started"
    proposal_event = next(item for item in events if item["type"] == "shadow_clone_proposed")
    assert proposal_event["live_activity"]["scope"] == "shadow_clone_main"
    assert proposal_event["live_activity"]["phase"] == "confirming"
    assert proposal_event["activity_owner"] == "shadow_clone"
    assert proposal_event["ui_phase"] == "confirming"
    assert proposal_event["phase_reason"] == "proposal_created"
    planning_status_event = next(
        item
        for item in events
        if item["type"] == "status"
        and item.get("status") == "pending"
        and item.get("ui_phase") == "planning"
    )
    assert planning_status_event["live_activity"]["scope"] == "shadow_clone_main"
    assert planning_status_event["live_activity"]["phase"] == "planning"
    assert planning_status_event["phase_reason"] == "planning_started"
    preparing_event = next(
        item for item in events if item["type"] == "shadow_clone_environment_preparing"
    )
    assert preparing_event["activity_owner"] == "shadow_clone"
    assert preparing_event["ui_phase"] == "preparing_environment"
    assert preparing_event["phase_reason"] == "initial"
    confirmation_waiting_event = next(
        item
        for item in events
        if item["type"] == "status"
        and item.get("status") == "confirming"
        and item.get("phase_reason") == "confirmation_waiting"
    )
    assert confirmation_waiting_event["live_activity"]["scope"] == "shadow_clone_main"
    assert confirmation_waiting_event["live_activity"]["phase"] == "confirming"
    assert confirmation_waiting_event["activity_owner"] == "shadow_clone"
    assert confirmation_waiting_event["ui_phase"] == "confirming"
    aggregating_event = next(item for item in events if item["type"] == "shadow_clone_aggregating")
    assert aggregating_event["live_activity"]["scope"] == "main_agent"
    assert aggregating_event["live_activity"]["phase"] == "aggregate"
    assert aggregating_event["activity_owner"] == "main_agent"
    assert aggregating_event["ui_phase"] == "aggregating"
    assert aggregating_event["phase_reason"] == "aggregate_started"
    assert events[-1]["completion_mode"] == "aggregate"
    assert events[-1]["terminal_reason"] == "aggregate_complete"
    assert events[-1]["activity_owner"] == "none"
    assert events[-1]["ui_phase"] == "completed"
    assert events[-1]["phase_reason"] == "aggregate_complete"
    assert fake_actor.sent
    assert visible_result_calls == [
        {
            "run_id": "run-1",
            "expected_results": [
                {
                    "subtask_id": "task-1",
                    "role": None,
                    "status": "completed",
                    "summary": None,
                    "attempt_index": None,
                    "failure_class": None,
                }
            ],
            "timeout": None,
        }
    ]
    assert len(calls["init_state"]) == 1
    assert calls["init_state"][0][1]["total"] == 0

    env_preparing_index = event_types.index("shadow_clone_environment_preparing")
    env_ready_index = event_types.index("shadow_clone_environment_ready")
    planning_started_index = event_types.index("shadow_clone_planning_started")
    planning_status_index = next(
        index
        for index, event in enumerate(events)
        if event["type"] == "status"
        and event.get("status") == "pending"
        and event.get("ui_phase") == "planning"
    )
    first_planning_index = next(
        index
        for index, event in enumerate(events)
        if event["type"] == "passthrough_message"
        and event.get("route_metadata", {}).get("shadow_clone_phase") == "planning"
    )
    proposal_index = event_types.index("shadow_clone_proposed")
    confirmation_waiting_index = next(
        index
        for index, event in enumerate(events)
        if event["type"] == "status"
        and event.get("status") == "confirming"
        and event.get("phase_reason") == "confirmation_waiting"
    )
    started_index = event_types.index("subagent_started")
    aggregating_index = event_types.index("shadow_clone_aggregating")
    completed_indexes = [
        index
        for index, event_type in enumerate(event_types)
        if event_type == "subagent_completed"
    ]
    assert planning_started_index < first_planning_index
    assert planning_started_index < planning_status_index
    assert planning_status_index < proposal_index
    assert planning_started_index < proposal_index
    assert proposal_index < env_preparing_index
    assert env_preparing_index < env_ready_index
    assert env_ready_index < confirmation_waiting_index
    assert confirmation_waiting_index < started_index
    assert env_ready_index < started_index
    assert completed_indexes
    assert max(completed_indexes) < aggregating_index


@pytest.mark.asyncio
async def test_coordinator_confirmation_wait_emits_heartbeat_until_resolved(monkeypatch):
    fake_actor = _FakeSubAgentActor()

    monkeypatch.setattr(coordinator_module, "MainAgent", _FakeMainAgent)
    monkeypatch.setattr(
        coordinator_module,
        "SHADOW_CLONE_CONFIRMATION_WAIT_HEARTBEAT_INTERVAL_SECONDS",
        0.01,
        raising=False,
    )

    async def _confirmed(*args, **kwargs):
        await asyncio.sleep(0.035)
        return ConfirmationResult.CONFIRMED

    monkeypatch.setattr(coordinator_module, "wait_for_confirmation", _confirmed)
    monkeypatch.setattr(coordinator_module, "run_subagent", fake_actor)
    monkeypatch.setattr(
        coordinator_module,
        "topological_sort_layers",
        lambda subtasks, deps: [subtasks],
    )

    async def _fake_init_state(*args, **kwargs):
        return None

    async def _fake_transition(*args, **kwargs):
        return True

    async def _fake_update_subagent(*args, **kwargs):
        return None

    async def _fake_cleanup(*args, **kwargs):
        return None

    async def _fake_cleanup_results(*args, **kwargs):
        return 0

    async def _fake_ensure_visible_results(*args, **kwargs):
        return []

    monkeypatch.setattr(coordinator_module, "init_state", _fake_init_state)
    monkeypatch.setattr(coordinator_module, "transition", _fake_transition)
    monkeypatch.setattr(coordinator_module, "update_subagent", _fake_update_subagent)
    monkeypatch.setattr(coordinator_module, "cleanup", _fake_cleanup)
    monkeypatch.setattr(coordinator_module, "cleanup_results", _fake_cleanup_results)
    monkeypatch.setattr(
        coordinator_module,
        "ensure_terminal_result_summaries_visible",
        _fake_ensure_visible_results,
        raising=False,
    )

    coordinator = coordinator_module.ShadowCloneCoordinator(
        thread_id="thread-1",
        project_id="project-1",
        agent_run_id="run-1",
        model_key="kimi-k2.5",
        db_client=object(),
        mode=ShadowCloneMode.AUTO,
    )

    async def _fake_wait_for_layer(layer, timeout=0):
        for item in layer:
            yield {"type": "subagent_completed", "subtask_id": item["id"]}

    monkeypatch.setattr(coordinator, "_wait_for_layer_completion", _fake_wait_for_layer)

    events = await _collect(
        coordinator.run(user_message="complex", thread_run_id="tr-1"),
    )

    confirmation_waiting_events = [
        event
        for event in events
        if event["type"] == "status"
        and event.get("status") == "confirming"
        and event.get("phase_reason") == "confirmation_waiting"
    ]

    assert len(confirmation_waiting_events) >= 2


@pytest.mark.asyncio
async def test_coordinator_confirmation_wait_emits_heartbeat_before_timeout(monkeypatch):
    monkeypatch.setattr(coordinator_module, "MainAgent", _FakeMainAgent)
    monkeypatch.setattr(
        coordinator_module,
        "SHADOW_CLONE_CONFIRMATION_WAIT_HEARTBEAT_INTERVAL_SECONDS",
        0.01,
        raising=False,
    )

    async def _timed_out(*args, **kwargs):
        await asyncio.sleep(0.035)
        return ConfirmationResult.TIMEOUT

    async def _fake_init_state(*args, **kwargs):
        return None

    async def _fake_transition(*args, **kwargs):
        return True

    async def _fake_update_subagent(*args, **kwargs):
        return None

    async def _fake_cleanup(*args, **kwargs):
        return None

    async def _fake_cleanup_results(*args, **kwargs):
        return 0

    monkeypatch.setattr(coordinator_module, "wait_for_confirmation", _timed_out)
    monkeypatch.setattr(coordinator_module, "init_state", _fake_init_state)
    monkeypatch.setattr(coordinator_module, "transition", _fake_transition)
    monkeypatch.setattr(coordinator_module, "update_subagent", _fake_update_subagent)
    monkeypatch.setattr(coordinator_module, "cleanup", _fake_cleanup)
    monkeypatch.setattr(coordinator_module, "cleanup_results", _fake_cleanup_results)

    coordinator = coordinator_module.ShadowCloneCoordinator(
        thread_id="thread-timeout",
        project_id="project-timeout",
        agent_run_id="run-timeout",
        model_key="kimi-k2.5",
        db_client=object(),
        mode=ShadowCloneMode.AUTO,
    )

    events = await _collect(
        coordinator.run(user_message="complex", thread_run_id="tr-timeout"),
    )

    confirmation_waiting_events = [
        event
        for event in events
        if event["type"] == "status"
        and event.get("status") == "confirming"
        and event.get("phase_reason") == "confirmation_waiting"
    ]

    assert len(confirmation_waiting_events) >= 2
    assert events[-1]["type"] == "status"
    assert events[-1]["status"] == "timeout"
    assert events[-1]["terminal_reason"] == "confirmation_timeout"
    assert events[-1]["activity_owner"] == "none"
    assert events[-1]["ui_phase"] == "timeout"
    assert events[-1]["phase_reason"] == "confirmation_timeout"


@pytest.mark.asyncio
async def test_coordinator_retries_only_failed_retryable_subtasks(monkeypatch):
    fake_actor = _FakeSubAgentActor()
    recovery_calls = []
    decision_calls = []
    deleted_results = []
    retry_preparations = []
    mutable_state = {
        "status": "running",
        "execution_epoch": 0,
        "subagents": {
            "task-1": {"status": "pending", "role": "researcher", "attempt_index": 1},
            "task-2": {"status": "pending", "role": "designer", "attempt_index": 1},
        },
    }

    monkeypatch.setattr(
        coordinator_module,
        "MainAgent",
        lambda **kwargs: _FakeMainAgent(
            **kwargs,
            decompose_result={
                "subtasks": [
                    {
                        "id": "task-1",
                        "role": "researcher",
                        "task_description": "research",
                    },
                    {
                        "id": "task-2",
                        "role": "designer",
                        "task_description": "design",
                    },
                ],
                "dependencies": [],
            },
            layer_review_result={
                "action": "retry_failed_subtasks",
                "subtask_ids": ["task-2"],
                "rationale": "The failure was a timeout and should be retried once.",
            },
        ),
    )
    monkeypatch.setattr(
        coordinator_module,
        "wait_for_confirmation",
        lambda *args, **kwargs: asyncio.sleep(0, result=ConfirmationResult.CONFIRMED),
    )
    monkeypatch.setattr(coordinator_module, "run_subagent", fake_actor)
    monkeypatch.setattr(
        coordinator_module,
        "topological_sort_layers",
        lambda subtasks, deps: [subtasks],
    )
    monkeypatch.setattr(
        coordinator_module,
        "topological_sort_layers",
        lambda subtasks, deps: [subtasks],
    )

    async def _fake_init_state(*args, **kwargs):
        return None

    async def _fake_transition(*args, **kwargs):
        return True

    async def _fake_update_subagent(*args, **kwargs):
        return None

    async def _fake_cleanup(*args, **kwargs):
        return None

    async def _fake_cleanup_results(*args, **kwargs):
        return 0

    async def _fake_initialize_attempts(_run_id, *, subtask_ids=None):
        for subtask_id in subtask_ids or []:
            mutable_state["subagents"].setdefault(
                subtask_id,
                {"status": "pending", "attempt_index": 1},
            )
            mutable_state["subagents"][subtask_id]["attempt_index"] = 1
        return mutable_state

    async def _fake_get_state(_run_id):
        return mutable_state

    async def _fake_get_epoch(_run_id):
        return mutable_state["execution_epoch"]

    async def _fake_record_failures(_run_id, *, failed_subtasks):
        for item in failed_subtasks:
            payload = mutable_state["subagents"].setdefault(item["subtask_id"], {})
            payload["status"] = "failed"
            payload["last_error"] = item.get("error")
            payload["failure_class"] = item.get("failure_class")
        return mutable_state

    async def _fake_request_recovery(_run_id, **kwargs):
        recovery_calls.append(kwargs)
        return mutable_state

    async def _fake_record_recovery_decision(_run_id, **kwargs):
        decision_calls.append(kwargs)
        return mutable_state

    async def _fake_bump_epoch(_run_id):
        mutable_state["execution_epoch"] += 1
        return mutable_state["execution_epoch"]

    async def _fake_sync_epoch(_run_id, execution_epoch):
        lease = dict(
            _TEST_LEASES.get(_run_id)
            or {
                "run_id": _run_id,
                "thread_id": "thread-retry",
                "project_id": "project-retry",
                "sandbox_id": "sandbox-primary",
                "sandbox_type": "code",
                "binding_state": "locked",
                "environment_status": "pending",
                "environment_ready": False,
                "environment_manifest": {},
            }
        )
        lease["execution_epoch"] = execution_epoch
        lease["lease_epoch"] = execution_epoch
        _TEST_LEASES[_run_id] = dict(lease)
        return dict(lease)

    async def _fake_delete_results(_run_id, subtask_ids):
        deleted_results.append(list(subtask_ids))
        return len(subtask_ids)

    async def _fake_prepare_retry(_run_id, subtask_ids, *, expected_epoch, roles, timeout=None):
        retry_preparations.append(
            {
                "subtask_ids": list(subtask_ids),
                "expected_epoch": expected_epoch,
                "roles": dict(roles),
            }
        )
        for subtask_id in subtask_ids:
            payload = mutable_state["subagents"].setdefault(subtask_id, {})
            payload["status"] = "pending"
            payload["attempt_index"] = 2
            payload["last_error"] = None
            payload["failure_class"] = None
        return mutable_state

    monkeypatch.setattr(coordinator_module, "init_state", _fake_init_state)
    monkeypatch.setattr(coordinator_module, "transition", _fake_transition)
    monkeypatch.setattr(coordinator_module, "update_subagent", _fake_update_subagent)
    monkeypatch.setattr(coordinator_module, "cleanup", _fake_cleanup)
    monkeypatch.setattr(coordinator_module, "cleanup_results", _fake_cleanup_results)
    monkeypatch.setattr(
        coordinator_module,
        "initialize_subagent_attempts",
        _fake_initialize_attempts,
    )
    monkeypatch.setattr(coordinator_module, "get_state", _fake_get_state)
    monkeypatch.setattr(coordinator_module, "get_execution_epoch", _fake_get_epoch)
    monkeypatch.setattr(
        coordinator_module,
        "record_subagent_failures",
        _fake_record_failures,
    )
    monkeypatch.setattr(coordinator_module, "request_recovery", _fake_request_recovery)
    monkeypatch.setattr(
        coordinator_module,
        "record_recovery_decision",
        _fake_record_recovery_decision,
    )
    monkeypatch.setattr(coordinator_module, "bump_execution_epoch", _fake_bump_epoch)
    monkeypatch.setattr(
        coordinator_module,
        "sync_run_sandbox_execution_epoch",
        _fake_sync_epoch,
    )
    monkeypatch.setattr(coordinator_module, "delete_results", _fake_delete_results)
    monkeypatch.setattr(
        coordinator_module,
        "prepare_subagents_for_retry",
        _fake_prepare_retry,
    )

    coordinator = coordinator_module.ShadowCloneCoordinator(
        thread_id="thread-retry",
        project_id="project-retry",
        agent_run_id="run-retry",
        model_key="kimi-k2.5",
        db_client=object(),
        mode=ShadowCloneMode.AUTO,
    )

    wait_call_count = {"count": 0}

    async def _fake_wait_for_layer(layer, timeout=0, **kwargs):
        wait_call_count["count"] += 1
        if wait_call_count["count"] == 1:
            assert [item["id"] for item in layer] == ["task-1", "task-2"]
            yield {
                "type": "subagent_completed",
                "subtask_id": "task-1",
                "attempt_index": 1,
            }
            yield {
                "type": "subagent_failed",
                "subtask_id": "task-2",
                "error": "SubAgent timed out after 300 seconds.",
                "failure_class": "timeout",
                "attempt_index": 1,
            }
            return

        assert [item["id"] for item in layer] == ["task-2"]
        yield {
            "type": "subagent_completed",
            "subtask_id": "task-2",
            "attempt_index": 2,
        }

    monkeypatch.setattr(coordinator, "_wait_for_layer_completion", _fake_wait_for_layer)

    events = await _collect(
        coordinator.run(user_message="complex", thread_run_id="tr-retry"),
    )

    assert wait_call_count["count"] == 2
    assert any(event["type"] == "shadow_clone_aggregating" for event in events)
    assert [item["subtask_id"] for item in fake_actor.sent] == ["task-1", "task-2", "task-2"]
    assert fake_actor.sent[0]["subtask_config"]["attempt_index"] == 1
    assert fake_actor.sent[2]["subtask_config"]["attempt_index"] == 2
    assert recovery_calls[0]["kind"] == "layer_review"
    assert recovery_calls[0]["retryable_subtasks"] == ["task-2"]
    assert decision_calls[0]["decision"] == "retry_failed_subtasks"
    assert deleted_results == [["task-2"]]
    assert retry_preparations == [
        {
            "subtask_ids": ["task-2"],
            "expected_epoch": 1,
            "roles": {"task-1": "researcher", "task-2": "designer"},
        }
    ]


@pytest.mark.asyncio
async def test_coordinator_continues_with_partial_results_after_failed_layer_review(monkeypatch):
    fake_actor = _FakeSubAgentActor()
    recovery_calls = []
    decision_calls = []
    release_calls = []
    visible_result_calls = []
    mutable_state = {
        "status": "running",
        "execution_epoch": 0,
        "subagents": {
            "task-1": {"status": "pending", "role": "researcher", "attempt_index": 1},
            "task-2": {"status": "pending", "role": "designer", "attempt_index": 1},
        },
    }

    monkeypatch.setattr(
        coordinator_module,
        "MainAgent",
        lambda **kwargs: _FakeMainAgent(
            **kwargs,
            decompose_result={
                "subtasks": [
                    {
                        "id": "task-1",
                        "role": "researcher",
                        "task_description": "research",
                    },
                    {
                        "id": "task-2",
                        "role": "designer",
                        "task_description": "design",
                    },
                ],
                "dependencies": [],
            },
            layer_review_result={
                "action": "continue_with_partial_results",
                "subtask_ids": [],
                "rationale": "Tooling failures are unlikely to improve with a blind retry.",
            },
        ),
    )
    monkeypatch.setattr(
        coordinator_module,
        "wait_for_confirmation",
        lambda *args, **kwargs: asyncio.sleep(0, result=ConfirmationResult.CONFIRMED),
    )
    monkeypatch.setattr(coordinator_module, "run_subagent", fake_actor)
    monkeypatch.setattr(
        coordinator_module,
        "topological_sort_layers",
        lambda subtasks, deps: [subtasks],
    )

    async def _fake_init_state(*args, **kwargs):
        return None

    async def _fake_transition(*args, **kwargs):
        return True

    async def _fake_update_subagent(*args, **kwargs):
        return None

    async def _fake_cleanup(*args, **kwargs):
        return None

    async def _fake_cleanup_results(*args, **kwargs):
        return 0

    async def _fake_ensure_visible_results(_run_id, expected_results, *, timeout=None):
        visible_result_calls.append(
            {
                "run_id": _run_id,
                "expected_results": expected_results,
                "timeout": timeout,
            }
        )
        return []

    async def _fake_initialize_attempts(_run_id, *, subtask_ids=None):
        for subtask_id in subtask_ids or []:
            mutable_state["subagents"].setdefault(
                subtask_id,
                {"status": "pending", "attempt_index": 1},
            )
            mutable_state["subagents"][subtask_id]["attempt_index"] = 1
        return mutable_state

    async def _fake_get_state(_run_id):
        return mutable_state

    async def _fake_get_epoch(_run_id):
        return mutable_state["execution_epoch"]

    async def _fake_record_failures(_run_id, *, failed_subtasks):
        for item in failed_subtasks:
            payload = mutable_state["subagents"].setdefault(item["subtask_id"], {})
            payload["status"] = "failed"
            payload["last_error"] = item.get("error")
            payload["failure_class"] = item.get("failure_class")
        return mutable_state

    async def _fake_request_recovery(_run_id, **kwargs):
        recovery_calls.append(kwargs)
        return mutable_state

    async def _fake_record_recovery_decision(_run_id, **kwargs):
        decision_calls.append(kwargs)
        return mutable_state

    async def _fake_set_binding_state(_run_id, binding_state, *, last_error=None):
        release_calls.append((binding_state, last_error))
        return None

    delete_results = AsyncMock(return_value=0)
    prepare_retry = AsyncMock(return_value=mutable_state)

    monkeypatch.setattr(coordinator_module, "init_state", _fake_init_state)
    monkeypatch.setattr(coordinator_module, "transition", _fake_transition)
    monkeypatch.setattr(coordinator_module, "update_subagent", _fake_update_subagent)
    monkeypatch.setattr(coordinator_module, "cleanup", _fake_cleanup)
    monkeypatch.setattr(coordinator_module, "cleanup_results", _fake_cleanup_results)
    monkeypatch.setattr(
        coordinator_module,
        "initialize_subagent_attempts",
        _fake_initialize_attempts,
    )
    monkeypatch.setattr(coordinator_module, "get_state", _fake_get_state)
    monkeypatch.setattr(coordinator_module, "get_execution_epoch", _fake_get_epoch)
    monkeypatch.setattr(
        coordinator_module,
        "record_subagent_failures",
        _fake_record_failures,
    )
    monkeypatch.setattr(coordinator_module, "request_recovery", _fake_request_recovery)
    monkeypatch.setattr(
        coordinator_module,
        "record_recovery_decision",
        _fake_record_recovery_decision,
    )
    monkeypatch.setattr(
        coordinator_module,
        "set_run_sandbox_binding_state",
        _fake_set_binding_state,
    )
    monkeypatch.setattr(coordinator_module, "delete_results", delete_results)
    monkeypatch.setattr(
        coordinator_module,
        "prepare_subagents_for_retry",
        prepare_retry,
    )
    monkeypatch.setattr(
        coordinator_module,
        "ensure_terminal_result_summaries_visible",
        _fake_ensure_visible_results,
        raising=False,
    )

    coordinator = coordinator_module.ShadowCloneCoordinator(
        thread_id="thread-partial",
        project_id="project-partial",
        agent_run_id="run-partial",
        model_key="kimi-k2.5",
        db_client=object(),
        mode=ShadowCloneMode.AUTO,
    )

    async def _fake_wait_for_layer(layer, timeout=0, **kwargs):
        assert [item["id"] for item in layer] == ["task-1", "task-2"]
        yield {
            "type": "subagent_completed",
            "subtask_id": "task-1",
            "attempt_index": 1,
        }
        yield {
            "type": "subagent_failed",
            "subtask_id": "task-2",
            "error": "html2pptx binary was not found.",
            "failure_class": "tooling",
            "attempt_index": 1,
        }

    monkeypatch.setattr(coordinator, "_wait_for_layer_completion", _fake_wait_for_layer)

    events = await _collect(
        coordinator.run(user_message="complex", thread_run_id="tr-partial"),
    )

    passthrough_events = [event for event in events if event["type"] == "passthrough_message"]
    assert any(event["msg"].content == "recovered directly" for event in passthrough_events)
    assert any(
        event["route_metadata"].get("live_activity", {}).get("scope") == "main_agent"
        and event["route_metadata"].get("live_activity", {}).get("phase") == "execution"
        for event in passthrough_events
    )
    assert not any(event["type"] == "shadow_clone_aggregating" for event in events)
    assert events[-1]["type"] == "shadow_clone_complete"
    assert events[-1]["completion_mode"] == "failed_layer_continue"
    assert events[-1]["terminal_reason"] == "failed_layer_continue_complete"
    assert recovery_calls[0]["kind"] == "layer_review"
    assert decision_calls[0]["decision"] == "continue_with_partial_results"
    assert delete_results.await_count == 0
    assert prepare_retry.await_count == 0
    assert release_calls[-1] == ("released", None)
    assert visible_result_calls == [
        {
            "run_id": "run-partial",
            "expected_results": [
                {
                    "subtask_id": "task-1",
                    "role": None,
                    "status": "completed",
                    "summary": None,
                    "attempt_index": 1,
                    "failure_class": None,
                },
                {
                    "subtask_id": "task-2",
                    "role": None,
                    "status": "failed",
                    "summary": "html2pptx binary was not found.",
                    "attempt_index": 1,
                    "failure_class": "tooling",
                },
            ],
            "timeout": None,
        }
    ]


@pytest.mark.asyncio
async def test_coordinator_run_stops_without_aggregating_when_layer_wait_reports_stop(monkeypatch):
    fake_actor = _FakeSubAgentActor()
    cleanup_calls = []
    mutable_state = {
        "status": "running",
        "execution_epoch": 0,
        "subagents": {
            "task-1": {"status": "pending", "role": "researcher", "attempt_index": 1},
        },
    }

    monkeypatch.setattr(
        coordinator_module,
        "MainAgent",
        lambda **kwargs: _FakeMainAgent(
            **kwargs,
            decompose_result={
                "subtasks": [
                    {
                        "id": "task-1",
                        "role": "researcher",
                        "task_description": "research",
                    },
                ],
                "dependencies": [],
            },
        ),
    )
    monkeypatch.setattr(
        coordinator_module,
        "wait_for_confirmation",
        lambda *args, **kwargs: asyncio.sleep(0, result=ConfirmationResult.CONFIRMED),
    )
    monkeypatch.setattr(coordinator_module, "run_subagent", fake_actor)
    monkeypatch.setattr(
        coordinator_module,
        "topological_sort_layers",
        lambda subtasks, deps: [subtasks],
    )

    async def _fake_init_state(*args, **kwargs):
        return None

    async def _fake_transition(*args, **kwargs):
        return True

    async def _fake_update_subagent(*args, **kwargs):
        return None

    async def _fake_cleanup(*args, **kwargs):
        cleanup_calls.append(args[0] if args else kwargs.get("run_id"))
        return None

    async def _fake_cleanup_results(*args, **kwargs):
        return 0

    async def _fake_initialize_attempts(_run_id, *, subtask_ids=None):
        for subtask_id in subtask_ids or []:
            mutable_state["subagents"].setdefault(
                subtask_id,
                {"status": "pending", "attempt_index": 1},
            )
        return mutable_state

    async def _fake_get_state(_run_id):
        return mutable_state

    async def _fake_get_epoch(_run_id):
        return mutable_state["execution_epoch"]

    monkeypatch.setattr(coordinator_module, "init_state", _fake_init_state)
    monkeypatch.setattr(coordinator_module, "transition", _fake_transition)
    monkeypatch.setattr(coordinator_module, "update_subagent", _fake_update_subagent)
    monkeypatch.setattr(coordinator_module, "cleanup", _fake_cleanup)
    monkeypatch.setattr(coordinator_module, "cleanup_results", _fake_cleanup_results)
    monkeypatch.setattr(
        coordinator_module,
        "initialize_subagent_attempts",
        _fake_initialize_attempts,
    )
    monkeypatch.setattr(coordinator_module, "get_state", _fake_get_state)
    monkeypatch.setattr(coordinator_module, "get_execution_epoch", _fake_get_epoch)

    coordinator = coordinator_module.ShadowCloneCoordinator(
        thread_id="thread-stopped-layer",
        project_id="project-stopped-layer",
        agent_run_id="run-stopped-layer",
        model_key="kimi-k2.5",
        db_client=object(),
        mode=ShadowCloneMode.AUTO,
    )

    async def _fake_wait_for_layer(layer, timeout=0, **kwargs):
        assert [item["id"] for item in layer] == ["task-1"]
        yield {
            "type": "status",
            "status": "stopped",
            "reason": "shadow_clone_cancelled",
            "shadow_clone_status": "cancelled",
            "execution_epoch": 1,
        }

    monkeypatch.setattr(coordinator, "_wait_for_layer_completion", _fake_wait_for_layer)

    events = await _collect(
        coordinator.run(user_message="complex", thread_run_id="tr-stopped"),
    )

    assert events[-1] == {
        "type": "status",
        "status": "stopped",
        "reason": "shadow_clone_cancelled",
        "shadow_clone_status": "cancelled",
        "execution_epoch": 1,
    }
    assert not any(event["type"] == "shadow_clone_aggregating" for event in events)
    assert cleanup_calls == ["run-stopped-layer"]


@pytest.mark.asyncio
async def test_coordinator_run_stops_without_aggregating_when_layer_wait_reports_terminal_failure(
    monkeypatch,
):
    fake_actor = _FakeSubAgentActor()
    cleanup_calls = []
    mutable_state = {
        "status": "running",
        "execution_epoch": 0,
        "subagents": {
            "task-1": {"status": "pending", "role": "researcher", "attempt_index": 1},
        },
    }

    monkeypatch.setattr(
        coordinator_module,
        "MainAgent",
        lambda **kwargs: _FakeMainAgent(
            **kwargs,
            decompose_result={
                "subtasks": [
                    {
                        "id": "task-1",
                        "role": "researcher",
                        "task_description": "research",
                    },
                ],
                "dependencies": [],
            },
        ),
    )
    monkeypatch.setattr(
        coordinator_module,
        "wait_for_confirmation",
        lambda *args, **kwargs: asyncio.sleep(0, result=ConfirmationResult.CONFIRMED),
    )
    monkeypatch.setattr(coordinator_module, "run_subagent", fake_actor)

    async def _fake_init_state(run_id, _mode, **kwargs):
        mutable_state["run_id"] = run_id
        mutable_state["subagents"] = {
            item["id"]: item for item in kwargs.get("subtasks") or []
        }

    async def _fake_transition(run_id, current, new):
        mutable_state["status"] = new
        return {"id": run_id, "status": new, "execution_epoch": mutable_state["execution_epoch"]}

    async def _fake_update_subagent(run_id, subtask_id, status, **kwargs):
        mutable_state["subagents"].setdefault(subtask_id, {})
        mutable_state["subagents"][subtask_id]["status"] = status
        mutable_state["subagents"][subtask_id].update(kwargs)
        return mutable_state["subagents"][subtask_id]

    async def _fake_cleanup(run_id):
        cleanup_calls.append(run_id)

    async def _fake_cleanup_results(_run_id):
        return None

    async def _fake_initialize_attempts(*args, **kwargs):
        return None

    async def _fake_get_state(_run_id):
        return mutable_state

    async def _fake_get_epoch(_run_id):
        return mutable_state["execution_epoch"]

    monkeypatch.setattr(coordinator_module, "init_state", _fake_init_state)
    monkeypatch.setattr(coordinator_module, "transition", _fake_transition)
    monkeypatch.setattr(coordinator_module, "update_subagent", _fake_update_subagent)
    monkeypatch.setattr(coordinator_module, "cleanup", _fake_cleanup)
    monkeypatch.setattr(coordinator_module, "cleanup_results", _fake_cleanup_results)
    monkeypatch.setattr(
        coordinator_module,
        "initialize_subagent_attempts",
        _fake_initialize_attempts,
    )
    monkeypatch.setattr(coordinator_module, "get_state", _fake_get_state)
    monkeypatch.setattr(coordinator_module, "get_execution_epoch", _fake_get_epoch)

    coordinator = coordinator_module.ShadowCloneCoordinator(
        thread_id="thread-failed-layer",
        project_id="project-failed-layer",
        agent_run_id="run-failed-layer",
        model_key="kimi-k2.5",
        db_client=object(),
        mode=ShadowCloneMode.AUTO,
    )

    async def _fake_wait_for_layer(layer, timeout=0, **kwargs):
        assert [item["id"] for item in layer] == ["task-1"]
        yield {
            "type": "status",
            "status": "failed",
            "reason": "shadow_clone_failed",
            "shadow_clone_status": "failed",
            "execution_epoch": 1,
        }

    monkeypatch.setattr(coordinator, "_wait_for_layer_completion", _fake_wait_for_layer)

    events = await _collect(
        coordinator.run(user_message="complex", thread_run_id="tr-failed"),
    )

    assert events[-1] == {
        "type": "status",
        "status": "failed",
        "reason": "shadow_clone_failed",
        "shadow_clone_status": "failed",
        "execution_epoch": 1,
    }
    assert not any(event["type"] == "shadow_clone_aggregating" for event in events)
    assert cleanup_calls == ["run-failed-layer"]


@pytest.mark.asyncio
async def test_refresh_environment_if_needed_reprepares_on_epoch_mismatch(monkeypatch):
    lease = {
        "sandbox_id": "sandbox-primary",
        "environment_ready": True,
        "environment_manifest": {
            "execution_epoch": 0,
            "sandbox": {"id": "sandbox-primary"},
        },
    }
    updates = []

    monkeypatch.setattr(coordinator_module, "MainAgent", _FakeMainAgent)

    async def _fake_get_epoch(_run_id):
        return 1

    async def _fake_get_lease(_run_id):
        return dict(lease)

    async def _fake_update_lease(_run_id, **kwargs):
        updates.append(("lease", kwargs))
        lease.update(kwargs)
        return dict(lease)

    async def _fake_update_environment(*args, **kwargs):
        updates.append(("state", kwargs))
        return None

    coordinator = coordinator_module.ShadowCloneCoordinator(
        thread_id="thread-refresh",
        project_id="project-refresh",
        agent_run_id="run-refresh",
        model_key="kimi-k2.5",
        db_client=object(),
        mode=ShadowCloneMode.AUTO,
    )

    async def _fake_prepare():
        return {
            "execution_epoch": 1,
            "sandbox": {"id": "sandbox-primary"},
        }

    monkeypatch.setattr(coordinator_module, "get_execution_epoch", _fake_get_epoch)
    monkeypatch.setattr(coordinator_module, "get_run_sandbox_lease", _fake_get_lease)
    monkeypatch.setattr(coordinator_module, "update_run_sandbox_lease", _fake_update_lease)
    monkeypatch.setattr(coordinator_module, "update_environment", _fake_update_environment)
    monkeypatch.setattr(coordinator, "_prepare_shadow_clone_environment", _fake_prepare)

    reason, manifest = await coordinator._refresh_environment_if_needed()

    assert reason == "epoch_mismatch"
    assert manifest["execution_epoch"] == 1
    assert any(kind == "lease" for kind, _payload in updates)
    assert any(kind == "state" for kind, _payload in updates)


@pytest.mark.asyncio
async def test_prepare_shadow_clone_environment_once_fails_when_lease_epoch_sync_missing(monkeypatch):
    update_environment_calls = []

    monkeypatch.setattr(coordinator_module, "MainAgent", _FakeMainAgent)

    async def _fake_get_epoch(_run_id):
        return 3

    async def _fake_update_environment(_run_id, **kwargs):
        update_environment_calls.append(kwargs)
        return None

    async def _fake_sync_epoch(_run_id, _execution_epoch):
        return None

    coordinator = coordinator_module.ShadowCloneCoordinator(
        thread_id="thread-prepare-sync",
        project_id="project-prepare-sync",
        agent_run_id="run-prepare-sync",
        model_key="kimi-k2.5",
        db_client=object(),
        mode=ShadowCloneMode.AUTO,
    )

    async def _fake_warmup():
        return {
            "run_id": coordinator.agent_run_id,
            "thread_id": coordinator.thread_id,
            "project_id": coordinator.project_id,
            "sandbox_id": "sandbox-primary",
            "sandbox_type": "code",
            "binding_state": "locked",
            "execution_epoch": 0,
            "lease_epoch": 0,
            "environment_status": "pending",
            "environment_ready": False,
            "environment_manifest": {},
        }

    monkeypatch.setattr(coordinator_module, "get_execution_epoch", _fake_get_epoch)
    monkeypatch.setattr(coordinator_module, "sync_run_sandbox_execution_epoch", _fake_sync_epoch)
    monkeypatch.setattr(coordinator_module, "update_environment", _fake_update_environment)
    monkeypatch.setattr(coordinator, "_warmup_project_sandbox", _fake_warmup)

    with pytest.raises(RuntimeError, match="failed to sync strict sandbox lease epoch"):
        await coordinator._prepare_shadow_clone_environment_once()

    assert any(call.get("status") == "preparing" for call in update_environment_calls)
    assert not any(call.get("status") == "ready" for call in update_environment_calls)


@pytest.mark.asyncio
async def test_prepare_shadow_clone_environment_once_requires_canonical_ready_lease_truth(monkeypatch):
    update_environment_calls = []
    lease_updates = []

    monkeypatch.setattr(coordinator_module, "MainAgent", _FakeMainAgent)

    async def _fake_get_epoch(_run_id):
        return 2

    async def _fake_update_environment(_run_id, **kwargs):
        update_environment_calls.append(kwargs)
        return None

    async def _fake_sync_epoch(_run_id, execution_epoch):
        return {
            "run_id": _run_id,
            "thread_id": "thread-ready-truth",
            "project_id": "project-ready-truth",
            "sandbox_id": "sandbox-primary",
            "sandbox_type": "code",
            "binding_state": "locked",
            "execution_epoch": execution_epoch,
            "lease_epoch": execution_epoch,
            "environment_status": "pending",
            "environment_ready": False,
            "environment_manifest": {},
        }

    async def _fake_update_lease(_run_id, **kwargs):
        lease_updates.append(kwargs)
        return {
            "run_id": _run_id,
            "thread_id": "thread-ready-truth",
            "project_id": "project-ready-truth",
            "sandbox_id": "sandbox-primary",
            "sandbox_type": "code",
            "binding_state": "locked",
            "execution_epoch": 2,
            "lease_epoch": 2,
            "environment_status": kwargs.get("environment_status", "pending"),
            "environment_ready": bool(kwargs.get("environment_ready", False)),
            "environment_prepared_at": kwargs.get("environment_prepared_at"),
            "environment_manifest": kwargs.get(
                "environment_manifest",
                {
                    "execution_epoch": 2,
                    "sandbox": {"id": "sandbox-primary", "type": "code"},
                },
            ),
        }

    async def _fake_get_lease(_run_id):
        return {
            "run_id": _run_id,
            "thread_id": "thread-ready-truth",
            "project_id": "project-ready-truth",
            "sandbox_id": "sandbox-primary",
            "sandbox_type": "code",
            "binding_state": "locked",
            "execution_epoch": 2,
            "lease_epoch": 2,
            "environment_status": "ready",
            "environment_ready": False,
            "environment_manifest": {
                "execution_epoch": 2,
                "sandbox": {"id": "sandbox-primary", "type": "code"},
            },
        }

    coordinator = coordinator_module.ShadowCloneCoordinator(
        thread_id="thread-ready-truth",
        project_id="project-ready-truth",
        agent_run_id="run-ready-truth",
        model_key="kimi-k2.5",
        db_client=object(),
        mode=ShadowCloneMode.AUTO,
    )

    async def _fake_warmup():
        return {
            "run_id": coordinator.agent_run_id,
            "thread_id": coordinator.thread_id,
            "project_id": coordinator.project_id,
            "sandbox_id": "sandbox-primary",
            "sandbox_type": "code",
            "binding_state": "locked",
            "execution_epoch": 0,
            "lease_epoch": 0,
            "environment_status": "pending",
            "environment_ready": False,
            "environment_manifest": {},
        }

    monkeypatch.setattr(coordinator_module, "get_execution_epoch", _fake_get_epoch)
    monkeypatch.setattr(coordinator_module, "sync_run_sandbox_execution_epoch", _fake_sync_epoch)
    monkeypatch.setattr(coordinator_module, "update_run_sandbox_lease", _fake_update_lease)
    monkeypatch.setattr(coordinator_module, "get_run_sandbox_lease", _fake_get_lease)
    monkeypatch.setattr(coordinator_module, "update_environment", _fake_update_environment)
    monkeypatch.setattr(coordinator, "_warmup_project_sandbox", _fake_warmup)

    with pytest.raises(RuntimeError, match="could not confirm canonical strict lease truth"):
        await coordinator._prepare_shadow_clone_environment_once()

    assert any(update.get("environment_status") == "ready" for update in lease_updates)
    assert not any(call.get("status") == "ready" for call in update_environment_calls)


@pytest.mark.asyncio
async def test_coordinator_defaults_to_local_subagent_execution_when_env_absent(monkeypatch):
    calls = {"local": []}
    fake_actor = _FakeSubAgentActor()

    monkeypatch.delenv("SHADOW_CLONE_SUBAGENT_EXECUTION_MODE", raising=False)
    monkeypatch.setattr(coordinator_module, "MainAgent", _FakeMainAgent)
    monkeypatch.setattr(
        coordinator_module,
        "wait_for_confirmation",
        lambda *args, **kwargs: asyncio.sleep(0, result=ConfirmationResult.CONFIRMED),
    )
    monkeypatch.setattr(
        coordinator_module,
        "execute_subagent",
        AsyncMock(side_effect=lambda **kwargs: calls["local"].append(kwargs)),
    )
    monkeypatch.setattr(coordinator_module, "run_subagent", fake_actor)

    async def _fake_init_state(*args, **kwargs):
        return None

    async def _fake_transition(*args, **kwargs):
        return True

    async def _fake_update_subagent(*args, **kwargs):
        return None

    async def _fake_cleanup(*args, **kwargs):
        return None

    async def _fake_cleanup_results(*args, **kwargs):
        return 0

    monkeypatch.setattr(coordinator_module, "init_state", _fake_init_state)
    monkeypatch.setattr(coordinator_module, "transition", _fake_transition)
    monkeypatch.setattr(coordinator_module, "update_subagent", _fake_update_subagent)
    monkeypatch.setattr(coordinator_module, "cleanup", _fake_cleanup)
    monkeypatch.setattr(coordinator_module, "cleanup_results", _fake_cleanup_results)

    coordinator = coordinator_module.ShadowCloneCoordinator(
        thread_id="thread-1",
        project_id="project-1",
        agent_run_id="run-local",
        model_key="kimi-k2.5",
        db_client=object(),
        mode=ShadowCloneMode.AUTO,
    )

    async def _fake_wait_for_layer(layer, timeout=0):
        for item in layer:
            yield {"type": "subagent_completed", "subtask_id": item["id"]}

    monkeypatch.setattr(coordinator, "_wait_for_layer_completion", _fake_wait_for_layer)

    events = await _collect(
        coordinator.run(user_message="complex", thread_run_id="tr-local"),
    )

    assert any(event["type"] == "subagent_started" for event in events)
    assert len(calls["local"]) == 1
    assert calls["local"][0]["parent_run_id"] == "run-local"
    assert calls["local"][0]["subtask_id"] == "task-1"
    assert fake_actor.sent == []


@pytest.mark.asyncio
async def test_dispatch_subagent_attempt_appends_local_task_when_defaulting_local(monkeypatch):
    local_calls = []
    local_task_started = asyncio.Event()
    release_local_task = asyncio.Event()
    fake_actor = _FakeSubAgentActor()

    monkeypatch.delenv("SHADOW_CLONE_SUBAGENT_EXECUTION_MODE", raising=False)
    monkeypatch.setattr(coordinator_module, "MainAgent", _FakeMainAgent)

    async def _fake_execute_subagent(**kwargs):
        local_calls.append(kwargs)
        local_task_started.set()
        await release_local_task.wait()
        return None

    monkeypatch.setattr(
        coordinator_module,
        "execute_subagent",
        AsyncMock(side_effect=_fake_execute_subagent),
    )
    monkeypatch.setattr(coordinator_module, "run_subagent", fake_actor)

    coordinator = coordinator_module.ShadowCloneCoordinator(
        thread_id="thread-local-dispatch",
        project_id="project-local-dispatch",
        agent_run_id="run-local-dispatch",
        model_key="kimi-k2.5",
        db_client=object(),
        mode=ShadowCloneMode.AUTO,
    )

    layer_tasks = []
    event = await coordinator._dispatch_subagent_attempt(
        subtask={"id": "task-1", "role": "researcher"},
        layer_index=1,
        execution_epoch=2,
        layer_tasks=layer_tasks,
        live_activity={"scope": "shadow_clone_main", "phase": "execution"},
    )

    assert event["type"] == "subagent_started"
    assert len(layer_tasks) == 1
    assert isinstance(layer_tasks[0], asyncio.Task)
    assert fake_actor.sent == []
    await local_task_started.wait()
    assert layer_tasks[0] in coordinator._active_local_subagent_tasks

    release_local_task.set()
    await coordinator._finalize_layer_tasks(layer_tasks, recovery_triggered=False)

    assert len(local_calls) == 1
    assert local_calls[0]["parent_run_id"] == "run-local-dispatch"
    assert local_calls[0]["subtask_id"] == "task-1"
    assert local_calls[0]["subtask_config"]["layer_index"] == 1
    assert local_calls[0]["subtask_config"]["execution_epoch"] == 2
    assert coordinator._active_local_subagent_tasks == set()


@pytest.mark.asyncio
async def test_close_cancels_active_local_subagent_tasks(monkeypatch):
    local_task_started = asyncio.Event()
    local_task_cancelled = asyncio.Event()

    monkeypatch.delenv("SHADOW_CLONE_SUBAGENT_EXECUTION_MODE", raising=False)
    monkeypatch.setattr(coordinator_module, "MainAgent", _FakeMainAgent)

    async def _fake_execute_subagent(**_kwargs):
        local_task_started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            local_task_cancelled.set()
            raise

    monkeypatch.setattr(
        coordinator_module,
        "execute_subagent",
        AsyncMock(side_effect=_fake_execute_subagent),
    )

    coordinator = coordinator_module.ShadowCloneCoordinator(
        thread_id="thread-local-close",
        project_id="project-local-close",
        agent_run_id="run-local-close",
        model_key="kimi-k2.5",
        db_client=object(),
        mode=ShadowCloneMode.AUTO,
    )
    coordinator.main_agent.close = AsyncMock(return_value=None)

    layer_tasks = []
    await coordinator._dispatch_subagent_attempt(
        subtask={"id": "task-1", "role": "researcher"},
        layer_index=1,
        execution_epoch=2,
        layer_tasks=layer_tasks,
        live_activity={"scope": "shadow_clone_main", "phase": "execution"},
    )

    await local_task_started.wait()
    assert len(coordinator._active_local_subagent_tasks) == 1

    await coordinator.close()

    assert local_task_cancelled.is_set()
    assert coordinator._active_local_subagent_tasks == set()
    assert layer_tasks[0].cancelled()
    coordinator.main_agent.close.assert_awaited_once()


@pytest.mark.asyncio
async def test_finalize_layer_tasks_drains_multiple_local_subagent_tasks(monkeypatch):
    local_calls = []
    started_events = {
        "task-1": asyncio.Event(),
        "task-2": asyncio.Event(),
    }
    release_events = {
        "task-1": asyncio.Event(),
        "task-2": asyncio.Event(),
    }

    monkeypatch.delenv("SHADOW_CLONE_SUBAGENT_EXECUTION_MODE", raising=False)
    monkeypatch.setattr(coordinator_module, "MainAgent", _FakeMainAgent)

    async def _fake_execute_subagent(**kwargs):
        subtask_id = kwargs["subtask_id"]
        local_calls.append(kwargs)
        started_events[subtask_id].set()
        await release_events[subtask_id].wait()
        return None

    monkeypatch.setattr(
        coordinator_module,
        "execute_subagent",
        AsyncMock(side_effect=_fake_execute_subagent),
    )

    coordinator = coordinator_module.ShadowCloneCoordinator(
        thread_id="thread-local-multi",
        project_id="project-local-multi",
        agent_run_id="run-local-multi",
        model_key="kimi-k2.5",
        db_client=object(),
        mode=ShadowCloneMode.AUTO,
    )

    layer_tasks = []
    await coordinator._dispatch_subagent_attempt(
        subtask={"id": "task-1", "role": "researcher"},
        layer_index=1,
        execution_epoch=3,
        layer_tasks=layer_tasks,
        live_activity={"scope": "shadow_clone_main", "phase": "execution"},
    )
    await coordinator._dispatch_subagent_attempt(
        subtask={"id": "task-2", "role": "writer"},
        layer_index=1,
        execution_epoch=3,
        layer_tasks=layer_tasks,
        live_activity={"scope": "shadow_clone_main", "phase": "execution"},
    )

    await asyncio.gather(*(event.wait() for event in started_events.values()))
    assert len(layer_tasks) == 2
    assert all(isinstance(task, asyncio.Task) for task in layer_tasks)
    assert len(coordinator._active_local_subagent_tasks) == 2

    release_events["task-1"].set()
    release_events["task-2"].set()
    await coordinator._finalize_layer_tasks(layer_tasks, recovery_triggered=False)

    assert sorted(call["subtask_id"] for call in local_calls) == ["task-1", "task-2"]
    assert coordinator._active_local_subagent_tasks == set()


@pytest.mark.asyncio
async def test_run_exception_drains_active_local_subagent_tasks(monkeypatch):
    fake_actor = _FakeSubAgentActor()
    local_task_started = asyncio.Event()
    local_task_cancelled = asyncio.Event()
    cleanup_calls = []

    monkeypatch.delenv("SHADOW_CLONE_SUBAGENT_EXECUTION_MODE", raising=False)
    monkeypatch.setattr(coordinator_module, "MainAgent", _FakeMainAgent)
    monkeypatch.setattr(
        coordinator_module,
        "wait_for_confirmation",
        lambda *args, **kwargs: asyncio.sleep(0, result=ConfirmationResult.CONFIRMED),
    )

    async def _fake_execute_subagent(**_kwargs):
        local_task_started.set()
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            local_task_cancelled.set()
            raise

    async def _fake_init_state(*args, **kwargs):
        return None

    async def _fake_transition(*args, **kwargs):
        return True

    async def _fake_update_subagent(*args, **kwargs):
        return None

    async def _fake_initialize_subagent_attempts(*args, **kwargs):
        return None

    async def _fake_cleanup(run_id):
        cleanup_calls.append(run_id)

    async def _fake_cleanup_results(*args, **kwargs):
        return 0

    monkeypatch.setattr(
        coordinator_module,
        "execute_subagent",
        AsyncMock(side_effect=_fake_execute_subagent),
    )
    monkeypatch.setattr(coordinator_module, "run_subagent", fake_actor)
    monkeypatch.setattr(coordinator_module, "init_state", _fake_init_state)
    monkeypatch.setattr(coordinator_module, "transition", _fake_transition)
    monkeypatch.setattr(coordinator_module, "update_subagent", _fake_update_subagent)
    monkeypatch.setattr(
        coordinator_module,
        "initialize_subagent_attempts",
        _fake_initialize_subagent_attempts,
    )
    monkeypatch.setattr(coordinator_module, "cleanup", _fake_cleanup)
    monkeypatch.setattr(coordinator_module, "cleanup_results", _fake_cleanup_results)

    coordinator = coordinator_module.ShadowCloneCoordinator(
        thread_id="thread-local-run-exception",
        project_id="project-local-run-exception",
        agent_run_id="run-local-run-exception",
        model_key="kimi-k2.5",
        db_client=object(),
        mode=ShadowCloneMode.AUTO,
    )

    async def _fake_wait_for_layer_completion(*args, **kwargs):
        await local_task_started.wait()
        raise RuntimeError("layer exploded")
        yield

    monkeypatch.setattr(
        coordinator,
        "_wait_for_layer_completion",
        _fake_wait_for_layer_completion,
    )

    with pytest.raises(RuntimeError, match="layer exploded"):
        await _collect(
            coordinator.run(user_message="complex", thread_run_id="tr-local-run-exception"),
        )

    assert local_task_cancelled.is_set()
    assert coordinator._active_local_subagent_tasks == set()
    assert cleanup_calls == ["run-local-run-exception"]
    assert fake_actor.sent == []


@pytest.mark.asyncio
async def test_dispatch_subagent_attempt_uses_dramatiq_when_explicitly_configured(monkeypatch):
    fake_actor = _FakeSubAgentActor()
    local_calls = []

    monkeypatch.setenv("SHADOW_CLONE_SUBAGENT_EXECUTION_MODE", "dramatiq")
    monkeypatch.setattr(coordinator_module, "MainAgent", _FakeMainAgent)
    monkeypatch.setattr(
        coordinator_module,
        "execute_subagent",
        AsyncMock(side_effect=lambda **kwargs: local_calls.append(kwargs)),
    )
    monkeypatch.setattr(coordinator_module, "run_subagent", fake_actor)

    coordinator = coordinator_module.ShadowCloneCoordinator(
        thread_id="thread-actor",
        project_id="project-actor",
        agent_run_id="run-actor",
        model_key="kimi-k2.5",
        db_client=object(),
        mode=ShadowCloneMode.AUTO,
    )

    layer_tasks = []
    event = await coordinator._dispatch_subagent_attempt(
        subtask={"id": "task-1", "role": "researcher"},
        layer_index=2,
        execution_epoch=4,
        layer_tasks=layer_tasks,
        live_activity={"scope": "shadow_clone_main", "phase": "execution"},
    )

    assert event["type"] == "subagent_started"
    assert layer_tasks == []
    assert local_calls == []
    assert len(fake_actor.sent) == 1
    assert fake_actor.sent[0]["parent_run_id"] == "run-actor"
    assert fake_actor.sent[0]["subtask_id"] == "task-1"
    assert fake_actor.sent[0]["subtask_config"]["layer_index"] == 2
    assert fake_actor.sent[0]["subtask_config"]["execution_epoch"] == 4
    assert fake_actor.sent[0]["subtask_config"]["attempt_index"] == 1


@pytest.mark.asyncio
async def test_coordinator_no_split_streams_direct_answer_and_completes(monkeypatch):
    async def _fake_init_state(*args, **kwargs):
        return None

    async def _fake_transition(*args, **kwargs):
        return True

    async def _fake_cleanup(*args, **kwargs):
        return None

    async def _fake_cleanup_results(*args, **kwargs):
        return 0

    monkeypatch.setattr(
        coordinator_module,
        "MainAgent",
        lambda **kwargs: _FakeMainAgent(
            **kwargs,
            decompose_result=None,
            direct_answer="simple direct answer",
        ),
    )
    monkeypatch.setattr(coordinator_module, "init_state", _fake_init_state)
    monkeypatch.setattr(coordinator_module, "transition", _fake_transition)
    monkeypatch.setattr(coordinator_module, "cleanup", _fake_cleanup)
    monkeypatch.setattr(coordinator_module, "cleanup_results", _fake_cleanup_results)

    coordinator = coordinator_module.ShadowCloneCoordinator(
        thread_id="thread-1",
        project_id="project-1",
        agent_run_id="run-direct",
        model_key="kimi-k2.5",
        db_client=object(),
        mode=ShadowCloneMode.AUTO,
    )

    events = await _collect(
        coordinator.run(user_message="simple", thread_run_id="tr-direct"),
    )

    passthrough_events = [event for event in events if event["type"] == "passthrough_message"]
    assert len(passthrough_events) == 1
    assert passthrough_events[0]["is_last"] is True
    assert passthrough_events[0]["route_metadata"]["shadow_clone_phase"] == "planning"
    assert passthrough_events[0]["msg"].content == "simple direct answer"
    assert not any(event["type"] == "shadow_clone_proposed" for event in events)
    assert not any(event["type"] == "subagent_started" for event in events)
    assert events[-1]["type"] == "shadow_clone_complete"


@pytest.mark.asyncio
async def test_cleanup_speculative_startup_releases_completed_warmup_lease(monkeypatch):
    release_calls = []
    lease = {
        "binding_state": "locked",
        "sandbox_id": "sandbox-primary",
    }

    monkeypatch.setattr(coordinator_module, "MainAgent", _FakeMainAgent)

    async def _fake_get_lease(_run_id):
        return dict(lease)

    async def _fake_set_binding_state(_run_id, binding_state, *, last_error=None):
        release_calls.append((binding_state, last_error))
        lease["binding_state"] = binding_state
        lease["last_error"] = last_error
        return dict(lease)

    coordinator = coordinator_module.ShadowCloneCoordinator(
        thread_id="thread-cleanup",
        project_id="project-cleanup",
        agent_run_id="run-cleanup",
        model_key="kimi-k2.5",
        db_client=object(),
        mode=ShadowCloneMode.AUTO,
    )

    async def _fake_warmup():
        return dict(lease)

    monkeypatch.setattr(coordinator_module, "get_run_sandbox_lease", _fake_get_lease)
    monkeypatch.setattr(
        coordinator_module,
        "set_run_sandbox_binding_state",
        _fake_set_binding_state,
    )
    monkeypatch.setattr(coordinator, "_warmup_project_sandbox", _fake_warmup)

    coordinator._ensure_warmup_started()
    await coordinator._cleanup_speculative_startup(release_lease=True)

    assert release_calls == [("released", None)]
    assert coordinator._warmup_task is None


@pytest.mark.asyncio
async def test_cleanup_speculative_startup_clears_standby_clone_metadata(monkeypatch):
    lease = {
        "binding_state": "locked",
        "sandbox_id": "sandbox-primary",
        "standby_sandbox_id": "sandbox-standby",
        "standby_sandbox_type": "desktop",
        "standby_sandbox_info": {"id": "sandbox-standby"},
        "standby_snapshot_template_id": "snapshot-1",
        "standby_source_sandbox_id": "sandbox-primary",
        "standby_created_at": "2026-03-20T00:00:00+00:00",
        "last_clone_error": "clone warning",
    }
    update_calls = []

    monkeypatch.setattr(coordinator_module, "MainAgent", _FakeMainAgent)

    async def _fake_get_lease(_run_id):
        return dict(lease)

    async def _fake_update_lease(_run_id, **kwargs):
        update_calls.append(kwargs)
        lease.update(kwargs)
        return dict(lease)

    coordinator = coordinator_module.ShadowCloneCoordinator(
        thread_id="thread-standby-cleanup",
        project_id="project-standby-cleanup",
        agent_run_id="run-standby-cleanup",
        model_key="kimi-k2.5",
        db_client=object(),
        mode=ShadowCloneMode.AUTO,
    )

    monkeypatch.setattr(coordinator_module, "get_run_sandbox_lease", _fake_get_lease)
    monkeypatch.setattr(coordinator_module, "update_run_sandbox_lease", _fake_update_lease)

    await coordinator._clear_bootstrap_standby_clone_metadata()

    assert update_calls == [
        {
            "standby_sandbox_id": None,
            "standby_sandbox_type": None,
            "standby_sandbox_info": {},
            "standby_snapshot_template_id": None,
            "standby_source_sandbox_id": None,
            "standby_created_at": None,
            "last_clone_error": None,
        }
    ]


@pytest.mark.asyncio
async def test_coordinator_fails_before_running_when_strict_sandbox_bind_fails(monkeypatch):
    calls = {"transition": []}

    monkeypatch.setattr(coordinator_module, "MainAgent", _FakeMainAgent)
    monkeypatch.setattr(
        coordinator_module,
        "wait_for_confirmation",
        lambda *args, **kwargs: asyncio.sleep(0, result=ConfirmationResult.CONFIRMED),
    )

    async def _fake_init_state(*args, **kwargs):
        return None

    async def _fake_transition(*args, **kwargs):
        calls["transition"].append((args, kwargs))
        return True

    async def _fake_update_subagent(*args, **kwargs):
        return None

    async def _fake_cleanup(*args, **kwargs):
        return None

    async def _fake_cleanup_results(*args, **kwargs):
        return 0

    async def _fake_bind(self):
        raise RuntimeError("strict sandbox bind failed")

    monkeypatch.setattr(coordinator_module, "init_state", _fake_init_state)
    monkeypatch.setattr(coordinator_module, "transition", _fake_transition)
    monkeypatch.setattr(coordinator_module, "update_subagent", _fake_update_subagent)
    monkeypatch.setattr(coordinator_module, "cleanup", _fake_cleanup)
    monkeypatch.setattr(coordinator_module, "cleanup_results", _fake_cleanup_results)
    monkeypatch.setattr(
        coordinator_module.ShadowCloneCoordinator,
        "_warmup_project_sandbox",
        _fake_bind,
    )

    coordinator = coordinator_module.ShadowCloneCoordinator(
        thread_id="thread-1",
        project_id="project-1",
        agent_run_id="run-fail",
        model_key="kimi-k2.5",
        db_client=object(),
        mode=ShadowCloneMode.AUTO,
    )

    events = await _collect(
        coordinator.run(user_message="complex", thread_run_id="tr-fail"),
    )

    assert any(event["type"] == "status" and event["status"] == "failed" for event in events)
    transitions = [args[1:3] for args, _kwargs in calls["transition"]]
    assert ("confirming", "failed") in transitions


@pytest.mark.asyncio
async def test_coordinator_passes_layer_index_to_subagents(monkeypatch):
    """Verify that coordinator passes layer_index to each subtask_config."""
    fake_actor = _FakeSubAgentActor()

    monkeypatch.setattr(coordinator_module, "MainAgent", _FakeMainAgent)
    async def _confirmed(*args, **kwargs):
        return ConfirmationResult.CONFIRMED

    monkeypatch.setattr(coordinator_module, "wait_for_confirmation", _confirmed)
    monkeypatch.setattr(coordinator_module, "run_subagent", fake_actor)

    # Mock topological_sort_layers to return 2 layers
    def _fake_topological_sort(subtasks, deps):
        return [
            [{"id": "task-1", "role": "researcher"}],
            [{"id": "task-2", "role": "analyst"}],
        ]

    monkeypatch.setattr(
        coordinator_module,
        "topological_sort_layers",
        _fake_topological_sort,
    )

    async def _fake_init_state(*args, **kwargs):
        return None

    async def _fake_transition(*args, **kwargs):
        return True

    async def _fake_update_subagent(*args, **kwargs):
        return None

    async def _fake_cleanup(*args, **kwargs):
        return None

    async def _fake_cleanup_results(*args, **kwargs):
        return 0

    monkeypatch.setattr(coordinator_module, "init_state", _fake_init_state)
    monkeypatch.setattr(coordinator_module, "transition", _fake_transition)
    monkeypatch.setattr(coordinator_module, "update_subagent", _fake_update_subagent)
    monkeypatch.setattr(coordinator_module, "cleanup", _fake_cleanup)
    monkeypatch.setattr(coordinator_module, "cleanup_results", _fake_cleanup_results)

    coordinator = coordinator_module.ShadowCloneCoordinator(
        thread_id="thread-1",
        project_id="project-1",
        agent_run_id="run-1",
        model_key="kimi-k2.5",
        db_client=object(),
        mode=ShadowCloneMode.AUTO,
    )

    async def _fake_wait_for_layer(layer, timeout=0):
        for item in layer:
            yield {"type": "subagent_completed", "subtask_id": item["id"]}

    monkeypatch.setattr(coordinator, "_wait_for_layer_completion", _fake_wait_for_layer)

    events = await _collect(
        coordinator.run(user_message="complex", thread_run_id="tr-1"),
    )

    # Verify that layer_index was passed to each subtask
    assert len(fake_actor.sent) == 2
    assert fake_actor.sent[0]["subtask_config"]["layer_index"] == 0
    assert fake_actor.sent[0]["subtask_config"]["id"] == "task-1"
    assert fake_actor.sent[1]["subtask_config"]["layer_index"] == 1
    assert fake_actor.sent[1]["subtask_config"]["id"] == "task-2"


@pytest.mark.asyncio
async def test_coordinator_denied_continues_with_main_agent(monkeypatch):
    fake_actor = _FakeSubAgentActor()
    recorded_live_activity = []

    monkeypatch.setattr(coordinator_module, "MainAgent", _FakeMainAgent)
    async def _denied(*args, **kwargs):
        return ConfirmationResult.DENIED

    monkeypatch.setattr(coordinator_module, "wait_for_confirmation", _denied)
    monkeypatch.setattr(coordinator_module, "run_subagent", fake_actor)

    async def _fake_init_state(*args, **kwargs):
        return None

    async def _fake_transition(*args, **kwargs):
        return True

    async def _fake_update_subagent(*args, **kwargs):
        return None

    async def _fake_cleanup(*args, **kwargs):
        return None

    async def _fake_cleanup_results(*args, **kwargs):
        return 0

    async def _fake_update_live_activity(
        _run_id,
        *,
        scope,
        phase,
        reason,
        subtask_id=None,
        epoch=None,
    ):
        payload = {
            "scope": scope,
            "phase": phase,
            "reason": reason,
            "subtask_id": subtask_id,
            "epoch": 0 if epoch is None else epoch,
        }
        recorded_live_activity.append(payload)
        return {"live_activity": payload}

    monkeypatch.setattr(coordinator_module, "init_state", _fake_init_state)
    monkeypatch.setattr(coordinator_module, "transition", _fake_transition)
    monkeypatch.setattr(coordinator_module, "update_subagent", _fake_update_subagent)
    monkeypatch.setattr(coordinator_module, "cleanup", _fake_cleanup)
    monkeypatch.setattr(coordinator_module, "cleanup_results", _fake_cleanup_results)
    monkeypatch.setattr(coordinator_module, "update_live_activity", _fake_update_live_activity)

    coordinator = coordinator_module.ShadowCloneCoordinator(
        thread_id="thread-1",
        project_id="project-1",
        agent_run_id="run-2",
        model_key="kimi-k2.5",
        db_client=object(),
        mode=ShadowCloneMode.AUTO,
    )
    events = await _collect(
        coordinator.run(user_message="complex", thread_run_id="tr-2"),
    )
    passthrough_events = [event for event in events if event["type"] == "passthrough_message"]
    assert passthrough_events[-1]["msg"].content == "direct after denial"
    assert passthrough_events[-1]["route_metadata"]["shadow_clone_phase"] == "execution"
    assert passthrough_events[-1]["route_metadata"]["activity_owner"] == "main_agent"
    assert passthrough_events[-1]["route_metadata"]["ui_phase"] == "main_agent_continuation"
    assert passthrough_events[-1]["route_metadata"]["phase_reason"] == "denied_continue"
    assert events[-1]["type"] == "shadow_clone_complete"
    assert events[-1]["activity_owner"] == "none"
    assert events[-1]["ui_phase"] == "completed"
    assert events[-1]["phase_reason"] == "denied_complete"
    assert recorded_live_activity[-1] == {
        "scope": "main_agent",
        "phase": "completed",
        "reason": "denied_complete",
        "subtask_id": None,
        "epoch": 0,
    }
    assert not fake_actor.sent


@pytest.mark.asyncio
async def test_coordinator_denied_releases_lease_after_direct_continuation(monkeypatch):
    call_order = []

    monkeypatch.setattr(coordinator_module, "MainAgent", _FakeMainAgent)

    async def _denied(*args, **kwargs):
        return ConfirmationResult.DENIED

    async def _fake_init_state(*args, **kwargs):
        return None

    async def _fake_transition(*args, **kwargs):
        return True

    async def _fake_update_subagent(*args, **kwargs):
        return None

    async def _fake_cleanup(*args, **kwargs):
        return None

    async def _fake_cleanup_results(*args, **kwargs):
        return 0

    monkeypatch.setattr(coordinator_module, "wait_for_confirmation", _denied)
    monkeypatch.setattr(coordinator_module, "init_state", _fake_init_state)
    monkeypatch.setattr(coordinator_module, "transition", _fake_transition)
    monkeypatch.setattr(coordinator_module, "update_subagent", _fake_update_subagent)
    monkeypatch.setattr(coordinator_module, "cleanup", _fake_cleanup)
    monkeypatch.setattr(coordinator_module, "cleanup_results", _fake_cleanup_results)

    coordinator = coordinator_module.ShadowCloneCoordinator(
        thread_id="thread-denied-order",
        project_id="project-denied-order",
        agent_run_id="run-denied-order",
        model_key="kimi-k2.5",
        db_client=object(),
        mode=ShadowCloneMode.AUTO,
    )

    original_stream_after_denial = coordinator.main_agent.stream_after_denial

    async def _wrapped_stream_after_denial():
        call_order.append("direct_after_denial_start")
        async for item in original_stream_after_denial():
            yield item
        call_order.append("direct_after_denial_end")

    async def _fake_cleanup_speculative_startup(
        *,
        release_lease=False,
        last_error=None,
        clear_standby=False,
    ):
        call_order.append(
            (
                "cleanup",
                release_lease,
                last_error,
                clear_standby,
            )
        )

    coordinator.main_agent.stream_after_denial = _wrapped_stream_after_denial
    monkeypatch.setattr(
        coordinator,
        "_cleanup_speculative_startup",
        _fake_cleanup_speculative_startup,
    )

    deny_events = await _collect(
        coordinator.run(user_message="complex", thread_run_id="tr-denied-order"),
    )

    assert call_order == [
        ("cleanup", False, None, True),
        "direct_after_denial_start",
        "direct_after_denial_end",
        ("cleanup", True, "denied", False),
    ]
    continuation_events = [
        event
        for event in deny_events
        if event["type"] == "passthrough_message"
        and event.get("route_metadata", {}).get("shadow_clone_phase") == "execution"
    ]
    assert continuation_events
    assert continuation_events[0]["route_metadata"]["live_activity"]["scope"] == "main_agent"
    assert continuation_events[0]["route_metadata"]["live_activity"]["phase"] == "execution"
    assert continuation_events[0]["route_metadata"]["activity_owner"] == "main_agent"
    assert continuation_events[0]["route_metadata"]["ui_phase"] == "main_agent_continuation"
    assert continuation_events[0]["route_metadata"]["phase_reason"] == "denied_continue"


@pytest.mark.asyncio
async def test_coordinator_prewarms_sandbox_before_dispatch(monkeypatch):
    fake_actor = _FakeSubAgentActor()
    order = []

    monkeypatch.setattr(coordinator_module, "MainAgent", _FakeMainAgent)
    monkeypatch.setattr(coordinator_module, "run_subagent", fake_actor)
    monkeypatch.setattr(
        coordinator_module,
        "topological_sort_layers",
        lambda subtasks, deps: [subtasks],
    )

    async def _confirmed(*args, **kwargs):
        return ConfirmationResult.CONFIRMED

    async def _fake_init_state(*args, **kwargs):
        return None

    async def _fake_transition(*args, **kwargs):
        return True

    async def _fake_update_subagent(*args, **kwargs):
        return None

    async def _fake_cleanup(*args, **kwargs):
        return None

    async def _fake_cleanup_results(*args, **kwargs):
        return 0

    monkeypatch.setattr(coordinator_module, "wait_for_confirmation", _confirmed)
    monkeypatch.setattr(coordinator_module, "init_state", _fake_init_state)
    monkeypatch.setattr(coordinator_module, "transition", _fake_transition)
    monkeypatch.setattr(coordinator_module, "update_subagent", _fake_update_subagent)
    monkeypatch.setattr(coordinator_module, "cleanup", _fake_cleanup)
    monkeypatch.setattr(coordinator_module, "cleanup_results", _fake_cleanup_results)

    coordinator = coordinator_module.ShadowCloneCoordinator(
        thread_id="thread-1",
        project_id="project-1",
        agent_run_id="run-3",
        model_key="kimi-k2.5",
        db_client=object(),
        mode=ShadowCloneMode.AUTO,
    )

    async def _fake_warmup():
        order.append("warmup")
        lease = {
            "run_id": coordinator.agent_run_id,
            "thread_id": coordinator.thread_id,
            "project_id": coordinator.project_id,
            "sandbox_id": "sandbox-primary",
            "sandbox_type": "desktop",
            "binding_state": "locked",
            "execution_epoch": 0,
            "lease_epoch": 0,
            "environment_status": "pending",
            "environment_ready": False,
            "environment_manifest": {},
        }
        _TEST_LEASES[coordinator.agent_run_id] = dict(lease)
        return dict(lease)

    async def _fake_bootstrap():
        order.append("bootstrap")

    def _send(**kwargs):
        order.append("send")
        fake_actor.sent.append(kwargs)

    fake_actor.send = _send

    async def _fake_wait_for_layer(layer, timeout=0):
        for item in layer:
            yield {"type": "subagent_completed", "subtask_id": item["id"]}

    monkeypatch.setattr(coordinator, "_warmup_project_sandbox", _fake_warmup)
    monkeypatch.setattr(coordinator, "_ensure_bootstrap_standby_clone", _fake_bootstrap)
    monkeypatch.setattr(coordinator, "_wait_for_layer_completion", _fake_wait_for_layer)

    await _collect(coordinator.run(user_message="complex", thread_run_id="tr-3"))

    assert order.index("warmup") < order.index("send")
    assert "bootstrap" not in order


@pytest.mark.asyncio
async def test_coordinator_starts_bootstrap_clone_before_confirmation_wait_when_enabled(monkeypatch):
    order = []
    monkeypatch.setenv("SHADOW_CLONE_ENABLE_PROACTIVE_STANDBY", "true")

    monkeypatch.setattr(coordinator_module, "MainAgent", _FakeMainAgent)

    async def _confirmed(*args, **kwargs):
        order.append("confirm_wait")
        assert "bootstrap" in order
        return ConfirmationResult.CONFIRMED

    async def _fake_init_state(*args, **kwargs):
        return None

    async def _fake_transition(*args, **kwargs):
        return True

    async def _fake_update_subagent(*args, **kwargs):
        return None

    async def _fake_cleanup(*args, **kwargs):
        return None

    async def _fake_cleanup_results(*args, **kwargs):
        return 0

    monkeypatch.setattr(coordinator_module, "wait_for_confirmation", _confirmed)
    monkeypatch.setattr(coordinator_module, "init_state", _fake_init_state)
    monkeypatch.setattr(coordinator_module, "transition", _fake_transition)
    monkeypatch.setattr(coordinator_module, "update_subagent", _fake_update_subagent)
    monkeypatch.setattr(coordinator_module, "cleanup", _fake_cleanup)
    monkeypatch.setattr(coordinator_module, "cleanup_results", _fake_cleanup_results)

    coordinator = coordinator_module.ShadowCloneCoordinator(
        thread_id="thread-bootstrap-order",
        project_id="project-bootstrap-order",
        agent_run_id="run-bootstrap-order",
        model_key="kimi-k2.5",
        db_client=object(),
        mode=ShadowCloneMode.AUTO,
    )

    async def _fake_warmup():
        order.append("warmup")
        lease = {
            "run_id": coordinator.agent_run_id,
            "thread_id": coordinator.thread_id,
            "project_id": coordinator.project_id,
            "sandbox_id": "sandbox-primary",
            "sandbox_type": "desktop",
            "binding_state": "locked",
            "execution_epoch": 0,
            "lease_epoch": 0,
            "environment_status": "pending",
            "environment_ready": False,
            "environment_manifest": {},
        }
        _TEST_LEASES[coordinator.agent_run_id] = dict(lease)
        return dict(lease)

    async def _fake_bootstrap():
        order.append("bootstrap")

    async def _fake_wait_for_layer(layer, timeout=0):
        for item in layer:
            yield {"type": "subagent_completed", "subtask_id": item["id"]}

    monkeypatch.setattr(coordinator, "_warmup_project_sandbox", _fake_warmup)
    monkeypatch.setattr(coordinator, "_ensure_bootstrap_standby_clone", _fake_bootstrap)
    monkeypatch.setattr(coordinator, "_wait_for_layer_completion", _fake_wait_for_layer)

    await _collect(coordinator.run(user_message="complex", thread_run_id="tr-bootstrap-order"))

    assert order.index("warmup") < order.index("bootstrap") < order.index("confirm_wait")


@pytest.mark.asyncio
async def test_coordinator_prewarm_failure_blocks_dispatch(monkeypatch):
    fake_actor = _FakeSubAgentActor()
    calls = {"transition": []}

    monkeypatch.setattr(coordinator_module, "MainAgent", _FakeMainAgent)
    monkeypatch.setattr(coordinator_module, "run_subagent", fake_actor)
    monkeypatch.setattr(
        coordinator_module,
        "topological_sort_layers",
        lambda subtasks, deps: [subtasks],
    )

    async def _confirmed(*args, **kwargs):
        return ConfirmationResult.CONFIRMED

    async def _fake_init_state(*args, **kwargs):
        return None

    async def _fake_transition(*args, **kwargs):
        calls["transition"].append((args, kwargs))
        return True

    async def _fake_update_subagent(*args, **kwargs):
        return None

    async def _fake_cleanup(*args, **kwargs):
        return None

    async def _fake_cleanup_results(*args, **kwargs):
        return 0

    monkeypatch.setattr(coordinator_module, "wait_for_confirmation", _confirmed)
    monkeypatch.setattr(coordinator_module, "init_state", _fake_init_state)
    monkeypatch.setattr(coordinator_module, "transition", _fake_transition)
    monkeypatch.setattr(coordinator_module, "update_subagent", _fake_update_subagent)
    monkeypatch.setattr(coordinator_module, "cleanup", _fake_cleanup)
    monkeypatch.setattr(coordinator_module, "cleanup_results", _fake_cleanup_results)

    coordinator = coordinator_module.ShadowCloneCoordinator(
        thread_id="thread-1",
        project_id="project-1",
        agent_run_id="run-4",
        model_key="kimi-k2.5",
        db_client=object(),
        mode=ShadowCloneMode.AUTO,
    )

    async def _boom():
        raise RuntimeError("warmup failed")

    async def _fake_wait_for_layer(layer, timeout=0):
        for item in layer:
            yield {"type": "subagent_completed", "subtask_id": item["id"]}

    monkeypatch.setattr(coordinator, "_warmup_project_sandbox", _boom)
    monkeypatch.setattr(coordinator, "_wait_for_layer_completion", _fake_wait_for_layer)

    events = await _collect(coordinator.run(user_message="complex", thread_run_id="tr-4"))

    assert any(event["type"] == "status" and event["status"] == "failed" for event in events)
    assert not any(event["type"] == "subagent_started" for event in events)
    assert not fake_actor.sent
    transitions = [args[1:3] for args, _kwargs in calls["transition"]]
    assert ("confirming", "failed") in transitions


@pytest.mark.asyncio
async def test_coordinator_bootstrap_clone_failure_does_not_block_dispatch(monkeypatch):
    fake_actor = _FakeSubAgentActor()
    lease_updates = []
    monkeypatch.setenv("SHADOW_CLONE_ENABLE_PROACTIVE_STANDBY", "true")

    monkeypatch.setattr(coordinator_module, "MainAgent", _FakeMainAgent)
    monkeypatch.setattr(coordinator_module, "run_subagent", fake_actor)
    monkeypatch.setattr(
        coordinator_module,
        "topological_sort_layers",
        lambda subtasks, deps: [subtasks],
    )

    async def _confirmed(*args, **kwargs):
        return ConfirmationResult.CONFIRMED

    async def _fake_init_state(*args, **kwargs):
        return None

    async def _fake_transition(*args, **kwargs):
        return True

    async def _fake_update_subagent(*args, **kwargs):
        return None

    async def _fake_cleanup(*args, **kwargs):
        return None

    async def _fake_cleanup_results(*args, **kwargs):
        return 0

    async def _fake_warmup():
        lease = {
            "run_id": coordinator.agent_run_id,
            "thread_id": coordinator.thread_id,
            "project_id": coordinator.project_id,
            "sandbox_id": "sandbox-primary",
            "sandbox_type": "desktop",
            "binding_state": "locked",
            "execution_epoch": 0,
            "lease_epoch": 0,
            "environment_status": "pending",
            "environment_ready": False,
            "environment_manifest": {},
        }
        _TEST_LEASES[coordinator.agent_run_id] = dict(lease)
        return dict(lease)

    async def _fake_get_db_client():
        return object()

    async def _fake_get_lease(_run_id):
        lease = dict(_TEST_LEASES.get(_run_id) or {})
        lease.update(
            {
                "run_id": _run_id,
                "project_id": coordinator.project_id,
                "sandbox_id": "sandbox-primary",
                "sandbox_type": "desktop",
                "binding_state": "locked",
            }
        )
        _TEST_LEASES[_run_id] = dict(lease)
        return dict(lease)

    async def _fake_update_lease(_run_id, **kwargs):
        lease_updates.append(kwargs)
        lease = dict(await _fake_get_lease(_run_id))
        if isinstance(kwargs.get("environment_manifest"), dict):
            kwargs = {
                **kwargs,
                "environment_manifest": {
                    **(lease.get("environment_manifest") or {}),
                    **kwargs["environment_manifest"],
                },
            }
        lease.update(kwargs)
        _TEST_LEASES[_run_id] = dict(lease)
        return dict(lease)

    async def _fake_wait_for_layer(layer, timeout=0):
        for item in layer:
            yield {"type": "subagent_completed", "subtask_id": item["id"]}

    async def _fake_clone_failure(*args, **kwargs):
        raise RuntimeError("bootstrap clone unavailable")

    fake_sandbox_module = types.ModuleType("sandbox")
    fake_sandbox_api_module = types.ModuleType("sandbox.api")
    fake_sandbox_api_module.create_shadow_clone_standby_sandbox = _fake_clone_failure
    fake_sandbox_module.api = fake_sandbox_api_module
    monkeypatch.setitem(sys.modules, "sandbox", fake_sandbox_module)
    monkeypatch.setitem(sys.modules, "sandbox.api", fake_sandbox_api_module)

    monkeypatch.setattr(coordinator_module, "wait_for_confirmation", _confirmed)
    monkeypatch.setattr(coordinator_module, "init_state", _fake_init_state)
    monkeypatch.setattr(coordinator_module, "transition", _fake_transition)
    monkeypatch.setattr(coordinator_module, "update_subagent", _fake_update_subagent)
    monkeypatch.setattr(coordinator_module, "cleanup", _fake_cleanup)
    monkeypatch.setattr(coordinator_module, "cleanup_results", _fake_cleanup_results)
    monkeypatch.setattr(coordinator_module, "get_run_sandbox_lease", _fake_get_lease)
    monkeypatch.setattr(
        coordinator_module,
        "update_run_sandbox_lease",
        _fake_update_lease,
    )

    coordinator = coordinator_module.ShadowCloneCoordinator(
        thread_id="thread-1",
        project_id="project-1",
        agent_run_id="run-bootstrap-soft-fail",
        model_key="kimi-k2.5",
        db_client=object(),
        mode=ShadowCloneMode.AUTO,
    )

    monkeypatch.setattr(coordinator, "_warmup_project_sandbox", _fake_warmup)
    monkeypatch.setattr(coordinator, "_get_db_client", _fake_get_db_client)
    monkeypatch.setattr(
        coordinator,
        "_ensure_bootstrap_standby_clone",
        _ORIGINAL_ENSURE_BOOTSTRAP_STANDBY_CLONE.__get__(
            coordinator,
            coordinator_module.ShadowCloneCoordinator,
        ),
    )
    monkeypatch.setattr(coordinator, "_wait_for_layer_completion", _fake_wait_for_layer)

    events = await _collect(
        coordinator.run(
            user_message="complex",
            thread_run_id="tr-bootstrap-soft-fail",
        ),
    )

    assert any(event["type"] == "subagent_started" for event in events)
    assert fake_actor.sent
    assert lease_updates
    assert any(
        update.get("last_clone_error") == "bootstrap clone unavailable"
        for update in lease_updates
    )


@pytest.mark.asyncio
async def test_checkpoint_completed_layer_skips_standby_clone_when_single_active_default(monkeypatch):
    create_calls = []
    lease_updates = []

    coordinator = coordinator_module.ShadowCloneCoordinator(
        thread_id="thread-checkpoint-single-active",
        project_id="project-checkpoint-single-active",
        agent_run_id="run-checkpoint-single-active",
        model_key="kimi-k2.5",
        db_client=object(),
        mode=ShadowCloneMode.AUTO,
    )

    lease = {
        "run_id": coordinator.agent_run_id,
        "thread_id": coordinator.thread_id,
        "project_id": coordinator.project_id,
        "sandbox_id": "sandbox-primary",
        "sandbox_type": "desktop",
        "binding_state": "locked",
        "execution_epoch": 0,
        "lease_epoch": 0,
        "environment_status": "ready",
        "environment_ready": True,
        "environment_manifest": {"sandbox": {"id": "sandbox-primary"}},
    }

    async def _fake_mark_checkpoint(_run_id, *, layer_index):
        assert layer_index == 0
        return None

    async def _fake_get_lease(_run_id):
        return dict(lease)

    async def _fake_update_lease(_run_id, **kwargs):
        create_calls.append(kwargs.get("standby_sandbox_id"))
        lease_updates.append(dict(kwargs))
        lease.update(kwargs)
        return dict(lease)

    async def _fake_create_standby(*_args, **_kwargs):
        raise AssertionError("standby clone should stay disabled on the default steady-state path")

    fake_sandbox_module = types.ModuleType("sandbox")
    fake_sandbox_api_module = types.ModuleType("sandbox.api")
    fake_sandbox_api_module.create_shadow_clone_standby_sandbox = _fake_create_standby
    fake_sandbox_module.api = fake_sandbox_api_module

    monkeypatch.setitem(sys.modules, "sandbox", fake_sandbox_module)
    monkeypatch.setitem(sys.modules, "sandbox.api", fake_sandbox_api_module)
    monkeypatch.setattr(coordinator_module, "mark_checkpoint", _fake_mark_checkpoint)
    monkeypatch.setattr(coordinator_module, "get_run_sandbox_lease", _fake_get_lease)
    monkeypatch.setattr(coordinator_module, "update_run_sandbox_lease", _fake_update_lease)
    monkeypatch.setattr(
        coordinator,
        "_checkpoint_completed_layer",
        _ORIGINAL_CHECKPOINT_COMPLETED_LAYER.__get__(
            coordinator,
            coordinator_module.ShadowCloneCoordinator,
        ),
    )

    error = await coordinator._checkpoint_completed_layer(
        layer_index=0,
        layer=[{"id": "task-1", "role": "researcher"}],
    )

    assert error is None
    assert create_calls == [None]
    assert lease_updates[-1]["checkpoint_layer_index"] == 0
    assert lease_updates[-1]["checkpoint_subtask_ids"] == ["task-1"]
    assert lease_updates[-1]["standby_sandbox_id"] is None
    assert lease_updates[-1]["last_clone_error"] is None


@pytest.mark.asyncio
async def test_coordinator_staggers_same_layer_dispatch(monkeypatch):
    fake_actor = _FakeSubAgentActor()
    sleep_calls = []

    monkeypatch.setattr(coordinator_module, "MainAgent", _FakeMainAgent)
    monkeypatch.setattr(coordinator_module, "run_subagent", fake_actor)
    monkeypatch.setattr(
        coordinator_module,
        "topological_sort_layers",
        lambda subtasks, deps: [[
            {"id": "task-1", "role": "researcher"},
            {"id": "task-2", "role": "analyst"},
            {"id": "task-3", "role": "builder"},
        ]],
    )

    async def _confirmed(*args, **kwargs):
        return ConfirmationResult.CONFIRMED

    async def _fake_init_state(*args, **kwargs):
        return None

    async def _fake_transition(*args, **kwargs):
        return True

    async def _fake_update_subagent(*args, **kwargs):
        return None

    async def _fake_cleanup(*args, **kwargs):
        return None

    async def _fake_cleanup_results(*args, **kwargs):
        return 0

    async def _fake_sleep(seconds):
        sleep_calls.append(seconds)

    monkeypatch.setattr(coordinator_module, "wait_for_confirmation", _confirmed)
    monkeypatch.setattr(coordinator_module, "init_state", _fake_init_state)
    monkeypatch.setattr(coordinator_module, "transition", _fake_transition)
    monkeypatch.setattr(coordinator_module, "update_subagent", _fake_update_subagent)
    monkeypatch.setattr(coordinator_module, "cleanup", _fake_cleanup)
    monkeypatch.setattr(coordinator_module, "cleanup_results", _fake_cleanup_results)
    monkeypatch.setattr(coordinator_module.asyncio, "sleep", _fake_sleep)

    coordinator = coordinator_module.ShadowCloneCoordinator(
        thread_id="thread-1",
        project_id="project-1",
        agent_run_id="run-5",
        model_key="kimi-k2.5",
        db_client=object(),
        mode=ShadowCloneMode.AUTO,
    )

    async def _fake_wait_for_layer(layer, timeout=0):
        for item in layer:
            yield {"type": "subagent_completed", "subtask_id": item["id"]}

    monkeypatch.setattr(coordinator, "_wait_for_layer_completion", _fake_wait_for_layer)

    await _collect(coordinator.run(user_message="complex", thread_run_id="tr-5"))

    assert sleep_calls == [
        coordinator_module.SHADOW_CLONE_SUBAGENT_DISPATCH_STAGGER_SECONDS,
        coordinator_module.SHADOW_CLONE_SUBAGENT_DISPATCH_STAGGER_SECONDS,
    ]


@pytest.mark.asyncio
async def test_wait_for_layer_completion_reconciles_state_without_pubsub(monkeypatch):
    class _FakePubSub:
        async def subscribe(self, *args):
            return None

        async def get_message(self, **kwargs):
            return None

        async def unsubscribe(self, *args):
            return None

        async def close(self):
            return None

    async def _fake_get_state(_run_id):
        return {
            "subagents": {
                "task-1": {"status": "completed"},
            }
        }

    async def _fake_create_pubsub():
        return _FakePubSub()

    monkeypatch.setattr(coordinator_module.redis_service, "create_pubsub", _fake_create_pubsub)
    monkeypatch.setattr(coordinator_module, "get_state", _fake_get_state)

    coordinator = coordinator_module.ShadowCloneCoordinator(
        thread_id="thread-1",
        project_id="project-1",
        agent_run_id="run-reconcile",
        model_key="kimi-k2.5",
        db_client=object(),
        mode=ShadowCloneMode.AUTO,
    )

    events = await _collect(
        coordinator._wait_for_layer_completion([
            {"id": "task-1", "role": "researcher"},
        ], timeout=1),
    )

    assert events == [{"type": "subagent_completed", "subtask_id": "task-1"}]


@pytest.mark.asyncio
async def test_wait_for_layer_completion_stops_when_shadow_clone_state_is_terminal(monkeypatch):
    class _FakePubSub:
        async def subscribe(self, *args):
            return None

        async def get_message(self, **kwargs):
            await asyncio.sleep(0)
            return None

        async def unsubscribe(self, *args):
            return None

        async def close(self):
            return None

    async def _fake_get_state(_run_id):
        return {
            "status": "cancelled",
            "execution_epoch": 4,
            "subagents": {
                "task-1": {"status": "running", "attempt_index": 1},
            },
        }

    async def _fake_get_lease(_run_id):
        return {
            "binding_state": "released",
            "execution_epoch": 4,
            "lease_epoch": 4,
            "environment_ready": False,
            "environment_status": "cancelled",
        }

    async def _fake_create_pubsub():
        return _FakePubSub()

    monkeypatch.setattr(coordinator_module.redis_service, "create_pubsub", _fake_create_pubsub)
    monkeypatch.setattr(coordinator_module, "get_state", _fake_get_state)
    monkeypatch.setattr(coordinator_module, "get_run_sandbox_lease", _fake_get_lease)

    coordinator = coordinator_module.ShadowCloneCoordinator(
        thread_id="thread-terminal-stop",
        project_id="project-terminal-stop",
        agent_run_id="run-terminal-stop",
        model_key="kimi-k2.5",
        db_client=object(),
        mode=ShadowCloneMode.AUTO,
    )

    events = await _collect(
        coordinator._wait_for_layer_completion(
            [{"id": "task-1", "role": "researcher"}],
            execution_epoch=4,
            timeout=120,
        ),
    )

    assert events == [
        {
            "type": "status",
            "status": "cancelled",
            "reason": "shadow_clone_cancelled",
            "shadow_clone_status": "cancelled",
            "execution_epoch": 4,
            "completion_mode": None,
            "terminal_reason": "shadow_clone_cancelled",
        }
    ]


@pytest.mark.asyncio
async def test_wait_for_layer_completion_stops_when_lease_is_released_from_aggregating_state(
    monkeypatch,
):
    class _FakePubSub:
        async def subscribe(self, *args):
            return None

        async def get_message(self, **kwargs):
            await asyncio.sleep(0)
            return None

        async def unsubscribe(self, *args):
            return None

        async def close(self):
            return None

    async def _fake_get_state(_run_id):
        return {
            "status": "aggregating",
            "execution_epoch": 4,
            "completed": 1,
            "failed": 0,
            "running": 0,
            "subagents": {
                "task-1": {
                    "status": "completed",
                    "role": "researcher",
                    "attempt_index": 1,
                },
            },
            "live_activity": {
                "scope": "main_agent",
                "phase": "aggregate",
                "reason": "aggregate_started",
            },
        }

    async def _fake_get_lease(_run_id):
        return {
            "binding_state": "released",
            "execution_epoch": 4,
            "lease_epoch": 4,
            "environment_ready": False,
            "environment_status": "cancelled",
        }

    async def _fake_create_pubsub():
        return _FakePubSub()

    monkeypatch.setattr(coordinator_module.redis_service, "create_pubsub", _fake_create_pubsub)
    monkeypatch.setattr(coordinator_module, "get_state", _fake_get_state)
    monkeypatch.setattr(coordinator_module, "get_run_sandbox_lease", _fake_get_lease)

    coordinator = coordinator_module.ShadowCloneCoordinator(
        thread_id="thread-terminal-aggregating-stop",
        project_id="project-terminal-aggregating-stop",
        agent_run_id="run-terminal-aggregating-stop",
        model_key="kimi-k2.5",
        db_client=object(),
        mode=ShadowCloneMode.AUTO,
    )

    events = await _collect(
        coordinator._wait_for_layer_completion(
            [{"id": "task-1", "role": "researcher"}],
            execution_epoch=4,
            timeout=120,
        ),
    )

    assert events == [
        {
            "type": "status",
            "status": "stopped",
            "reason": "lease_released",
            "execution_epoch": 4,
        }
    ]


@pytest.mark.asyncio
async def test_wait_for_layer_completion_preserves_failed_shadow_clone_terminal_status(
    monkeypatch,
):
    class _FakePubSub:
        async def subscribe(self, *args):
            return None

        async def get_message(self, **kwargs):
            await asyncio.sleep(0)
            return None

        async def unsubscribe(self, *args):
            return None

        async def close(self):
            return None

    async def _fake_get_state(_run_id):
        return {
            "status": "failed",
            "execution_epoch": 4,
            "subagents": {
                "task-1": {"status": "running", "attempt_index": 1},
            },
        }

    async def _fake_get_lease(_run_id):
        return {
            "binding_state": "locked",
            "execution_epoch": 4,
            "lease_epoch": 4,
            "environment_ready": False,
            "environment_status": "failed",
        }

    async def _fake_create_pubsub():
        return _FakePubSub()

    monkeypatch.setattr(coordinator_module.redis_service, "create_pubsub", _fake_create_pubsub)
    monkeypatch.setattr(coordinator_module, "get_state", _fake_get_state)
    monkeypatch.setattr(coordinator_module, "get_run_sandbox_lease", _fake_get_lease)

    coordinator = coordinator_module.ShadowCloneCoordinator(
        thread_id="thread-terminal-failed",
        project_id="project-terminal-failed",
        agent_run_id="run-terminal-failed",
        model_key="kimi-k2.5",
        db_client=object(),
        mode=ShadowCloneMode.AUTO,
    )

    events = await _collect(
        coordinator._wait_for_layer_completion(
            [{"id": "task-1", "role": "researcher"}],
            execution_epoch=4,
            timeout=120,
        ),
    )

    assert events == [
        {
            "type": "status",
            "status": "failed",
            "reason": "shadow_clone_failed",
            "shadow_clone_status": "failed",
            "execution_epoch": 4,
            "completion_mode": None,
            "terminal_reason": "shadow_clone_failed",
        }
    ]


@pytest.mark.asyncio
async def test_wait_for_layer_completion_stops_when_lease_is_released(monkeypatch):
    class _FakePubSub:
        async def subscribe(self, *args):
            return None

        async def get_message(self, **kwargs):
            await asyncio.sleep(0)
            return None

        async def unsubscribe(self, *args):
            return None

        async def close(self):
            return None

    async def _fake_get_state(_run_id):
        return {
            "status": "running",
            "execution_epoch": 7,
            "subagents": {
                "task-1": {"status": "running", "attempt_index": 1},
            },
        }

    async def _fake_get_lease(_run_id):
        return {
            "binding_state": "released",
            "execution_epoch": 7,
            "lease_epoch": 7,
            "environment_ready": False,
            "environment_status": "cancelled",
        }

    async def _fake_create_pubsub():
        return _FakePubSub()

    monkeypatch.setattr(coordinator_module.redis_service, "create_pubsub", _fake_create_pubsub)
    monkeypatch.setattr(coordinator_module, "get_state", _fake_get_state)
    monkeypatch.setattr(coordinator_module, "get_run_sandbox_lease", _fake_get_lease)

    coordinator = coordinator_module.ShadowCloneCoordinator(
        thread_id="thread-lease-released",
        project_id="project-lease-released",
        agent_run_id="run-lease-released",
        model_key="kimi-k2.5",
        db_client=object(),
        mode=ShadowCloneMode.AUTO,
    )

    events = await _collect(
        coordinator._wait_for_layer_completion(
            [{"id": "task-1", "role": "researcher"}],
            execution_epoch=7,
            timeout=120,
        ),
    )

    assert events == [
        {
            "type": "status",
            "status": "stopped",
            "reason": "lease_released",
            "execution_epoch": 7,
        }
    ]


@pytest.mark.asyncio
async def test_wait_for_layer_completion_resubscribes_after_connection_error(monkeypatch):
    class _FakePubSub:
        def __init__(self):
            self.subscribe_calls = 0
            self.get_message_calls = 0

        async def subscribe(self, *args):
            self.subscribe_calls += 1
            return None

        async def get_message(self, **kwargs):
            self.get_message_calls += 1
            if self.get_message_calls == 1:
                raise ConnectionError("pubsub dropped")
            return {
                "type": "message",
                "data": json.dumps({"subtask_id": "task-1", "status": "completed"}),
            }

        async def unsubscribe(self, *args):
            return None

        async def close(self):
            return None

    fake_pubsub = _FakePubSub()
    state_calls = {"count": 0}

    async def _fake_get_state(_run_id):
        state_calls["count"] += 1
        if state_calls["count"] == 1:
            return {"subagents": {"task-1": {"status": "running"}}}
        return {"subagents": {"task-1": {"status": "completed"}}}

    async def _fake_create_pubsub():
        return fake_pubsub

    monkeypatch.setattr(coordinator_module.redis_service, "create_pubsub", _fake_create_pubsub)
    monkeypatch.setattr(coordinator_module, "get_state", _fake_get_state)

    coordinator = coordinator_module.ShadowCloneCoordinator(
        thread_id="thread-1",
        project_id="project-1",
        agent_run_id="run-resubscribe",
        model_key="kimi-k2.5",
        db_client=object(),
        mode=ShadowCloneMode.AUTO,
    )

    events = await _collect(
        coordinator._wait_for_layer_completion([
            {"id": "task-1", "role": "researcher"},
        ], timeout=2),
    )

    assert fake_pubsub.subscribe_calls == 2
    assert events == [{"type": "subagent_completed", "subtask_id": "task-1"}]


@pytest.mark.asyncio
async def test_wait_for_layer_completion_yields_activity_updates(monkeypatch):
    class _FakePubSub:
        def __init__(self):
            self.messages = [
                {
                    "type": "message",
                    "data": json.dumps({
                        "event_type": "activity",
                        "subtask_id": "task-1",
                        "sequence": 1,
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
                    }),
                },
                {
                    "type": "message",
                    "data": json.dumps({"subtask_id": "task-1", "status": "completed"}),
                },
            ]

        async def subscribe(self, *args):
            return None

        async def get_message(self, **kwargs):
            return self.messages.pop(0) if self.messages else None

        async def unsubscribe(self, *args):
            return None

        async def close(self):
            return None

    async def _fake_create_pubsub():
        return _FakePubSub()

    async def _fake_get_state(_run_id):
        return {
            "subagents": {
                "task-1": {"status": "completed"},
            }
        }

    monkeypatch.setattr(coordinator_module.redis_service, "create_pubsub", _fake_create_pubsub)
    monkeypatch.setattr(coordinator_module, "get_state", _fake_get_state)

    coordinator = coordinator_module.ShadowCloneCoordinator(
        thread_id="thread-1",
        project_id="project-1",
        agent_run_id="run-activity",
        model_key="kimi-k2.5",
        db_client=object(),
        mode=ShadowCloneMode.AUTO,
    )

    events = await _collect(
        coordinator._wait_for_layer_completion([
            {"id": "task-1", "role": "researcher"},
        ], timeout=2),
    )

    assert events[0]["type"] == "subagent_activity"
    assert events[0]["subtask_id"] == "task-1"
    assert events[0]["metadata"]["stream_status"] == "tool_call_chunk"
    assert events[-1] == {"type": "subagent_completed", "subtask_id": "task-1"}


@pytest.mark.asyncio
async def test_wait_for_layer_completion_defers_reconcile_for_pure_activity_until_next_tick(
    monkeypatch,
):
    class _FakePubSub:
        def __init__(self):
            self.get_message_calls = 0
            self.messages = [
                {
                    "type": "message",
                    "data": json.dumps(
                        {
                            "event_type": "activity",
                            "subtask_id": "task-1",
                            "sequence": 1,
                            "role": "researcher",
                            "message_type": "assistant",
                            "content": {"text": "working"},
                            "metadata": {"stream_status": "delta"},
                        }
                    ),
                },
                None,
            ]

        async def subscribe(self, *args):
            return None

        async def get_message(self, **kwargs):
            self.get_message_calls += 1
            return self.messages.pop(0) if self.messages else None

        async def unsubscribe(self, *args):
            return None

        async def close(self):
            return None

    fake_pubsub = _FakePubSub()
    state_calls = {"count": 0}

    async def _fake_create_pubsub():
        return fake_pubsub

    async def _fake_get_state(_run_id):
        state_calls["count"] += 1
        if state_calls["count"] == 1:
            return {
                "status": "running",
                "execution_epoch": 0,
                "subagents": {
                    "task-1": {"status": "running"},
                },
            }
        return {
            "status": "running",
            "execution_epoch": 0,
            "subagents": {
                "task-1": {"status": "completed"},
            },
        }

    async def _fake_get_lease(_run_id):
        return {
            "binding_state": "locked",
            "execution_epoch": 0,
            "lease_epoch": 0,
            "environment_ready": True,
            "environment_status": "ready",
        }

    monkeypatch.setattr(
        coordinator_module.redis_service,
        "create_pubsub",
        _fake_create_pubsub,
    )
    monkeypatch.setattr(coordinator_module, "get_state", _fake_get_state)
    monkeypatch.setattr(
        coordinator_module,
        "get_run_sandbox_lease",
        _fake_get_lease,
    )
    monkeypatch.setattr(
        coordinator_module,
        "SHADOW_CLONE_COORDINATOR_RECONCILE_INTERVAL_SECONDS",
        0.001,
    )

    coordinator = coordinator_module.ShadowCloneCoordinator(
        thread_id="thread-activity-deferral",
        project_id="project-activity-deferral",
        agent_run_id="run-activity-deferral",
        model_key="kimi-k2.5",
        db_client=object(),
        mode=ShadowCloneMode.AUTO,
    )

    events = await _collect(
        coordinator._wait_for_layer_completion(
            [{"id": "task-1", "role": "researcher"}],
            execution_epoch=0,
            timeout=2,
        ),
    )

    assert fake_pubsub.get_message_calls == 2
    assert events[0]["type"] == "subagent_activity"
    assert events[-1] == {"type": "subagent_completed", "subtask_id": "task-1"}


@pytest.mark.asyncio
async def test_wait_for_layer_completion_releases_lease_when_cancelled_with_pending_subtasks(
    monkeypatch,
):
    release_calls = []
    subscribed = asyncio.Event()

    class _FakePubSub:
        async def subscribe(self, *args):
            subscribed.set()
            return None

        async def get_message(self, **kwargs):
            await asyncio.sleep(3600)
            return None

        async def unsubscribe(self, *args):
            return None

        async def close(self):
            return None

    async def _fake_create_pubsub():
        return _FakePubSub()

    async def _fake_get_state(_run_id):
        return {"subagents": {"task-1": {"status": "running", "attempt_index": 1}}}

    async def _fake_get_lease(_run_id):
        return dict(_TEST_LEASES[_run_id])

    async def _fake_set_binding_state(run_id, binding_state, last_error=None):
        release_calls.append((run_id, binding_state, last_error))
        lease = dict(_TEST_LEASES[run_id])
        lease["binding_state"] = binding_state
        lease["last_error"] = last_error
        _TEST_LEASES[run_id] = lease
        return dict(lease)

    run_id = "run-cancelled-layer"
    _TEST_LEASES[run_id] = {
        "run_id": run_id,
        "thread_id": "thread-cancelled-layer",
        "project_id": "project-cancelled-layer",
        "sandbox_id": "sandbox-primary",
        "sandbox_type": "code",
        "binding_state": "locked",
        "execution_epoch": 0,
        "lease_epoch": 0,
        "environment_status": "ready",
        "environment_ready": True,
        "environment_manifest": {},
    }

    monkeypatch.setattr(coordinator_module.redis_service, "create_pubsub", _fake_create_pubsub)
    monkeypatch.setattr(coordinator_module, "get_state", _fake_get_state)
    monkeypatch.setattr(coordinator_module, "get_run_sandbox_lease", _fake_get_lease)
    monkeypatch.setattr(
        coordinator_module,
        "set_run_sandbox_binding_state",
        _fake_set_binding_state,
    )

    coordinator = coordinator_module.ShadowCloneCoordinator(
        thread_id="thread-cancelled-layer",
        project_id="project-cancelled-layer",
        agent_run_id=run_id,
        model_key="kimi-k2.5",
        db_client=object(),
        mode=ShadowCloneMode.AUTO,
    )

    task = asyncio.create_task(
        _collect(
            coordinator._wait_for_layer_completion(
                [{"id": "task-1", "role": "researcher"}],
                execution_epoch=0,
                timeout=120,
            )
        )
    )
    await subscribed.wait()
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert release_calls == [(run_id, "released", None)]
    assert _TEST_LEASES[run_id]["binding_state"] == "released"


@pytest.mark.asyncio
async def test_wait_for_layer_completion_marks_timeouts_for_current_epoch(monkeypatch):
    update_calls = []

    class _FakePubSub:
        async def subscribe(self, *args):
            return None

        async def get_message(self, **kwargs):
            await asyncio.sleep(0)
            return None

        async def unsubscribe(self, *args):
            return None

        async def close(self):
            return None

    async def _fake_create_pubsub():
        return _FakePubSub()

    async def _fake_get_state(_run_id):
        return {"subagents": {"task-1": {"status": "running", "attempt_index": 1}}}

    async def _fake_update_subagent_for_epoch(*args, **kwargs):
        update_calls.append((args, kwargs))
        return True

    monkeypatch.setattr(coordinator_module.redis_service, "create_pubsub", _fake_create_pubsub)
    monkeypatch.setattr(coordinator_module, "get_state", _fake_get_state)
    monkeypatch.setattr(
        coordinator_module,
        "update_subagent_for_epoch",
        _fake_update_subagent_for_epoch,
    )
    monkeypatch.setattr(
        coordinator_module,
        "is_runtime_recoverable_failure_class",
        lambda _failure_class: False,
    )

    coordinator = coordinator_module.ShadowCloneCoordinator(
        thread_id="thread-timeout",
        project_id="project-timeout",
        agent_run_id="run-timeout",
        model_key="kimi-k2.5",
        db_client=object(),
        mode=ShadowCloneMode.AUTO,
    )

    events = await _collect(
        coordinator._wait_for_layer_completion(
            [{"id": "task-1", "role": "researcher"}],
            execution_epoch=7,
            timeout=0,
        ),
    )

    assert update_calls == [
        (
            ("run-timeout", "task-1", "failed"),
            {"expected_epoch": 7, "result_summary": "timeout"},
        )
    ]
    assert events == [
        {
            "type": "subagent_failed",
            "subtask_id": "task-1",
            "role": "researcher",
            "error": "timeout",
            "attempt_index": 1,
            "failure_class": "timeout",
        }
    ]


@pytest.mark.asyncio
async def test_wait_for_layer_completion_fails_last_straggler_after_peer_completion(monkeypatch):
    update_calls = []

    class _FakePubSub:
        async def subscribe(self, *args):
            return None

        async def get_message(self, **kwargs):
            await asyncio.sleep(0)
            return None

        async def unsubscribe(self, *args):
            return None

        async def close(self):
            return None

    async def _fake_create_pubsub():
        return _FakePubSub()

    async def _fake_get_state(_run_id):
        return {
            "subagents": {
                "task-1": {"status": "completed", "attempt_index": 1},
                "task-2": {"status": "running", "attempt_index": 1},
            }
        }

    async def _fake_update_subagent_for_epoch(*args, **kwargs):
        update_calls.append((args, kwargs))
        return True

    monkeypatch.setattr(coordinator_module.redis_service, "create_pubsub", _fake_create_pubsub)
    monkeypatch.setattr(coordinator_module, "get_state", _fake_get_state)
    monkeypatch.setattr(
        coordinator_module,
        "update_subagent_for_epoch",
        _fake_update_subagent_for_epoch,
    )
    monkeypatch.setattr(
        coordinator_module,
        "_shadow_clone_subagent_straggler_grace_seconds",
        lambda: 0.0,
    )

    coordinator = coordinator_module.ShadowCloneCoordinator(
        thread_id="thread-straggler",
        project_id="project-straggler",
        agent_run_id="run-straggler",
        model_key="kimi-k2.5",
        db_client=object(),
        mode=ShadowCloneMode.AUTO,
    )

    events = await _collect(
        coordinator._wait_for_layer_completion(
            [
                {"id": "task-1", "role": "researcher"},
                {"id": "task-2", "role": "validator"},
            ],
            execution_epoch=3,
            timeout=120,
        ),
    )

    assert update_calls == [
        (
            ("run-straggler", "task-2", "failed"),
            {
                "expected_epoch": 3,
                "result_summary": "straggler_timeout_after_peer_completion",
            },
        )
    ]
    assert events == [
        {"type": "subagent_completed", "subtask_id": "task-1", "attempt_index": 1},
        {
            "type": "subagent_failed",
            "subtask_id": "task-2",
            "role": "validator",
            "error": "straggler_timeout_after_peer_completion",
            "attempt_index": 1,
            "failure_class": "stalled",
        },
    ]


@pytest.mark.asyncio
async def test_maybe_schedule_runtime_recovery_prefers_wake_first(monkeypatch):
    state = {
        "subagents": {
            "task-1": {
                "status": "failed",
                "role": "researcher",
                "attempt_index": 1,
                "failure_class": "timeout",
                "last_error": "timeout",
                "recovery": {
                    "mode": None,
                    "phase": None,
                    "reason": None,
                    "wake_attempts": 0,
                    "replacement_attempts": 0,
                    "replacement_context_id": None,
                    "handoff_summary": None,
                    "last_command_id": None,
                    "updated_at": None,
                },
            }
        }
    }
    append_calls = []
    delete_calls = []
    dispatch_calls = []

    async def _fake_get_state(_run_id):
        return json.loads(json.dumps(state))

    async def _fake_record_subagent_failures(_run_id, *, failed_subtasks):
        failure = failed_subtasks[0]
        state["subagents"]["task-1"]["failure_class"] = failure["failure_class"]
        state["subagents"]["task-1"]["last_error"] = failure["error"]
        return state

    async def _fake_update_subagent_recovery(_run_id, _subtask_id, **patch):
        recovery = state["subagents"]["task-1"]["recovery"]
        for key, value in patch.items():
            if key == "expected_epoch":
                continue
            recovery[key] = value
        recovery["updated_at"] = "2026-03-26T00:00:00Z"
        return state

    async def _fake_append_supervisor_command(**kwargs):
        append_calls.append(kwargs)
        return {"note_id": "note-1"}

    async def _fake_delete_results(_run_id, subtask_ids):
        delete_calls.append(subtask_ids)
        return 1

    async def _fake_prepare_subagents_for_retry(_run_id, subtask_ids, **_kwargs):
        assert subtask_ids == ["task-1"]
        state["subagents"]["task-1"]["status"] = "pending"
        state["subagents"]["task-1"]["attempt_index"] = 2
        return state

    async def _fake_dispatch_subagent_attempt(**kwargs):
        dispatch_calls.append(kwargs)
        return {
            "type": "subagent_started",
            "subtask_id": "task-1",
            "role": "researcher",
            "attempt_index": 2,
        }

    monkeypatch.setattr(coordinator_module, "get_state", _fake_get_state)
    monkeypatch.setattr(
        coordinator_module,
        "record_subagent_failures",
        _fake_record_subagent_failures,
    )
    monkeypatch.setattr(
        coordinator_module,
        "update_subagent_recovery",
        _fake_update_subagent_recovery,
    )
    monkeypatch.setattr(
        coordinator_module,
        "append_supervisor_command",
        _fake_append_supervisor_command,
    )
    monkeypatch.setattr(coordinator_module, "delete_results", _fake_delete_results)
    monkeypatch.setattr(
        coordinator_module,
        "prepare_subagents_for_retry",
        _fake_prepare_subagents_for_retry,
    )

    coordinator = coordinator_module.ShadowCloneCoordinator(
        thread_id="thread-runtime-wake",
        project_id="project-runtime-wake",
        agent_run_id="run-runtime-wake",
        model_key="kimi-k2.5",
        db_client=object(),
        mode=ShadowCloneMode.AUTO,
    )
    coordinator._set_live_activity = AsyncMock(
        return_value={"scope": "shadow_clone_main", "phase": "execution"},
    )
    coordinator._dispatch_subagent_attempt = AsyncMock(
        side_effect=_fake_dispatch_subagent_attempt,
    )

    events = await coordinator._maybe_schedule_runtime_recovery(
        subtask={"id": "task-1", "role": "researcher"},
        failure_event={
            "subtask_id": "task-1",
            "role": "researcher",
            "error": "timeout",
            "attempt_index": 1,
            "failure_class": "timeout",
        },
        layer_index=0,
        execution_epoch=0,
        layer_tasks=[],
    )

    assert [event["type"] for event in events] == [
        "shadow_clone_subagent_recovering",
        "shadow_clone_subagent_wake_sent",
        "shadow_clone_subagent_resumed",
        "subagent_started",
    ]
    assert append_calls[0]["command_type"] == "wake"
    assert append_calls[0]["attempt_index"] == 2
    assert delete_calls == [["task-1"]]
    assert dispatch_calls[0]["shadow_recovery_mode"] == "wake_first"
    assert dispatch_calls[0]["shadow_recovery_handoff_summary"] == (
        "Continue this subtask from the existing context already stored in memory."
    )
    assert state["subagents"]["task-1"]["recovery"]["phase"] == "wake_dispatched"
    assert state["subagents"]["task-1"]["recovery"]["wake_attempts"] == 1


@pytest.mark.asyncio
async def test_maybe_schedule_runtime_recovery_falls_back_to_replacement(monkeypatch):
    state = {
        "subagents": {
            "task-1": {
                "status": "failed",
                "role": "researcher",
                "attempt_index": 2,
                "failure_class": "timeout",
                "last_error": "timeout again",
                "result_summary": "partial draft available",
                "recovery": {
                    "mode": "wake_first",
                    "phase": "wake_dispatched",
                    "reason": "timeout",
                    "wake_attempts": 1,
                    "replacement_attempts": 0,
                    "replacement_context_id": None,
                    "handoff_summary": None,
                    "last_command_id": "cmd-1",
                    "updated_at": "2026-03-26T00:00:00Z",
                },
            }
        }
    }
    append_calls = []
    dispatch_calls = []

    async def _fake_get_state(_run_id):
        return json.loads(json.dumps(state))

    async def _fake_record_subagent_failures(_run_id, *, failed_subtasks):
        failure = failed_subtasks[0]
        state["subagents"]["task-1"]["failure_class"] = failure["failure_class"]
        state["subagents"]["task-1"]["last_error"] = failure["error"]
        return state

    async def _fake_update_subagent_recovery(_run_id, _subtask_id, **patch):
        recovery = state["subagents"]["task-1"]["recovery"]
        for key, value in patch.items():
            if key == "expected_epoch":
                continue
            recovery[key] = value
        recovery["updated_at"] = "2026-03-26T00:00:00Z"
        return state

    async def _fake_append_supervisor_command(**kwargs):
        append_calls.append(kwargs)
        return {"note_id": "note-2"}

    async def _fake_delete_results(_run_id, _subtask_ids):
        return 1

    async def _fake_prepare_subagents_for_retry(_run_id, subtask_ids, **_kwargs):
        assert subtask_ids == ["task-1"]
        state["subagents"]["task-1"]["status"] = "pending"
        state["subagents"]["task-1"]["attempt_index"] = 3
        return state

    async def _fake_dispatch_subagent_attempt(**kwargs):
        dispatch_calls.append(kwargs)
        return {
            "type": "subagent_started",
            "subtask_id": "task-1",
            "role": "researcher",
            "attempt_index": 3,
        }

    monkeypatch.setattr(coordinator_module, "get_state", _fake_get_state)
    monkeypatch.setattr(
        coordinator_module,
        "record_subagent_failures",
        _fake_record_subagent_failures,
    )
    monkeypatch.setattr(
        coordinator_module,
        "update_subagent_recovery",
        _fake_update_subagent_recovery,
    )
    monkeypatch.setattr(
        coordinator_module,
        "append_supervisor_command",
        _fake_append_supervisor_command,
    )
    monkeypatch.setattr(coordinator_module, "delete_results", _fake_delete_results)
    monkeypatch.setattr(
        coordinator_module,
        "prepare_subagents_for_retry",
        _fake_prepare_subagents_for_retry,
    )

    coordinator = coordinator_module.ShadowCloneCoordinator(
        thread_id="thread-runtime-replacement",
        project_id="project-runtime-replacement",
        agent_run_id="run-runtime-replacement",
        model_key="kimi-k2.5",
        db_client=object(),
        mode=ShadowCloneMode.AUTO,
    )
    coordinator._set_live_activity = AsyncMock(
        return_value={"scope": "shadow_clone_main", "phase": "execution"},
    )
    coordinator._dispatch_subagent_attempt = AsyncMock(
        side_effect=_fake_dispatch_subagent_attempt,
    )

    events = await coordinator._maybe_schedule_runtime_recovery(
        subtask={"id": "task-1", "role": "researcher"},
        failure_event={
            "subtask_id": "task-1",
            "role": "researcher",
            "error": "timeout again",
            "attempt_index": 2,
            "failure_class": "timeout",
        },
        layer_index=0,
        execution_epoch=0,
        layer_tasks=[],
    )

    assert [event["type"] for event in events] == [
        "shadow_clone_subagent_recovering",
        "shadow_clone_subagent_replacement_started",
        "subagent_started",
    ]
    assert append_calls[0]["command_type"] == "replacement_handoff"
    assert append_calls[0]["attempt_index"] == 3
    assert dispatch_calls[0]["shadow_recovery_mode"] == "replacement"
    assert "replacement:" in str(dispatch_calls[0]["shadow_thread_run_id"])
    assert state["subagents"]["task-1"]["recovery"]["phase"] == "replacement_started"
    assert state["subagents"]["task-1"]["recovery"]["replacement_attempts"] == 1
    assert state["subagents"]["task-1"]["recovery"]["replacement_context_id"] is not None


@pytest.mark.asyncio
async def test_wait_for_layer_completion_emits_replacement_completed_event(monkeypatch):
    state = {
        "subagents": {
            "task-1": {
                "status": "completed",
                "role": "researcher",
                "attempt_index": 2,
                "result_summary": "done",
                "recovery": {
                    "mode": "replacement",
                    "phase": "replacement_started",
                    "reason": "timeout",
                    "wake_attempts": 1,
                    "replacement_attempts": 1,
                    "replacement_context_id": "ctx-123",
                    "handoff_summary": "Continue from the partial outline.",
                    "last_command_id": "cmd-2",
                    "updated_at": "2026-03-26T00:00:00Z",
                },
            }
        }
    }
    recovery_updates = []

    class _FakePubSub:
        async def subscribe(self, *args):
            return None

        async def get_message(self, **kwargs):
            await asyncio.sleep(0)
            return None

        async def unsubscribe(self, *args):
            return None

        async def close(self):
            return None

    async def _fake_create_pubsub():
        return _FakePubSub()

    async def _fake_get_state(_run_id):
        return json.loads(json.dumps(state))

    async def _fake_update_subagent_recovery(_run_id, _subtask_id, **patch):
        recovery = state["subagents"]["task-1"]["recovery"]
        recovery_updates.append(patch)
        for key, value in patch.items():
            if key == "expected_epoch":
                continue
            recovery[key] = value
        recovery["updated_at"] = "2026-03-26T00:00:01Z"
        return state

    monkeypatch.setattr(coordinator_module.redis_service, "create_pubsub", _fake_create_pubsub)
    monkeypatch.setattr(coordinator_module, "get_state", _fake_get_state)
    monkeypatch.setattr(
        coordinator_module,
        "update_subagent_recovery",
        _fake_update_subagent_recovery,
    )

    coordinator = coordinator_module.ShadowCloneCoordinator(
        thread_id="thread-replacement-complete",
        project_id="project-replacement-complete",
        agent_run_id="run-replacement-complete",
        model_key="kimi-k2.5",
        db_client=object(),
        mode=ShadowCloneMode.AUTO,
    )
    coordinator._set_live_activity = AsyncMock(
        return_value={"scope": "shadow_clone_main", "phase": "execution"},
    )

    events = await _collect(
        coordinator._wait_for_layer_completion(
            [{"id": "task-1", "role": "researcher"}],
            execution_epoch=0,
            timeout=2,
        ),
    )

    assert [event["type"] for event in events] == [
        "shadow_clone_subagent_replacement_completed",
        "subagent_completed",
    ]
    assert events[0]["replacement_context_id"] == "ctx-123"
    assert events[0]["handoff_summary"] == "Continue from the partial outline."
    assert recovery_updates[0]["phase"] == "replacement_succeeded"


@pytest.mark.asyncio
async def test_promote_standby_clone_replenishes_next_clone_before_retry(monkeypatch):
    coordinator = coordinator_module.ShadowCloneCoordinator(
        thread_id="thread-promote",
        project_id="project-promote",
        agent_run_id="run-promote",
        model_key="kimi-k2.5",
        db_client=object(),
        mode=ShadowCloneMode.AUTO,
    )

    lease = {
        "run_id": "run-promote",
        "project_id": "project-promote",
        "sandbox_id": "sb-active",
        "sandbox_type": "desktop",
        "standby_sandbox_id": "sb-standby",
        "standby_sandbox_type": "desktop",
        "checkpoint_layer_index": 0,
        "checkpoint_subtask_ids": ["task-1"],
    }
    lease_state = lease
    order = []

    async def _get_lease(_run_id):
        return dict(lease)

    async def _set_binding_state(_run_id, binding_state, *, last_error=None):
        lease["binding_state"] = binding_state
        lease["last_error"] = last_error
        return dict(lease)

    async def _update_lease(_run_id, **patch):
        lease.update(patch)
        return dict(lease)

    async def _get_db_client():
        return object()

    async def _promote(_client, *, lease):
        order.append("promote")
        lease_state.update(
            {
                "sandbox_id": "sb-standby",
                "sandbox_type": "desktop",
                "standby_sandbox_id": None,
                "checkpoint_layer_index": 0,
                "checkpoint_subtask_ids": ["task-1"],
            }
        )
        return {
            **lease_state,
            "sandbox_id": "sb-standby",
            "sandbox_type": "desktop",
            "standby_sandbox_id": None,
            "checkpoint_layer_index": 0,
            "checkpoint_subtask_ids": ["task-1"],
        }

    async def _replenish(*, layer_index, subtask_ids):
        order.append(("replenish", layer_index, tuple(subtask_ids)))
        return None

    async def _bump_epoch(_run_id):
        order.append("bump")
        return 3

    async def _delete_results(_run_id, subtask_ids):
        order.append(("delete", tuple(subtask_ids)))
        return 0

    async def _reset_subagents(_run_id, subtask_ids, *, expected_epoch, roles):
        order.append(("reset", expected_epoch, tuple(subtask_ids), dict(roles)))
        return None

    async def _clear_recovery(_run_id):
        order.append("clear")
        return None

    fake_sandbox_module = types.ModuleType("sandbox")
    fake_sandbox_api_module = types.ModuleType("sandbox.api")
    fake_sandbox_api_module.promote_shadow_clone_standby_sandbox = _promote
    fake_sandbox_module.api = fake_sandbox_api_module

    monkeypatch.setitem(sys.modules, "sandbox", fake_sandbox_module)
    monkeypatch.setitem(sys.modules, "sandbox.api", fake_sandbox_api_module)
    monkeypatch.setattr(coordinator_module, "get_run_sandbox_lease", _get_lease)
    monkeypatch.setattr(coordinator_module, "set_run_sandbox_binding_state", _set_binding_state)
    monkeypatch.setattr(coordinator_module, "update_run_sandbox_lease", _update_lease)
    monkeypatch.setattr(coordinator, "_get_db_client", _get_db_client)
    monkeypatch.setattr(
        coordinator,
        "_checkpoint_clone_from_existing_checkpoint",
        _replenish,
    )
    monkeypatch.setattr(coordinator_module, "bump_execution_epoch", _bump_epoch)
    monkeypatch.setattr(coordinator_module, "delete_results", _delete_results)
    monkeypatch.setattr(coordinator_module, "reset_subagents_for_epoch", _reset_subagents)
    monkeypatch.setattr(coordinator_module, "clear_recovery", _clear_recovery)

    next_epoch, error = await coordinator._promote_standby_clone_for_layer(
        layer_index=1,
        layer=[{"id": "task-1", "role": "researcher"}],
    )

    assert next_epoch == 3
    assert error is None
    assert order[:3] == [
        "promote",
        ("replenish", 0, ("task-1",)),
        "bump",
    ]


@pytest.mark.asyncio
async def test_replace_active_sandbox_for_layer_bumps_epoch_without_standby(monkeypatch):
    order = []
    coordinator = coordinator_module.ShadowCloneCoordinator(
        thread_id="thread-replacement",
        project_id="project-replacement",
        agent_run_id="run-replacement",
        model_key="kimi-k2.5",
        db_client=object(),
        mode=ShadowCloneMode.AUTO,
    )

    lease = {
        "run_id": "run-replacement",
        "thread_id": "thread-replacement",
        "project_id": "project-replacement",
        "sandbox_id": "sb-active",
        "sandbox_type": "desktop",
        "sandbox_info": {
            "id": "sb-active",
            "type": "desktop",
            "pass": "secret-1",
            "state": "running",
        },
        "binding_state": "lost",
        "execution_epoch": 0,
        "lease_epoch": 0,
        "environment_status": "failed",
        "environment_ready": False,
        "environment_manifest": {
            "execution_epoch": 0,
            "sandbox": {"id": "sb-active", "type": "desktop"},
        },
        "checkpoint_layer_index": 0,
        "checkpoint_subtask_ids": ["task-1"],
    }

    class _ProjectTable:
        def __init__(self, row):
            self._row = row
            self._filters = {}
            self._update_payload = None

        def select(self, *_args, **_kwargs):
            return self

        def eq(self, key, value):
            self._filters[key] = value
            return self

        def update(self, payload):
            self._update_payload = dict(payload)
            return self

        async def execute(self):
            if self._update_payload is not None:
                if self._filters.get("project_id") == self._row.get("project_id"):
                    self._row.update(self._update_payload)
                    return types.SimpleNamespace(data=[dict(self._row)])
                return types.SimpleNamespace(data=[])

            if "project_id" in self._filters:
                return types.SimpleNamespace(
                    data=[dict(self._row)]
                    if self._filters["project_id"] == self._row.get("project_id")
                    else []
                )

            sandbox_filter = self._filters.get("(sandbox::jsonb ->> 'id')")
            if sandbox_filter is not None:
                sandbox_payload = self._row.get("sandbox")
                if isinstance(sandbox_payload, str):
                    sandbox_payload = json.loads(sandbox_payload)
                sandbox_id = str((sandbox_payload or {}).get("id") or "")
                return types.SimpleNamespace(
                    data=[dict(self._row)] if sandbox_filter == sandbox_id else []
                )

            return types.SimpleNamespace(data=[])

    project_row = {
        "project_id": "project-replacement",
        "account_id": "user-1",
        "sandbox": json.dumps(
            {
                "id": "sb-active",
                "type": "desktop",
                "state": "running",
            }
        ),
    }

    class _FakeClient:
        def table(self, table_name):
            assert table_name == "projects"
            return _ProjectTable(project_row)

    async def _get_lease(_run_id):
        return dict(lease)

    async def _update_lease(_run_id, **patch):
        order.append(("update", dict(patch)))
        lease.update(patch)
        return dict(lease)

    async def _set_binding_state(_run_id, binding_state, *, last_error=None):
        order.append(("binding", binding_state, last_error))
        lease["binding_state"] = binding_state
        lease["last_error"] = last_error
        return dict(lease)

    async def _resume_or_create_sandbox(
        *,
        password,
        project_id,
        sandbox_type,
        sandbox_info,
        reattach_budget_seconds=None,
        allow_create=True,
    ):
        order.append(
            (
                "resume_or_create",
                password,
                project_id,
                sandbox_type,
                dict(sandbox_info),
                allow_create,
            )
        )
        assert allow_create is True
        return types.SimpleNamespace(sandbox_id="sb-recovery"), "created"

    async def _bump_epoch(_run_id):
        order.append("bump")
        return 1

    async def _sync_epoch(_run_id, execution_epoch):
        order.append(("sync", execution_epoch))
        lease["execution_epoch"] = execution_epoch
        lease["lease_epoch"] = execution_epoch
        return dict(lease)

    async def _delete_results(_run_id, subtask_ids):
        order.append(("delete", tuple(subtask_ids)))
        return 0

    async def _reset_subagents(_run_id, subtask_ids, *, expected_epoch, roles):
        order.append(("reset", expected_epoch, tuple(subtask_ids), dict(roles)))
        return None

    async def _clear_recovery(_run_id):
        order.append("clear_recovery")
        return None

    async def _update_environment(*_args, **_kwargs):
        order.append(("environment", dict(_kwargs)))
        return None

    def _clear_session_state(run_id):
        order.append(("clear_session", run_id))

    async def _get_db_client():
        return _FakeClient()

    monkeypatch.setattr(coordinator_module, "get_run_sandbox_lease", _get_lease)
    monkeypatch.setattr(coordinator_module, "update_run_sandbox_lease", _update_lease)
    monkeypatch.setattr(coordinator_module, "set_run_sandbox_binding_state", _set_binding_state)
    monkeypatch.setattr(coordinator_module, "resume_or_create_sandbox", _resume_or_create_sandbox)
    monkeypatch.setattr(coordinator_module, "bump_execution_epoch", _bump_epoch)
    monkeypatch.setattr(coordinator_module, "sync_run_sandbox_execution_epoch", _sync_epoch)
    monkeypatch.setattr(coordinator_module, "delete_results", _delete_results)
    monkeypatch.setattr(coordinator_module, "reset_subagents_for_epoch", _reset_subagents)
    monkeypatch.setattr(coordinator_module, "clear_recovery", _clear_recovery)
    monkeypatch.setattr(coordinator_module, "update_environment", _update_environment)
    monkeypatch.setattr(coordinator_module, "clear_shadow_clone_session_state", _clear_session_state)
    monkeypatch.setattr(coordinator, "_get_db_client", _get_db_client)

    next_epoch, error = await coordinator._replace_active_sandbox_for_layer(
        layer_index=1,
        layer=[{"id": "task-1", "role": "researcher"}],
        recovery_reason="sandbox lost",
    )

    assert error is None
    assert next_epoch == 1
    assert lease["sandbox_id"] == "sb-recovery"
    assert lease["execution_epoch"] == 1
    assert lease["lease_epoch"] == 1
    assert any(
        item[0] == "update"
        and item[1].get("sandbox_id") == "sb-recovery"
        and item[1].get("binding_state") == "locked"
        and item[1].get("standby_sandbox_id") is None
        for item in order
    )
    assert ("clear_session", "run-replacement") in order
    assert ("delete", ("task-1",)) in order
    assert ("reset", 1, ("task-1",), {"task-1": "researcher"}) in order
    project_access = await sandbox_api.verify_sandbox_access(_FakeClient(), "sb-recovery", "user-1")
    assert project_access["project_id"] == "project-replacement"


@pytest.mark.asyncio
async def test_coordinator_ignores_stale_standby_metadata_when_single_active_default(monkeypatch):
    fake_actor = _FakeSubAgentActor()
    replace_calls = []

    monkeypatch.setattr(coordinator_module, "MainAgent", _FakeMainAgent)
    monkeypatch.setattr(coordinator_module, "run_subagent", fake_actor)
    monkeypatch.setattr(
        coordinator_module,
        "topological_sort_layers",
        lambda subtasks, deps: [subtasks],
    )

    async def _confirmed(*args, **kwargs):
        return ConfirmationResult.CONFIRMED

    async def _fake_init_state(*args, **kwargs):
        return None

    async def _fake_transition(*args, **kwargs):
        return True

    async def _fake_update_subagent(*args, **kwargs):
        return None

    async def _fake_cleanup(*args, **kwargs):
        return None

    async def _fake_cleanup_results(*args, **kwargs):
        return 0

    run_id = "run-stale-standby"
    _TEST_LEASES[run_id] = {
        "run_id": run_id,
        "thread_id": "thread-stale-standby",
        "project_id": "project-stale-standby",
        "sandbox_id": "sandbox-primary",
        "sandbox_type": "desktop",
        "binding_state": "locked",
        "execution_epoch": 0,
        "lease_epoch": 0,
        "environment_status": "ready",
        "environment_ready": True,
        "environment_manifest": {"execution_epoch": 0, "sandbox": {"id": "sandbox-primary"}},
        "standby_sandbox_id": "sandbox-stale-standby",
        "standby_sandbox_type": "desktop",
        "standby_sandbox_info": {"id": "sandbox-stale-standby", "type": "desktop"},
        "standby_snapshot_template_id": "snapshot-stale",
        "standby_source_sandbox_id": "sandbox-primary",
        "standby_created_at": "2026-04-01T00:00:00+00:00",
    }

    monkeypatch.setattr(coordinator_module, "wait_for_confirmation", _confirmed)
    monkeypatch.setattr(coordinator_module, "init_state", _fake_init_state)
    monkeypatch.setattr(coordinator_module, "transition", _fake_transition)
    monkeypatch.setattr(coordinator_module, "update_subagent", _fake_update_subagent)
    monkeypatch.setattr(coordinator_module, "cleanup", _fake_cleanup)
    monkeypatch.setattr(coordinator_module, "cleanup_results", _fake_cleanup_results)

    coordinator = coordinator_module.ShadowCloneCoordinator(
        thread_id="thread-stale-standby",
        project_id="project-stale-standby",
        agent_run_id=run_id,
        model_key="kimi-k2.5",
        db_client=object(),
        mode=ShadowCloneMode.AUTO,
    )

    wait_calls = {"count": 0}

    async def _fake_wait_for_layer(layer, **_kwargs):
        wait_calls["count"] += 1
        if wait_calls["count"] == 1:
            yield {
                "type": "shadow_clone_layer_recovery",
                "recovery_kind": "clone",
                "layer_index": 0,
                "message": "replacement required",
            }
            return
        for item in layer:
            yield {"type": "subagent_completed", "subtask_id": item["id"]}

    async def _fake_replace(*, layer_index, layer, recovery_reason):
        replace_calls.append(
            {
                "layer_index": layer_index,
                "layer": [dict(item) for item in layer],
                "recovery_reason": recovery_reason,
                "standby_sandbox_id": _TEST_LEASES[run_id].get("standby_sandbox_id"),
            }
        )
        assert _TEST_LEASES[run_id].get("standby_sandbox_id") is None
        _TEST_LEASES[run_id]["execution_epoch"] = 1
        _TEST_LEASES[run_id]["lease_epoch"] = 1
        _TEST_LEASES[run_id]["binding_state"] = "locked"
        return 1, None

    async def _boom_promote(*args, **kwargs):
        raise AssertionError("stale standby metadata should not route recovery through promotion")

    monkeypatch.setattr(coordinator, "_wait_for_layer_completion", _fake_wait_for_layer)
    monkeypatch.setattr(coordinator, "_replace_active_sandbox_for_layer", _fake_replace)
    monkeypatch.setattr(coordinator, "_promote_standby_clone_for_layer", _boom_promote)

    events = await _collect(
        coordinator.run(user_message="complex", thread_run_id="tr-stale-standby"),
    )

    assert replace_calls
    assert _TEST_LEASES[run_id].get("standby_sandbox_id") is None
    assert events[-1]["type"] == "shadow_clone_complete"


@pytest.mark.asyncio
async def test_coordinator_falls_back_to_replacement_when_standby_promotion_fails(monkeypatch):
    fake_actor = _FakeSubAgentActor()
    replace_calls = []

    monkeypatch.setenv("SHADOW_CLONE_ENABLE_PROACTIVE_STANDBY", "true")
    monkeypatch.setattr(coordinator_module, "MainAgent", _FakeMainAgent)
    monkeypatch.setattr(coordinator_module, "run_subagent", fake_actor)
    monkeypatch.setattr(
        coordinator_module,
        "topological_sort_layers",
        lambda subtasks, deps: [subtasks],
    )

    async def _confirmed(*args, **kwargs):
        return ConfirmationResult.CONFIRMED

    async def _fake_init_state(*args, **kwargs):
        return None

    async def _fake_transition(*args, **kwargs):
        return True

    async def _fake_update_subagent(*args, **kwargs):
        return None

    async def _fake_cleanup(*args, **kwargs):
        return None

    async def _fake_cleanup_results(*args, **kwargs):
        return 0

    run_id = "run-promotion-fallback"
    _TEST_LEASES[run_id] = {
        "run_id": run_id,
        "thread_id": "thread-promotion-fallback",
        "project_id": "project-promotion-fallback",
        "sandbox_id": "sandbox-primary",
        "sandbox_type": "desktop",
        "binding_state": "locked",
        "execution_epoch": 0,
        "lease_epoch": 0,
        "environment_status": "ready",
        "environment_ready": True,
        "environment_manifest": {"execution_epoch": 0, "sandbox": {"id": "sandbox-primary"}},
        "standby_sandbox_id": "sandbox-standby",
        "standby_sandbox_type": "desktop",
        "standby_sandbox_info": {"id": "sandbox-standby", "type": "desktop"},
    }

    monkeypatch.setattr(coordinator_module, "wait_for_confirmation", _confirmed)
    monkeypatch.setattr(coordinator_module, "init_state", _fake_init_state)
    monkeypatch.setattr(coordinator_module, "transition", _fake_transition)
    monkeypatch.setattr(coordinator_module, "update_subagent", _fake_update_subagent)
    monkeypatch.setattr(coordinator_module, "cleanup", _fake_cleanup)
    monkeypatch.setattr(coordinator_module, "cleanup_results", _fake_cleanup_results)

    coordinator = coordinator_module.ShadowCloneCoordinator(
        thread_id="thread-promotion-fallback",
        project_id="project-promotion-fallback",
        agent_run_id=run_id,
        model_key="kimi-k2.5",
        db_client=object(),
        mode=ShadowCloneMode.AUTO,
    )

    wait_calls = {"count": 0}

    async def _fake_wait_for_layer(layer, **_kwargs):
        wait_calls["count"] += 1
        if wait_calls["count"] == 1:
            yield {
                "type": "shadow_clone_layer_recovery",
                "recovery_kind": "clone",
                "layer_index": 0,
                "message": "promotion failed",
            }
            return
        for item in layer:
            yield {"type": "subagent_completed", "subtask_id": item["id"]}

    async def _fake_promote(*, layer_index, layer):
        assert layer_index == 0
        return None, "standby promotion failed"

    async def _fake_replace(*, layer_index, layer, recovery_reason):
        replace_calls.append(recovery_reason)
        _TEST_LEASES[run_id]["standby_sandbox_id"] = None
        _TEST_LEASES[run_id]["execution_epoch"] = 1
        _TEST_LEASES[run_id]["lease_epoch"] = 1
        _TEST_LEASES[run_id]["binding_state"] = "locked"
        return 1, None

    monkeypatch.setattr(coordinator, "_wait_for_layer_completion", _fake_wait_for_layer)
    monkeypatch.setattr(coordinator, "_promote_standby_clone_for_layer", _fake_promote)
    monkeypatch.setattr(coordinator, "_replace_active_sandbox_for_layer", _fake_replace)

    events = await _collect(
        coordinator.run(user_message="complex", thread_run_id="tr-promotion-fallback"),
    )

    assert replace_calls == ["promotion failed"]
    assert events[-1]["type"] == "shadow_clone_complete"


@pytest.mark.asyncio
async def test_coordinator_replans_when_layer_checkpoint_clone_fails(monkeypatch):
    fake_actor = _FakeSubAgentActor()
    replan_calls = []
    visible_result_calls = []

    monkeypatch.setattr(coordinator_module, "MainAgent", _FakeMainAgent)
    monkeypatch.setattr(coordinator_module, "run_subagent", fake_actor)
    monkeypatch.setattr(
        coordinator_module,
        "topological_sort_layers",
        lambda subtasks, deps: [subtasks],
    )

    async def _confirmed(*args, **kwargs):
        return ConfirmationResult.CONFIRMED

    async def _fake_init_state(*args, **kwargs):
        return None

    async def _fake_transition(*args, **kwargs):
        return True

    async def _fake_update_subagent(*args, **kwargs):
        return None

    async def _fake_cleanup(*args, **kwargs):
        return None

    async def _fake_cleanup_results(*args, **kwargs):
        return 0

    async def _fake_ensure_visible_results(_run_id, expected_results, *, timeout=None):
        visible_result_calls.append(
            {
                "run_id": _run_id,
                "expected_results": expected_results,
                "timeout": timeout,
            }
        )
        return []

    async def _fake_request_recovery(*args, **kwargs):
        replan_calls.append(("request_recovery", kwargs))
        return None

    async def _fake_bump_epoch(*args, **kwargs):
        return 1

    async def _fake_delete_results(*args, **kwargs):
        return 0

    monkeypatch.setattr(coordinator_module, "wait_for_confirmation", _confirmed)
    monkeypatch.setattr(coordinator_module, "init_state", _fake_init_state)
    monkeypatch.setattr(coordinator_module, "transition", _fake_transition)
    monkeypatch.setattr(coordinator_module, "update_subagent", _fake_update_subagent)
    monkeypatch.setattr(coordinator_module, "cleanup", _fake_cleanup)
    monkeypatch.setattr(coordinator_module, "cleanup_results", _fake_cleanup_results)
    monkeypatch.setattr(coordinator_module, "request_recovery", _fake_request_recovery)
    monkeypatch.setattr(coordinator_module, "bump_execution_epoch", _fake_bump_epoch)
    monkeypatch.setattr(coordinator_module, "delete_results", _fake_delete_results)
    monkeypatch.setattr(
        coordinator_module,
        "ensure_terminal_result_summaries_visible",
        _fake_ensure_visible_results,
        raising=False,
    )

    coordinator = coordinator_module.ShadowCloneCoordinator(
        thread_id="thread-replan",
        project_id="project-replan",
        agent_run_id="run-replan",
        model_key="kimi-k2.5",
        db_client=object(),
        mode=ShadowCloneMode.AUTO,
    )

    async def _fake_wait_for_layer(layer, timeout=0):
        for item in layer:
            yield {"type": "subagent_completed", "subtask_id": item["id"]}

    async def _fake_checkpoint_completed_layer(*, layer_index, layer):
        return "checkpoint clone unavailable"

    async def _fake_stream_replan(*, layer_index, reason):
        replan_calls.append(("stream_replan", {"layer_index": layer_index, "reason": reason}))
        yield {
            "type": "passthrough_message",
            "msg": Msg(name="assistant", content="fallback", role="assistant"),
            "is_last": True,
            "route_metadata": {
                "shadow_clone_phase": "execution",
                "live_activity": {
                    "scope": "main_agent",
                    "phase": "execution",
                    "reason": "replan_continue",
                    "subtask_id": None,
                    "epoch": 1,
                },
            },
        }

    monkeypatch.setattr(coordinator, "_wait_for_layer_completion", _fake_wait_for_layer)
    monkeypatch.setattr(
        coordinator,
        "_checkpoint_completed_layer",
        _fake_checkpoint_completed_layer,
    )
    monkeypatch.setattr(coordinator, "_stream_main_agent_replan", _fake_stream_replan)

    events = await _collect(
        coordinator.run(user_message="complex", thread_run_id="tr-replan"),
    )

    assert any(event["type"] == "passthrough_message" for event in events)
    assert any(
        event["type"] == "passthrough_message"
        and event["route_metadata"].get("live_activity", {}).get("scope") == "main_agent"
        and event["route_metadata"].get("live_activity", {}).get("phase") == "execution"
        for event in events
    )
    assert events[-1]["type"] == "shadow_clone_complete"
    assert events[-1]["completion_mode"] == "replan_continue"
    assert events[-1]["terminal_reason"] == "replan_continue_complete"
    assert visible_result_calls == [
        {
            "run_id": "run-replan",
            "expected_results": [
                {
                    "subtask_id": "task-1",
                    "role": None,
                    "status": "completed",
                    "summary": None,
                    "attempt_index": None,
                    "failure_class": None,
                }
            ],
            "timeout": None,
        }
    ]
    assert any(
        call[0] == "stream_replan"
        and "checkpoint clone unavailable" in call[1]["reason"]
        for call in replan_calls
    )


@pytest.mark.asyncio
async def test_coordinator_replan_continuation_preserves_completed_results(monkeypatch):
    fake_actor = _FakeSubAgentActor()
    transitions = []
    deleted_results = AsyncMock(return_value=0)
    summaries_seen = []

    monkeypatch.setattr(coordinator_module, "MainAgent", _FakeMainAgent)
    monkeypatch.setattr(coordinator_module, "run_subagent", fake_actor)
    monkeypatch.setattr(
        coordinator_module,
        "topological_sort_layers",
        lambda subtasks, deps: [subtasks],
    )

    async def _confirmed(*args, **kwargs):
        return ConfirmationResult.CONFIRMED

    async def _fake_init_state(*args, **kwargs):
        return None

    async def _fake_transition(*args, **kwargs):
        transitions.append(args[1:3])
        return True

    async def _fake_update_subagent(*args, **kwargs):
        return None

    async def _fake_cleanup(*args, **kwargs):
        return None

    async def _fake_cleanup_results(*args, **kwargs):
        return 0

    async def _fake_request_recovery(*args, **kwargs):
        return None

    async def _fake_bump_epoch(*args, **kwargs):
        return 1

    async def _fake_read_summaries(_run_id):
        assert deleted_results.await_count == 0
        summaries_seen.append(_run_id)
        return [{"subtask_id": "task-1", "summary": "preserved"}]

    monkeypatch.setattr(coordinator_module, "wait_for_confirmation", _confirmed)
    monkeypatch.setattr(coordinator_module, "init_state", _fake_init_state)
    monkeypatch.setattr(coordinator_module, "transition", _fake_transition)
    monkeypatch.setattr(coordinator_module, "update_subagent", _fake_update_subagent)
    monkeypatch.setattr(coordinator_module, "cleanup", _fake_cleanup)
    monkeypatch.setattr(coordinator_module, "cleanup_results", _fake_cleanup_results)
    monkeypatch.setattr(coordinator_module, "request_recovery", _fake_request_recovery)
    monkeypatch.setattr(coordinator_module, "bump_execution_epoch", _fake_bump_epoch)
    monkeypatch.setattr(coordinator_module, "delete_results", deleted_results)
    monkeypatch.setattr(coordinator_module, "read_summaries", _fake_read_summaries)

    coordinator = coordinator_module.ShadowCloneCoordinator(
        thread_id="thread-replan-keep",
        project_id="project-replan-keep",
        agent_run_id="run-replan-keep",
        model_key="kimi-k2.5",
        db_client=object(),
        mode=ShadowCloneMode.AUTO,
    )

    async def _fake_wait_for_layer(layer, timeout=0):
        for item in layer:
            yield {"type": "subagent_completed", "subtask_id": item["id"]}

    async def _fake_checkpoint_completed_layer(*, layer_index, layer):
        return "checkpoint clone unavailable"

    monkeypatch.setattr(coordinator, "_wait_for_layer_completion", _fake_wait_for_layer)
    monkeypatch.setattr(
        coordinator,
        "_checkpoint_completed_layer",
        _fake_checkpoint_completed_layer,
    )

    events = await _collect(
        coordinator.run(user_message="complex", thread_run_id="tr-replan-keep"),
    )

    assert any(
        event["type"] == "passthrough_message"
        and event["msg"].content == "recovered directly"
        for event in events
    )
    assert deleted_results.await_count == 0
    assert summaries_seen == ["run-replan-keep"]
    assert ("running", "completed") in transitions


@pytest.mark.asyncio
async def test_shadow_clone_state_cleanup_retains_terminal_snapshot(monkeypatch):
    run_id = "run-terminal"
    state_key = state_machine_module.STATE_KEY_TEMPLATE.format(run_id=run_id)
    raw_state = json.dumps(
        {
            "status": "completed",
            "updated_at": "2026-04-01T00:00:00+00:00",
            "live_activity": {"phase": "completed"},
        }
    )
    store = {state_key: raw_state}
    expire_calls = []

    async def _get(key: str, **_kwargs):
        return store.get(key)

    async def _expire(key: str, seconds: int, **_kwargs):
        expire_calls.append((key, seconds))
        return 1 if key in store else 0

    async def _delete(*_args, **_kwargs):
        raise AssertionError("cleanup should retain terminal state")

    monkeypatch.setattr(state_machine_module.redis_service, "get", _get)
    monkeypatch.setattr(state_machine_module.redis_service, "expire", _expire)
    monkeypatch.setattr(state_machine_module.redis_service, "delete", _delete)

    before = await state_machine_module.get_state(run_id)
    await state_machine_module.cleanup(run_id)
    after = await state_machine_module.get_state(run_id)

    assert after == before
    assert expire_calls == [(state_key, state_machine_module.STATE_TTL_SECONDS)]


@pytest.mark.asyncio
async def test_coordinator_direct_execution_without_proposal_marks_completed(monkeypatch):
    transitions = []

    monkeypatch.setattr(
        coordinator_module,
        "MainAgent",
        lambda **kwargs: _FakeMainAgent(**kwargs, decompose_result=None),
    )

    async def _fake_init_state(*args, **kwargs):
        return None

    async def _fake_transition(*args, **kwargs):
        transitions.append(args[1:3])
        return True

    async def _fake_cleanup(*args, **kwargs):
        return None

    async def _fake_cleanup_results(*args, **kwargs):
        return 0

    monkeypatch.setattr(coordinator_module, "init_state", _fake_init_state)
    monkeypatch.setattr(coordinator_module, "transition", _fake_transition)
    monkeypatch.setattr(coordinator_module, "cleanup", _fake_cleanup)
    monkeypatch.setattr(coordinator_module, "cleanup_results", _fake_cleanup_results)

    coordinator = coordinator_module.ShadowCloneCoordinator(
        thread_id="thread-direct",
        project_id="project-direct",
        agent_run_id="run-direct",
        model_key="kimi-k2.5",
        db_client=object(),
        mode=ShadowCloneMode.AUTO,
    )

    events = await _collect(
        coordinator.run(user_message="simple", thread_run_id="tr-direct"),
    )

    assert ("pending", "completed") in transitions
    assert events[-1]["type"] == "shadow_clone_complete"


@pytest.mark.asyncio
async def test_coordinator_on_mode_without_proposal_fails_loudly(monkeypatch):
    transitions = []
    environment_updates = []
    cleanup_speculative_calls = []

    monkeypatch.setattr(
        coordinator_module,
        "MainAgent",
        lambda **kwargs: _FakeMainAgent(**kwargs, decompose_result=None),
    )

    async def _fake_init_state(*args, **kwargs):
        return None

    async def _fake_transition(*args, **kwargs):
        transitions.append(args[1:3])
        return True

    async def _fake_update_environment(*_args, **kwargs):
        environment_updates.append(kwargs)
        return {"environment": kwargs}

    async def _fake_cleanup(*args, **kwargs):
        return None

    async def _fake_cleanup_results(*args, **kwargs):
        return 0

    async def _fake_cleanup_speculative(self, **kwargs):
        cleanup_speculative_calls.append(kwargs)
        return None

    monkeypatch.setattr(coordinator_module, "init_state", _fake_init_state)
    monkeypatch.setattr(coordinator_module, "transition", _fake_transition)
    monkeypatch.setattr(coordinator_module, "update_environment", _fake_update_environment)
    monkeypatch.setattr(coordinator_module, "cleanup", _fake_cleanup)
    monkeypatch.setattr(coordinator_module, "cleanup_results", _fake_cleanup_results)
    monkeypatch.setattr(
        coordinator_module.ShadowCloneCoordinator,
        "_cleanup_speculative_startup",
        _fake_cleanup_speculative,
    )

    coordinator = coordinator_module.ShadowCloneCoordinator(
        thread_id="thread-direct-on",
        project_id="project-direct-on",
        agent_run_id="run-direct-on",
        model_key="kimi-k2.5",
        db_client=object(),
        mode=ShadowCloneMode.ON,
    )

    events = await _collect(
        coordinator.run(user_message="simple", thread_run_id="tr-direct-on"),
    )

    failed_event = next(
        event
        for event in events
        if event["type"] == "status" and event.get("status") == "failed"
    )
    assert failed_event["status"] == "failed"
    assert "requires a proposal" in failed_event["message"]
    assert ("pending", "failed") in transitions
    assert ("pending", "completed") not in transitions
    assert environment_updates[-1]["status"] == "failed"
    assert environment_updates[-1]["manifest"]["proposal_required"] is True
    assert environment_updates[-1]["manifest"]["proposal_missing"] is True
    assert cleanup_speculative_calls == [
        {
            "release_lease": True,
            "clear_standby": True,
            "last_error": (
                "Shadow Clone mode 'on' requires a proposal via spawn_subagents, "
                "but the planning turn completed without producing one."
            ),
        }
    ]
