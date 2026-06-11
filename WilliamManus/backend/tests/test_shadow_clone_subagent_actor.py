import asyncio
import json
import sys
import types

import pytest

if "services" not in sys.modules:
    sys.modules["services"] = types.ModuleType("services")

if "services.redis" not in sys.modules:
    services_redis_stub = types.SimpleNamespace(
        publish=lambda *args, **kwargs: None,
        REDIS_KEY_TTL=3600,
    )
    sys.modules["services.redis"] = services_redis_stub
    setattr(sys.modules["services"], "redis", services_redis_stub)

if "services.postgresql" not in sys.modules:
    postgresql_mod = types.ModuleType("services.postgresql")

    class _DummyDBConnection:
        @property
        async def client(self):
            return object()

    postgresql_mod.DBConnection = _DummyDBConnection
    sys.modules["services.postgresql"] = postgresql_mod
    setattr(sys.modules["services"], "postgresql", postgresql_mod)

import agentscope_integration.shadow_clone.subagent_actor as subagent_actor
from utils import dramatiq_queue_names as dramatiq_queue_names_module


class _FakeMsg:
    def __init__(self, text):
        self._text = text
        self.content = text

    def get_text_content(self):
        return self._text


class _FakeDB:
    async def initialize(self):
        return None

    @property
    async def client(self):
        return object()


async def _invoke_actor(*args, **kwargs):
    target = subagent_actor.run_subagent
    if hasattr(target, "fn"):
        return await target.fn(*args, **kwargs)
    return await target(*args, **kwargs)


@pytest.fixture(autouse=True)
def _stub_runtime_state(monkeypatch):
    async def _fake_get_state(_run_id):
        return {"execution_epoch": 0}

    async def _fake_get_lease(_run_id):
        return {"binding_state": "locked"}

    monkeypatch.setattr(subagent_actor, "get_state", _fake_get_state)
    monkeypatch.setattr(subagent_actor, "get_run_sandbox_lease", _fake_get_lease)


@pytest.mark.asyncio
async def test_subagent_actor_happy_path(monkeypatch):
    claims = []
    finalizes = []
    submits = []
    publishes = []

    async def _fake_claim(*args, **kwargs):
        claims.append((args, kwargs))
        return True

    async def _fake_finalize(*args, **kwargs):
        finalizes.append((args, kwargs))
        return True

    async def _fake_submit(*args, **kwargs):
        submits.append((args, kwargs))
        return {"status": "submitted"}

    async def _fake_publish(parent_run_id, subtask_id, status, **kwargs):
        publishes.append((parent_run_id, subtask_id, status, kwargs))

    async def _fake_reconcile(**kwargs):
        return [], kwargs.get("cursor", 0)

    class _FakeAgent:
        def __init__(self):
            self._shadow_peer_note_cursor = 0
            self._shadow_seen_peer_note_ids = set()

        async def __call__(self, _msg):
            return _FakeMsg("done")

    async def _fake_create_subagent(**kwargs):
        return _FakeAgent()

    monkeypatch.setattr(subagent_actor, "db", _FakeDB())
    monkeypatch.setattr(subagent_actor, "claim_subagent_for_epoch", _fake_claim)
    monkeypatch.setattr(subagent_actor, "finalize_subagent_for_epoch", _fake_finalize)
    monkeypatch.setattr(subagent_actor, "submit_result", _fake_submit)
    monkeypatch.setattr(subagent_actor, "_publish_subagent_update", _fake_publish)
    monkeypatch.setattr(subagent_actor, "create_subagent", _fake_create_subagent)
    monkeypatch.setattr(subagent_actor, "reconcile_late_notes", _fake_reconcile)

    await _invoke_actor(
        parent_run_id="run-1",
        subtask_id="task-1",
        subtask_config={"role": "researcher", "task_description": "do it"},
        project_id="proj-1",
        thread_id="thread-1",
    )

    assert claims[0][0][1] == "task-1"
    assert finalizes[-1][0][2] == "completed"
    assert submits[-1][1]["status"] == "completed"
    assert "full_result" in submits[-1][1]
    assert "agent_run_id" not in submits[-1][1]
    assert publishes[-1][0:3] == ("run-1", "task-1", "completed")
    assert publishes[-1][3]["result_summary"] == "done"


def test_subagent_actor_uses_configured_queue_name():
    assert (
        subagent_actor.run_subagent.options["queue_name"]
        == dramatiq_queue_names_module.SHADOW_CLONE_SUBAGENT_QUEUE
    )


@pytest.mark.asyncio
async def test_subagent_actor_persists_provisional_running_result_on_tool_progress(monkeypatch):
    finalizes = []
    submits = []

    async def _fake_claim(*args, **kwargs):
        return True

    async def _fake_finalize(*args, **kwargs):
        finalizes.append((args, kwargs))
        return True

    async def _fake_submit(*args, **kwargs):
        submits.append((args, kwargs))
        return {"status": "submitted"}

    async def _fake_publish(*args, **kwargs):
        return None

    async def _fake_reconcile(**kwargs):
        return [], kwargs.get("cursor", 0)

    class _FakeAgent:
        def __init__(self):
            self._shadow_peer_note_cursor = 0
            self._shadow_seen_peer_note_ids = set()

        async def __call__(self, _msg):
            return _FakeMsg("done")

    async def _fake_create_subagent(**kwargs):
        return _FakeAgent()

    async def _fake_execute_with_activity(**kwargs):
        on_useful_progress = kwargs.get("on_useful_progress")
        assert callable(on_useful_progress)
        await on_useful_progress(
            {
                "message_type": "assistant",
                "content": {
                    "tool_calls": [
                        {
                            "function": {
                                "name": "write_file",
                            }
                        }
                    ]
                },
                "metadata": {"stream_status": "tool_call_chunk"},
            }
        )
        return _FakeMsg("done")

    monkeypatch.setattr(subagent_actor, "db", _FakeDB())
    monkeypatch.setattr(subagent_actor, "claim_subagent_for_epoch", _fake_claim)
    monkeypatch.setattr(subagent_actor, "finalize_subagent_for_epoch", _fake_finalize)
    monkeypatch.setattr(subagent_actor, "submit_result", _fake_submit)
    monkeypatch.setattr(subagent_actor, "_publish_subagent_update", _fake_publish)
    monkeypatch.setattr(subagent_actor, "create_subagent", _fake_create_subagent)
    monkeypatch.setattr(subagent_actor, "_execute_subagent_with_activity", _fake_execute_with_activity)
    monkeypatch.setattr(subagent_actor, "reconcile_late_notes", _fake_reconcile)

    await _invoke_actor(
        parent_run_id="run-progress",
        subtask_id="task-progress",
        subtask_config={"role": "researcher", "task_description": "do it"},
        project_id="proj-1",
        thread_id="thread-1",
    )

    assert [call[1]["status"] for call in submits] == ["running", "completed"]
    assert submits[0][1]["summary_override"] == "Subagent dispatched tool call: write_file"
    assert submits[0][1]["full_result"] == "Subagent dispatched tool call: write_file"
    assert submits[-1][1]["full_result"] == "done"
    assert finalizes[-1][0][2] == "completed"


@pytest.mark.asyncio
async def test_subagent_actor_skips_final_updates_after_provisional_result_if_invalidated(
    monkeypatch,
):
    finalizes = []
    submits = []
    publishes = []

    async def _fake_claim(*args, **kwargs):
        return True

    async def _fake_finalize(*args, **kwargs):
        finalizes.append((args, kwargs))
        return True

    async def _fake_submit(*args, **kwargs):
        submits.append((args, kwargs))
        return {"status": "submitted"}

    async def _fake_publish(*args, **kwargs):
        publishes.append((args, kwargs))
        return None

    async def _fake_reconcile(**kwargs):
        raise AssertionError("late-note reconciliation should be skipped after invalidation")

    async def _fake_get_state(_run_id):
        return {"status": "running", "execution_epoch": 0}

    async def _fake_get_lease(_run_id):
        return {"binding_state": "released"}

    class _FakeAgent:
        def __init__(self):
            self._shadow_peer_note_cursor = 0
            self._shadow_seen_peer_note_ids = set()

        async def __call__(self, _msg):
            return _FakeMsg("done")

    async def _fake_create_subagent(**kwargs):
        return _FakeAgent()

    async def _fake_execute_with_activity(**kwargs):
        on_useful_progress = kwargs.get("on_useful_progress")
        assert callable(on_useful_progress)
        await on_useful_progress(
            {
                "message_type": "assistant",
                "content": {
                    "tool_calls": [
                        {
                            "function": {
                                "name": "write_file",
                            }
                        }
                    ]
                },
                "metadata": {"stream_status": "tool_call_chunk"},
            }
        )
        return _FakeMsg("done")

    monkeypatch.setattr(subagent_actor, "db", _FakeDB())
    monkeypatch.setattr(subagent_actor, "claim_subagent_for_epoch", _fake_claim)
    monkeypatch.setattr(subagent_actor, "finalize_subagent_for_epoch", _fake_finalize)
    monkeypatch.setattr(subagent_actor, "submit_result", _fake_submit)
    monkeypatch.setattr(subagent_actor, "_publish_subagent_update", _fake_publish)
    monkeypatch.setattr(subagent_actor, "create_subagent", _fake_create_subagent)
    monkeypatch.setattr(subagent_actor, "_execute_subagent_with_activity", _fake_execute_with_activity)
    monkeypatch.setattr(subagent_actor, "reconcile_late_notes", _fake_reconcile)
    monkeypatch.setattr(subagent_actor, "get_state", _fake_get_state)
    monkeypatch.setattr(subagent_actor, "get_run_sandbox_lease", _fake_get_lease)

    await _invoke_actor(
        parent_run_id="run-progress-invalidated",
        subtask_id="task-progress-invalidated",
        subtask_config={"role": "researcher", "task_description": "do it"},
        project_id="proj-1",
        thread_id="thread-1",
    )

    assert [call[1]["status"] for call in submits] == ["running"]
    assert submits[0][1]["full_result"] == "Subagent dispatched tool call: write_file"
    assert finalizes == []
    assert publishes == []


@pytest.mark.asyncio
async def test_subagent_actor_marks_failed_on_error(monkeypatch):
    claims = []
    finalizes = []
    submits = []

    async def _fake_claim(*args, **kwargs):
        claims.append((args, kwargs))
        return True

    async def _fake_finalize(*args, **kwargs):
        finalizes.append((args, kwargs))
        return True

    async def _fake_submit(*args, **kwargs):
        submits.append((args, kwargs))
        return {"status": "submitted"}

    async def _fake_publish(parent_run_id, subtask_id, status, **kwargs):
        return None

    async def _fake_reconcile(**kwargs):
        return [], kwargs.get("cursor", 0)

    async def _failing_create_subagent(**kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(subagent_actor, "db", _FakeDB())
    monkeypatch.setattr(subagent_actor, "claim_subagent_for_epoch", _fake_claim)
    monkeypatch.setattr(subagent_actor, "finalize_subagent_for_epoch", _fake_finalize)
    monkeypatch.setattr(subagent_actor, "submit_result", _fake_submit)
    monkeypatch.setattr(subagent_actor, "_publish_subagent_update", _fake_publish)
    monkeypatch.setattr(subagent_actor, "create_subagent", _failing_create_subagent)
    monkeypatch.setattr(subagent_actor, "reconcile_late_notes", _fake_reconcile)

    await _invoke_actor(
        parent_run_id="run-2",
        subtask_id="task-2",
        subtask_config={"role": "analyst", "task_description": "do it"},
        project_id="proj-1",
        thread_id="thread-1",
    )

    assert claims[0][0][1] == "task-2"
    assert finalizes[-1][0][2] == "failed"
    assert submits[-1][1]["status"] == "failed"
    assert "full_result" in submits[-1][1]


@pytest.mark.asyncio
async def test_subagent_actor_claim_failure_aborts_execution(monkeypatch):
    create_calls = []
    submit_calls = []

    async def _failing_claim(*args, **kwargs):
        raise RuntimeError("claim unavailable")

    async def _fake_submit(*args, **kwargs):
        submit_calls.append((args, kwargs))
        return {"status": "submitted"}

    async def _fake_reconcile(**kwargs):
        return [], kwargs.get("cursor", 0)

    class _FakeAgent:
        async def __call__(self, _msg):
            return _FakeMsg("done")

    async def _fake_create_subagent(**kwargs):
        create_calls.append(kwargs)
        return _FakeAgent()

    monkeypatch.setattr(subagent_actor, "db", _FakeDB())
    monkeypatch.setattr(subagent_actor, "SHADOW_CLONE_REDIS_WRITE_RETRY_ATTEMPTS", 0)
    monkeypatch.setattr(subagent_actor, "claim_subagent_for_epoch", _failing_claim)
    monkeypatch.setattr(subagent_actor, "submit_result", _fake_submit)
    monkeypatch.setattr(subagent_actor, "create_subagent", _fake_create_subagent)
    monkeypatch.setattr(subagent_actor, "reconcile_late_notes", _fake_reconcile)

    await _invoke_actor(
        parent_run_id="run-3",
        subtask_id="task-3",
        subtask_config={"role": "researcher", "task_description": "do it"},
        project_id="proj-1",
        thread_id="thread-1",
    )

    assert create_calls == []
    assert submit_calls == []


@pytest.mark.asyncio
async def test_subagent_actor_publish_failure_is_non_fatal(monkeypatch):
    claims = []
    finalizes = []
    submits = []

    async def _fake_claim(*args, **kwargs):
        claims.append((args, kwargs))
        return True

    async def _fake_finalize(*args, **kwargs):
        finalizes.append((args, kwargs))
        return True

    async def _fake_submit(*args, **kwargs):
        submits.append((args, kwargs))
        return {"status": "submitted"}

    async def _failing_publish(*args, **kwargs):
        raise RuntimeError("pubsub unavailable")

    async def _fake_reconcile(**kwargs):
        return [], kwargs.get("cursor", 0)

    class _FakeAgent:
        def __init__(self):
            self._shadow_peer_note_cursor = 0
            self._shadow_seen_peer_note_ids = set()

        async def __call__(self, _msg):
            return _FakeMsg("done")

    async def _fake_create_subagent(**kwargs):
        return _FakeAgent()

    monkeypatch.setattr(subagent_actor, "db", _FakeDB())
    monkeypatch.setattr(subagent_actor, "claim_subagent_for_epoch", _fake_claim)
    monkeypatch.setattr(subagent_actor, "finalize_subagent_for_epoch", _fake_finalize)
    monkeypatch.setattr(subagent_actor, "submit_result", _fake_submit)
    monkeypatch.setattr(subagent_actor, "_publish_subagent_update", _failing_publish)
    monkeypatch.setattr(subagent_actor, "create_subagent", _fake_create_subagent)
    monkeypatch.setattr(subagent_actor, "reconcile_late_notes", _fake_reconcile)

    await _invoke_actor(
        parent_run_id="run-4",
        subtask_id="task-4",
        subtask_config={"role": "builder", "task_description": "do it"},
        project_id="proj-1",
        thread_id="thread-1",
    )

    assert submits[-1][1]["status"] == "completed"
    assert claims[0][0][1] == "task-4"
    assert finalizes[-1][0][2] == "completed"


@pytest.mark.asyncio
async def test_subagent_actor_appends_late_peer_notes_before_persist(monkeypatch):
    claims = []
    finalizes = []
    submits = []

    async def _fake_claim(*args, **kwargs):
        claims.append((args, kwargs))
        return True

    async def _fake_finalize(*args, **kwargs):
        finalizes.append((args, kwargs))
        return True

    async def _fake_submit(*args, **kwargs):
        submits.append((args, kwargs))
        return {"status": "submitted"}

    async def _fake_publish(parent_run_id, subtask_id, status, **kwargs):
        return None

    async def _fake_reconcile(**kwargs):
        return (
            [
                {
                    "note_id": "note-1",
                    "sender_subtask_id": "task-9",
                    "sender_role": "Cross Checker",
                    "recipient_subtask_id": "task-1",
                    "summary": "Late signal",
                    "details": "Verify the concentration risk.",
                },
            ],
            1,
        )

    class _FakeAgent:
        def __init__(self):
            self._shadow_peer_note_cursor = 0
            self._shadow_seen_peer_note_ids = set()

        async def __call__(self, _msg):
            return _FakeMsg("done")

    async def _fake_create_subagent(**kwargs):
        return _FakeAgent()

    monkeypatch.setattr(subagent_actor, "db", _FakeDB())
    monkeypatch.setattr(subagent_actor, "claim_subagent_for_epoch", _fake_claim)
    monkeypatch.setattr(subagent_actor, "finalize_subagent_for_epoch", _fake_finalize)
    monkeypatch.setattr(subagent_actor, "submit_result", _fake_submit)
    monkeypatch.setattr(subagent_actor, "_publish_subagent_update", _fake_publish)
    monkeypatch.setattr(subagent_actor, "create_subagent", _fake_create_subagent)
    monkeypatch.setattr(subagent_actor, "reconcile_late_notes", _fake_reconcile)

    await _invoke_actor(
        parent_run_id="run-5",
        subtask_id="task-1",
        subtask_config={"role": "researcher", "task_description": "do it"},
        project_id="proj-1",
        thread_id="thread-1",
    )

    submit_kwargs = submits[-1][1]
    assert submit_kwargs["status"] == "completed"
    assert submit_kwargs["summary_override"] == "done"
    assert submit_kwargs["late_peer_notes_count"] == 1
    assert "[Late Peer Notes]" in submit_kwargs["full_result"]
    assert "Late signal" in submit_kwargs["full_result"]
    assert claims[0][0][1] == "task-1"
    assert finalizes[-1][0][2] == "completed"


@pytest.mark.asyncio
async def test_subagent_actor_closes_readonly_ltm_sidecar(monkeypatch):
    class _FakeReadonlyLTM:
        def __init__(self):
            self.exited = False

        async def __aexit__(self, *_args):
            self.exited = True

    readonly_ltm = _FakeReadonlyLTM()

    async def _fake_claim(*args, **kwargs):
        return True

    async def _fake_finalize(*args, **kwargs):
        return True

    async def _fake_submit(*args, **kwargs):
        return {"status": "submitted"}

    async def _fake_publish(parent_run_id, subtask_id, status):
        return None

    async def _fake_reconcile(**kwargs):
        return [], kwargs.get("cursor", 0)

    class _FakeAgent:
        def __init__(self):
            self._shadow_peer_note_cursor = 0
            self._shadow_seen_peer_note_ids = set()
            self._shadow_readonly_long_term_memory = readonly_ltm

        async def __call__(self, _msg):
            return _FakeMsg("done")

    async def _fake_create_subagent(**kwargs):
        return _FakeAgent()

    monkeypatch.setattr(subagent_actor, "db", _FakeDB())
    monkeypatch.setattr(subagent_actor, "claim_subagent_for_epoch", _fake_claim)
    monkeypatch.setattr(subagent_actor, "finalize_subagent_for_epoch", _fake_finalize)
    monkeypatch.setattr(subagent_actor, "submit_result", _fake_submit)
    monkeypatch.setattr(subagent_actor, "_publish_subagent_update", _fake_publish)
    monkeypatch.setattr(subagent_actor, "create_subagent", _fake_create_subagent)
    monkeypatch.setattr(subagent_actor, "reconcile_late_notes", _fake_reconcile)

    await _invoke_actor(
        parent_run_id="run-6",
        subtask_id="task-6",
        subtask_config={"role": "researcher", "task_description": "do it"},
        project_id="proj-1",
        thread_id="thread-1",
    )

    assert readonly_ltm.exited is True


@pytest.mark.asyncio
async def test_subagent_actor_aborts_when_running_claim_is_rejected(monkeypatch):
    claims = []
    create_calls = []
    submit_calls = []

    async def _fake_claim(*args, **kwargs):
        claims.append((args, kwargs))
        return False

    async def _fake_submit(*args, **kwargs):
        submit_calls.append((args, kwargs))
        return {"status": "submitted"}

    async def _fake_create_subagent(**kwargs):
        create_calls.append(kwargs)
        return _FakeMsg("should not run")

    async def _fake_get_state(_run_id):
        return {"execution_epoch": 0}

    monkeypatch.setattr(subagent_actor, "db", _FakeDB())
    monkeypatch.setattr(subagent_actor, "claim_subagent_for_epoch", _fake_claim)
    monkeypatch.setattr(subagent_actor, "submit_result", _fake_submit)
    monkeypatch.setattr(subagent_actor, "create_subagent", _fake_create_subagent)
    monkeypatch.setattr(subagent_actor, "get_state", _fake_get_state)

    await _invoke_actor(
        parent_run_id="run-claim",
        subtask_id="task-claim",
        subtask_config={"role": "researcher", "task_description": "do it"},
        project_id="proj-1",
        thread_id="thread-1",
    )

    assert claims[0][0][1] == "task-claim"
    assert create_calls == []
    assert submit_calls == []


@pytest.mark.asyncio
async def test_subagent_actor_passes_subagent_span_to_create_subagent(monkeypatch):
    captured = {}

    class _FakeLangfuseSpan:
        def end(self, **_kwargs):
            return None

    class _FakeLangfuseTrace:
        def span(self, **kwargs):
            captured["created_span_kwargs"] = kwargs
            span = _FakeLangfuseSpan()
            captured["created_span"] = span
            return span

    class _FakeLangfuseClient:
        def trace(self, id=None):
            captured["trace_id"] = id
            return _FakeLangfuseTrace()

    services_langfuse_mod = types.ModuleType("services.langfuse")
    services_langfuse_mod.langfuse = _FakeLangfuseClient()
    sys.modules["services.langfuse"] = services_langfuse_mod
    setattr(sys.modules["services"], "langfuse", services_langfuse_mod)

    async def _fake_claim(*args, **kwargs):
        return True

    async def _fake_finalize(*args, **kwargs):
        return True

    async def _fake_submit(*args, **kwargs):
        return {"status": "submitted"}

    async def _fake_publish(*args, **kwargs):
        return None

    async def _fake_reconcile(**kwargs):
        return [], kwargs.get("cursor", 0)

    class _FakeAgent:
        def __init__(self):
            self._shadow_peer_note_cursor = 0
            self._shadow_seen_peer_note_ids = set()

        async def __call__(self, _msg):
            return _FakeMsg("done")

    async def _fake_create_subagent(**kwargs):
        captured["create_subagent_kwargs"] = kwargs
        return _FakeAgent()

    monkeypatch.setattr(subagent_actor, "db", _FakeDB())
    monkeypatch.setattr(subagent_actor, "claim_subagent_for_epoch", _fake_claim)
    monkeypatch.setattr(subagent_actor, "finalize_subagent_for_epoch", _fake_finalize)
    monkeypatch.setattr(subagent_actor, "submit_result", _fake_submit)
    monkeypatch.setattr(subagent_actor, "_publish_subagent_update", _fake_publish)
    monkeypatch.setattr(subagent_actor, "create_subagent", _fake_create_subagent)
    monkeypatch.setattr(subagent_actor, "reconcile_late_notes", _fake_reconcile)

    await _invoke_actor(
        parent_run_id="run-span-parent",
        subtask_id="task-span-parent",
        subtask_config={"role": "researcher", "task_description": "do it"},
        project_id="proj-1",
        thread_id="thread-1",
        langfuse_trace_id="lf-trace-parent",
    )

    assert captured["trace_id"] == "lf-trace-parent"
    assert captured["create_subagent_kwargs"]["langfuse_trace_id"] == "lf-trace-parent"
    assert (
        captured["create_subagent_kwargs"]["langfuse_parent_observation"]
        is captured["created_span"]
    )


@pytest.mark.asyncio
async def test_subagent_actor_ends_span_on_execution_invalidation_early_return(monkeypatch):
    captured = {"span_end_calls": []}

    class _FakeLangfuseSpan:
        def end(self, **kwargs):
            captured["span_end_calls"].append(kwargs)

    class _FakeLangfuseTrace:
        def span(self, **_kwargs):
            span = _FakeLangfuseSpan()
            captured["created_span"] = span
            return span

    class _FakeLangfuseClient:
        def trace(self, id=None):
            captured["trace_id"] = id
            return _FakeLangfuseTrace()

    services_langfuse_mod = types.ModuleType("services.langfuse")
    services_langfuse_mod.langfuse = _FakeLangfuseClient()
    sys.modules["services.langfuse"] = services_langfuse_mod
    setattr(sys.modules["services"], "langfuse", services_langfuse_mod)

    async def _fake_claim(*args, **kwargs):
        return True

    async def _fake_submit(*args, **kwargs):
        raise AssertionError("submit_result should not run after invalidation")

    async def _fake_finalize(*args, **kwargs):
        raise AssertionError("finalize_subagent_for_epoch should not run after invalidation")

    async def _fake_publish(*args, **kwargs):
        raise AssertionError("publish should not run after invalidation")

    async def _fake_reconcile(**kwargs):
        raise AssertionError("late-note reconciliation should not run after invalidation")

    class _FakeAgent:
        def __init__(self):
            self._shadow_peer_note_cursor = 0
            self._shadow_seen_peer_note_ids = set()

        async def __call__(self, _msg):
            return _FakeMsg("done")

    async def _fake_create_subagent(**kwargs):
        return _FakeAgent()

    async def _fake_execute_with_activity(**kwargs):
        raise subagent_actor.ShadowCloneExecutionInvalidatedError("lease invalidated")

    monkeypatch.setattr(subagent_actor, "db", _FakeDB())
    monkeypatch.setattr(subagent_actor, "claim_subagent_for_epoch", _fake_claim)
    monkeypatch.setattr(subagent_actor, "submit_result", _fake_submit)
    monkeypatch.setattr(subagent_actor, "finalize_subagent_for_epoch", _fake_finalize)
    monkeypatch.setattr(subagent_actor, "_publish_subagent_update", _fake_publish)
    monkeypatch.setattr(subagent_actor, "create_subagent", _fake_create_subagent)
    monkeypatch.setattr(subagent_actor, "reconcile_late_notes", _fake_reconcile)
    monkeypatch.setattr(subagent_actor, "_execute_subagent_with_activity", _fake_execute_with_activity)

    await _invoke_actor(
        parent_run_id="run-invalidation",
        subtask_id="task-invalidation",
        subtask_config={"role": "researcher", "task_description": "do it"},
        project_id="proj-1",
        thread_id="thread-1",
        langfuse_trace_id="lf-trace-invalidation",
    )

    assert captured["trace_id"] == "lf-trace-invalidation"
    assert len(captured["span_end_calls"]) == 1


@pytest.mark.asyncio
async def test_subagent_actor_aborts_when_result_state_disappears(monkeypatch):
    claims = []
    submit_calls = []

    async def _fake_claim(*args, **kwargs):
        claims.append((args, kwargs))
        return True

    async def _fake_submit(*args, **kwargs):
        submit_calls.append((args, kwargs))
        raise subagent_actor.ShadowCloneResultStateUnavailableError("state missing")

    async def _fake_publish(parent_run_id, subtask_id, status):
        raise AssertionError("publish should not be called when state is missing")

    async def _fake_reconcile(**kwargs):
        return [], kwargs.get("cursor", 0)

    async def _fake_get_state(_run_id):
        return {"execution_epoch": 0}

    class _FakeAgent:
        def __init__(self):
            self._shadow_peer_note_cursor = 0
            self._shadow_seen_peer_note_ids = set()

        async def __call__(self, _msg):
            return _FakeMsg("done")

    async def _fake_create_subagent(**kwargs):
        return _FakeAgent()

    monkeypatch.setattr(subagent_actor, "db", _FakeDB())
    monkeypatch.setattr(subagent_actor, "claim_subagent_for_epoch", _fake_claim)
    monkeypatch.setattr(subagent_actor, "submit_result", _fake_submit)
    monkeypatch.setattr(subagent_actor, "_publish_subagent_update", _fake_publish)
    monkeypatch.setattr(subagent_actor, "create_subagent", _fake_create_subagent)
    monkeypatch.setattr(subagent_actor, "reconcile_late_notes", _fake_reconcile)
    monkeypatch.setattr(subagent_actor, "get_state", _fake_get_state)

    await _invoke_actor(
        parent_run_id="run-state-missing",
        subtask_id="task-state-missing",
        subtask_config={"role": "researcher", "task_description": "do it"},
        project_id="proj-1",
        thread_id="thread-1",
    )

    assert claims[0][0][1] == "task-state-missing"
    assert len(submit_calls) == 1


@pytest.mark.asyncio
async def test_subagent_actor_aborts_when_lease_is_released(monkeypatch):
    async def _fake_claim(*args, **kwargs):
        return True

    async def _fake_submit(*args, **kwargs):
        raise AssertionError("submit should not be called after released lease")

    async def _fake_reconcile(**kwargs):
        return [], kwargs.get("cursor", 0)

    async def _fake_get_state(_run_id):
        return {"execution_epoch": 0}

    async def _fake_get_lease(_run_id):
        return {"binding_state": "released"}

    class _FakeAgent:
        def __init__(self):
            self._shadow_peer_note_cursor = 0
            self._shadow_seen_peer_note_ids = set()

        async def __call__(self, _msg):
            return _FakeMsg("done")

    async def _fake_create_subagent(**kwargs):
        return _FakeAgent()

    monkeypatch.setattr(subagent_actor, "db", _FakeDB())
    monkeypatch.setattr(subagent_actor, "claim_subagent_for_epoch", _fake_claim)
    monkeypatch.setattr(subagent_actor, "submit_result", _fake_submit)
    monkeypatch.setattr(subagent_actor, "create_subagent", _fake_create_subagent)
    monkeypatch.setattr(subagent_actor, "reconcile_late_notes", _fake_reconcile)
    monkeypatch.setattr(subagent_actor, "get_state", _fake_get_state)
    monkeypatch.setattr(subagent_actor, "get_run_sandbox_lease", _fake_get_lease)

    await _invoke_actor(
        parent_run_id="run-released",
        subtask_id="task-released",
        subtask_config={"role": "researcher", "task_description": "do it"},
        project_id="proj-1",
        thread_id="thread-1",
    )


@pytest.mark.asyncio
async def test_subagent_actor_does_not_persist_invalidated_execution(monkeypatch):
    submits = []
    finalizes = []
    publishes = []

    async def _fake_claim(*args, **kwargs):
        return True

    async def _fake_finalize(*args, **kwargs):
        finalizes.append((args, kwargs))
        return True

    async def _fake_submit(*args, **kwargs):
        submits.append((args, kwargs))
        return {"status": "submitted"}

    async def _fake_publish(*args, **kwargs):
        publishes.append((args, kwargs))
        return None

    async def _fake_reconcile(**kwargs):
        return [], kwargs.get("cursor", 0)

    class _FakeAgent:
        def __init__(self):
            self._shadow_peer_note_cursor = 0
            self._shadow_seen_peer_note_ids = set()

        async def __call__(self, _msg):
            return _FakeMsg("done")

    async def _fake_create_subagent(**kwargs):
        return _FakeAgent()

    async def _fake_execute_with_activity(**kwargs):
        raise subagent_actor.ShadowCloneExecutionInvalidatedError("stopped")

    monkeypatch.setattr(subagent_actor, "db", _FakeDB())
    monkeypatch.setattr(subagent_actor, "claim_subagent_for_epoch", _fake_claim)
    monkeypatch.setattr(subagent_actor, "finalize_subagent_for_epoch", _fake_finalize)
    monkeypatch.setattr(subagent_actor, "submit_result", _fake_submit)
    monkeypatch.setattr(subagent_actor, "_publish_subagent_update", _fake_publish)
    monkeypatch.setattr(subagent_actor, "create_subagent", _fake_create_subagent)
    monkeypatch.setattr(subagent_actor, "_execute_subagent_with_activity", _fake_execute_with_activity)
    monkeypatch.setattr(subagent_actor, "reconcile_late_notes", _fake_reconcile)

    await _invoke_actor(
        parent_run_id="run-invalidated",
        subtask_id="task-invalidated",
        subtask_config={"role": "researcher", "task_description": "do it"},
        project_id="proj-1",
        thread_id="thread-1",
    )

    assert submits == []
    assert finalizes == []
    assert publishes == []


@pytest.mark.asyncio
async def test_execute_subagent_with_activity_cancels_streaming_agent_task_after_invalidation(
    monkeypatch,
):
    cancellation_markers = {"count": 0}
    started = asyncio.Event()

    class _FakeStreamingAgent:
        def set_console_output_enabled(self, _enabled):
            return None

        async def __call__(self, _msg):
            started.set()
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                cancellation_markers["count"] += 1
                raise

    async def _fake_stream_printing_messages(*, coroutine_task, **_kwargs):
        asyncio.create_task(coroutine_task)
        await started.wait()
        yield _FakeMsg("partial"), False

    async def _fake_raise_if_execution_invalidated(**_kwargs):
        raise subagent_actor.ShadowCloneExecutionInvalidatedError("stopped")

    monkeypatch.setattr(
        subagent_actor,
        "stream_printing_messages",
        _fake_stream_printing_messages,
    )
    monkeypatch.setattr(
        subagent_actor,
        "_raise_if_execution_invalidated",
        _fake_raise_if_execution_invalidated,
    )

    with pytest.raises(subagent_actor.ShadowCloneExecutionInvalidatedError):
        await subagent_actor._execute_subagent_with_activity(
            parent_run_id="run-invalidated-stream",
            subtask_id="task-invalidated-stream",
            role="researcher",
            thread_id="thread-1",
            user_task="do it",
            subagent=_FakeStreamingAgent(),
            execution_epoch=0,
        )

    await asyncio.sleep(0)
    assert cancellation_markers["count"] == 1


@pytest.mark.asyncio
async def test_subagent_actor_allows_reattaching_lease(monkeypatch):
    finalizes = []

    async def _fake_claim(*args, **kwargs):
        return True

    async def _fake_finalize(*args, **kwargs):
        finalizes.append((args, kwargs))
        return True

    async def _fake_submit(*args, **kwargs):
        return {"status": "submitted"}

    async def _fake_publish(*args, **kwargs):
        return None

    async def _fake_reconcile(**kwargs):
        return [], kwargs.get("cursor", 0)

    async def _fake_get_state(_run_id):
        return {"execution_epoch": 0}

    async def _fake_get_lease(_run_id):
        return {"binding_state": "reattaching"}

    class _FakeAgent:
        def __init__(self):
            self._shadow_peer_note_cursor = 0
            self._shadow_seen_peer_note_ids = set()

        async def __call__(self, _msg):
            return _FakeMsg("done")

    async def _fake_create_subagent(**kwargs):
        return _FakeAgent()

    monkeypatch.setattr(subagent_actor, "db", _FakeDB())
    monkeypatch.setattr(subagent_actor, "claim_subagent_for_epoch", _fake_claim)
    monkeypatch.setattr(subagent_actor, "finalize_subagent_for_epoch", _fake_finalize)
    monkeypatch.setattr(subagent_actor, "submit_result", _fake_submit)
    monkeypatch.setattr(subagent_actor, "_publish_subagent_update", _fake_publish)
    monkeypatch.setattr(subagent_actor, "create_subagent", _fake_create_subagent)
    monkeypatch.setattr(subagent_actor, "reconcile_late_notes", _fake_reconcile)
    monkeypatch.setattr(subagent_actor, "get_state", _fake_get_state)
    monkeypatch.setattr(subagent_actor, "get_run_sandbox_lease", _fake_get_lease)

    await _invoke_actor(
        parent_run_id="run-reattaching",
        subtask_id="task-reattaching",
        subtask_config={"role": "researcher", "task_description": "do it"},
        project_id="proj-1",
        thread_id="thread-1",
    )

    assert finalizes[-1][0][2] == "completed"



def test_build_activity_payload_relays_text_and_reasoning_chunks():
    text_payload = subagent_actor._build_activity_payload(
        subtask_id='task-text',
        role='researcher',
        sse_message={
            'type': 'assistant',
            'sequence': 3,
            'content': json.dumps({'content': 'hello world'}),
            'metadata': json.dumps({'stream_status': 'chunk', 'thread_run_id': 'tr-1'}),
            'created_at': '2026-03-06T00:00:00Z',
            'updated_at': '2026-03-06T00:00:00Z',
        },
    )
    assert text_payload is not None
    assert text_payload['message_type'] == 'assistant'
    assert text_payload['metadata']['stream_status'] == 'chunk'
    assert text_payload['content']['content'] == 'hello world'

    reasoning_payload = subagent_actor._build_activity_payload(
        subtask_id='task-think',
        role='analyst',
        sse_message={
            'type': 'assistant',
            'sequence': 4,
            'content': json.dumps({'reasoning_content': 'thinking'}),
            'metadata': json.dumps({'stream_status': 'reasoning_chunk', 'thread_run_id': 'tr-2'}),
            'created_at': '2026-03-06T00:00:01Z',
            'updated_at': '2026-03-06T00:00:01Z',
        },
    )
    assert reasoning_payload is not None
    assert reasoning_payload['metadata']['stream_status'] == 'reasoning_chunk'
    assert reasoning_payload['content']['reasoning_content'] == 'thinking'
