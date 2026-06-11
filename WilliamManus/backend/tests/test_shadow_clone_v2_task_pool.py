from __future__ import annotations

import json

import pytest

from agentscope_integration.shadow_clone_v2 import task_pool
from agentscope_integration.shadow_clone_v2.models import TaskStatus


@pytest.mark.asyncio
async def test_claim_task_uses_atomic_lua_with_task_lease(monkeypatch) -> None:
    captured = {}
    task = task_pool.Task(
        id="task-1",
        run_id="run-1",
        thread_id="thread-1",
        subject="Review",
        description="Review the draft.",
        created_at="2026-05-31T12:00:00Z",
        updated_at="2026-05-31T12:00:00Z",
    )

    async def _get(key, default=None, timeout=None):
        return task.model_dump_json()

    async def _eval_script(script, keys, args, timeout=None):
        captured["script"] = script
        captured["keys"] = keys
        captured["args"] = args
        captured["timeout"] = timeout
        return 1

    monkeypatch.setattr(task_pool.redis_service, "eval_script", _eval_script)
    monkeypatch.setattr(task_pool.redis_service, "get", _get)

    claimed = await task_pool.claim_task(
        run_id="run-1",
        task_id="task-1",
        agent_name="reviewer",
        lease_token="lease-abc",
        lease_expires_at="2026-05-31T12:05:00Z",
        updated_at="2026-05-31T12:00:00Z",
    )

    assert claimed is True
    assert captured["keys"] == ["sc_v2:run:run-1:task:task-1"]
    assert captured["args"] == [
        "reviewer",
        "lease-abc",
        "2026-05-31T12:05:00Z",
        "2026-05-31T12:00:00Z",
        0,
    ]
    assert "status" in captured["script"]
    assert "in_progress" in captured["script"]
    assert "attempt" in captured["script"]
    assert "plan_revision" in captured["script"]
    assert "KEEPTTL" in captured["script"]


@pytest.mark.asyncio
async def test_claim_task_rejects_pending_task_with_blockers_in_lua(
    monkeypatch,
) -> None:
    captured = {}
    task = task_pool.Task(
        id="task-1",
        run_id="run-1",
        thread_id="thread-1",
        subject="Review",
        description="Review the draft.",
        blocked_by=["task-0"],
        created_at="2026-05-31T12:00:00Z",
        updated_at="2026-05-31T12:00:00Z",
    )

    async def _get(key, default=None, timeout=None):
        return task.model_dump_json()

    async def _eval_script(script, keys, args, timeout=None):
        captured["script"] = script
        captured["keys"] = keys
        captured["args"] = args
        return -1

    monkeypatch.setattr(task_pool.redis_service, "eval_script", _eval_script)
    monkeypatch.setattr(task_pool.redis_service, "get", _get)

    claimed = await task_pool.claim_task(
        run_id="run-1",
        task_id="task-1",
        agent_name="reviewer",
        lease_token="lease-abc",
        lease_expires_at="2026-05-31T12:05:00Z",
        updated_at="2026-05-31T12:00:00Z",
    )

    assert claimed is False
    assert "blocked_by" in captured["script"]
    assert "completed" in captured["script"]
    assert captured["keys"] == [
        "sc_v2:run:run-1:task:task-1",
        "sc_v2:run:run-1:task:task-0",
    ]
    assert captured["args"][4:] == [1, "task-0"]


@pytest.mark.asyncio
async def test_claim_task_allows_task_when_blockers_are_completed(monkeypatch) -> None:
    captured = {}
    task = task_pool.Task(
        id="task-1",
        run_id="run-1",
        thread_id="thread-1",
        subject="Review",
        description="Review the draft.",
        blocked_by=["task-0"],
        created_at="2026-05-31T12:00:00Z",
        updated_at="2026-05-31T12:00:00Z",
    )

    async def _get(key, default=None, timeout=None):
        return task.model_dump_json()

    async def _eval_script(script, keys, args, timeout=None):
        captured["script"] = script
        captured["keys"] = keys
        captured["args"] = args
        return 1

    monkeypatch.setattr(task_pool.redis_service, "eval_script", _eval_script)
    monkeypatch.setattr(task_pool.redis_service, "get", _get)

    claimed = await task_pool.claim_task(
        run_id="run-1",
        task_id="task-1",
        agent_name="reviewer",
        lease_token="lease-abc",
        lease_expires_at="2026-05-31T12:05:00Z",
        updated_at="2026-05-31T12:00:00Z",
    )

    assert claimed is True
    assert "completed" in captured["script"]


@pytest.mark.asyncio
async def test_claim_task_returns_false_when_task_is_not_pending(monkeypatch) -> None:
    task = task_pool.Task(
        id="task-1",
        run_id="run-1",
        thread_id="thread-1",
        subject="Review",
        description="Review the draft.",
        created_at="2026-05-31T12:00:00Z",
        updated_at="2026-05-31T12:00:00Z",
    )

    async def _get(key, default=None, timeout=None):
        return task.model_dump_json()

    async def _eval_script(*_args, **_kwargs):
        return -1

    monkeypatch.setattr(task_pool.redis_service, "eval_script", _eval_script)
    monkeypatch.setattr(task_pool.redis_service, "get", _get)

    claimed = await task_pool.claim_task(
        run_id="run-1",
        task_id="task-1",
        agent_name="reviewer",
        lease_token="lease-abc",
        lease_expires_at="2026-05-31T12:05:00Z",
        updated_at="2026-05-31T12:00:00Z",
    )

    assert claimed is False


@pytest.mark.asyncio
async def test_claim_task_rejects_blank_agent_or_lease_before_lua(monkeypatch) -> None:
    evals = []

    async def _eval_script(*args, **kwargs):
        evals.append((args, kwargs))
        return 1

    monkeypatch.setattr(task_pool.redis_service, "eval_script", _eval_script)

    with pytest.raises(ValueError, match="agent_name is required"):
        await task_pool.claim_task(
            run_id="run-1",
            task_id="task-1",
            agent_name=" ",
            lease_token="lease-abc",
            lease_expires_at="2026-05-31T12:05:00Z",
            updated_at="2026-05-31T12:00:00Z",
        )

    with pytest.raises(ValueError, match="lease_token is required"):
        await task_pool.claim_task(
            run_id="run-1",
            task_id="task-1",
            agent_name="reviewer",
            lease_token=" ",
            lease_expires_at="2026-05-31T12:05:00Z",
            updated_at="2026-05-31T12:00:00Z",
        )

    assert evals == []


@pytest.mark.asyncio
async def test_store_and_read_task_round_trip(monkeypatch) -> None:
    stored = {}

    async def _set(key, value, ex=None, nx=False, timeout=None):
        stored[key] = {"value": value, "ex": ex, "nx": nx, "timeout": timeout}
        return True

    async def _get(key, default=None, timeout=None):
        return stored.get(key, {}).get("value", default)

    monkeypatch.setattr(task_pool.redis_service, "set", _set)
    monkeypatch.setattr(task_pool.redis_service, "get", _get)

    task = task_pool.Task(
        id="task-1",
        run_id="run-1",
        thread_id="thread-1",
        subject="Review",
        description="Review the draft.",
        created_at="2026-05-31T12:00:00Z",
        updated_at="2026-05-31T12:00:00Z",
    )

    await task_pool.store_task(task, ttl_seconds=3600)
    loaded = await task_pool.read_task(run_id="run-1", task_id="task-1")

    assert stored["sc_v2:run:run-1:task:task-1"]["ex"] == 3600
    assert loaded == task


@pytest.mark.asyncio
async def test_read_task_normalizes_redis_empty_object_dependency_fields(
    monkeypatch,
) -> None:
    raw_task = {
        "id": "task-1",
        "run_id": "run-1",
        "thread_id": "thread-1",
        "subject": "Review",
        "description": "Review the draft.",
        "status": "pending",
        "priority": 100,
        "blocked_by": {},
        "blocks": {},
        "owner_agent": None,
        "lease_token": None,
        "lease_expires_at": None,
        "attempt": 0,
        "max_attempts": 2,
        "created_by": "facilitator",
        "plan_revision": 1,
        "result_ref": None,
        "error": {},
        "metadata": {"agent_name": "reviewer"},
        "created_at": "2026-05-31T12:00:00Z",
        "updated_at": "2026-05-31T12:00:00Z",
    }

    async def _get(key, default=None, timeout=None):
        return json.dumps(raw_task)

    monkeypatch.setattr(task_pool.redis_service, "get", _get)

    task = await task_pool.read_task(run_id="run-1", task_id="task-1")

    assert task is not None
    assert task.blocked_by == []
    assert task.blocks == []


@pytest.mark.asyncio
async def test_complete_task_uses_atomic_lua_and_owner_lease(monkeypatch) -> None:
    captured = {}
    task = task_pool.Task(
        id="task-1",
        run_id="run-1",
        thread_id="thread-1",
        subject="Review",
        description="Review the draft.",
        status=TaskStatus.IN_PROGRESS,
        owner_agent="reviewer",
        lease_token="lease-abc",
        lease_expires_at="2026-05-31T12:20:00Z",
        created_at="2026-05-31T12:00:00Z",
        updated_at="2026-05-31T12:00:00Z",
    )

    async def _get(key, default=None, timeout=None):
        return task.model_dump_json()

    async def _eval_script(script, keys, args, timeout=None):
        captured["script"] = script
        captured["keys"] = keys
        captured["args"] = args
        captured["timeout"] = timeout
        return 1

    monkeypatch.setattr(task_pool.redis_service, "get", _get)
    monkeypatch.setattr(task_pool.redis_service, "eval_script", _eval_script)

    completed = await task_pool.complete_task(
        run_id="run-1",
        task_id="task-1",
        owner_agent="reviewer",
        lease_token="lease-abc",
        result_ref="result://task-1",
        updated_at="2026-05-31T12:10:00Z",
    )

    assert completed is True
    assert captured["keys"] == ["sc_v2:run:run-1:task:task-1"]
    assert captured["args"] == [
        "reviewer",
        "lease-abc",
        "result://task-1",
        "2026-05-31T12:20:00Z",
        "2026-05-31T12:10:00Z",
        "2026-05-31T12:10:00Z",
    ]
    assert "lease_token" in captured["script"]
    assert "completed" in captured["script"]
    assert "plan_revision" in captured["script"]
    assert "KEEPTTL" in captured["script"]


@pytest.mark.asyncio
async def test_complete_task_returns_false_when_lease_is_lost(monkeypatch) -> None:
    task = task_pool.Task(
        id="task-1",
        run_id="run-1",
        thread_id="thread-1",
        subject="Review",
        description="Review the draft.",
        status=TaskStatus.IN_PROGRESS,
        owner_agent="reviewer",
        lease_token="lease-abc",
        lease_expires_at="2026-05-31T12:20:00Z",
        created_at="2026-05-31T12:00:00Z",
        updated_at="2026-05-31T12:00:00Z",
    )

    async def _get(key, default=None, timeout=None):
        return task.model_dump_json()

    async def _eval_script(*_args, **_kwargs):
        return -2

    monkeypatch.setattr(task_pool.redis_service, "get", _get)
    monkeypatch.setattr(task_pool.redis_service, "eval_script", _eval_script)

    completed = await task_pool.complete_task(
        run_id="run-1",
        task_id="task-1",
        owner_agent="reviewer",
        lease_token="stale-lease",
        result_ref="result://task-1",
        updated_at="2026-05-31T12:10:00Z",
    )

    assert completed is False


@pytest.mark.asyncio
async def test_complete_task_returns_false_when_lease_is_expired(monkeypatch) -> None:
    evals = []
    task = task_pool.Task(
        id="task-1",
        run_id="run-1",
        thread_id="thread-1",
        subject="Review",
        description="Review the draft.",
        status=TaskStatus.IN_PROGRESS,
        owner_agent="reviewer",
        lease_token="lease-abc",
        lease_expires_at="2026-05-31T12:05:00Z",
        created_at="2026-05-31T12:00:00Z",
        updated_at="2026-05-31T12:00:00Z",
    )

    async def _get(key, default=None, timeout=None):
        return task.model_dump_json()

    async def _eval_script(script, keys, args, timeout=None):
        evals.append((script, keys, args, timeout))
        return 1

    monkeypatch.setattr(task_pool.redis_service, "get", _get)
    monkeypatch.setattr(task_pool.redis_service, "eval_script", _eval_script)

    completed = await task_pool.complete_task(
        run_id="run-1",
        task_id="task-1",
        owner_agent="reviewer",
        lease_token="lease-abc",
        result_ref="result://task-1",
        updated_at="2026-05-31T12:10:00Z",
        now="2026-05-31T12:10:00Z",
    )

    assert completed is False
    assert evals == []


@pytest.mark.asyncio
async def test_complete_task_rejects_non_utc_offset_expiry_without_lua(
    monkeypatch,
) -> None:
    evals = []
    task = task_pool.Task(
        id="task-1",
        run_id="run-1",
        thread_id="thread-1",
        subject="Review",
        description="Review the draft.",
        status=TaskStatus.IN_PROGRESS,
        owner_agent="reviewer",
        lease_token="lease-abc",
        lease_expires_at="2026-05-31T13:00:00+01:00",
        created_at="2026-05-31T12:00:00Z",
        updated_at="2026-05-31T12:00:00Z",
    )

    async def _get(key, default=None, timeout=None):
        return task.model_dump_json()

    async def _eval_script(*args, **kwargs):
        evals.append((args, kwargs))
        return 1

    monkeypatch.setattr(task_pool.redis_service, "get", _get)
    monkeypatch.setattr(task_pool.redis_service, "eval_script", _eval_script)

    completed = await task_pool.complete_task(
        run_id="run-1",
        task_id="task-1",
        owner_agent="reviewer",
        lease_token="lease-abc",
        result_ref="result://task-1",
        updated_at="2026-05-31T12:30:00+00:00",
        now="2026-05-31T12:30:00+00:00",
    )

    assert completed is False
    assert evals == []


@pytest.mark.asyncio
async def test_fail_task_uses_atomic_lua_and_owner_lease(monkeypatch) -> None:
    captured = {}
    task = task_pool.Task(
        id="task-1",
        run_id="run-1",
        thread_id="thread-1",
        subject="Review",
        description="Review the draft.",
        status=TaskStatus.IN_PROGRESS,
        owner_agent="reviewer",
        lease_token="lease-abc",
        lease_expires_at="2026-05-31T12:20:00Z",
        created_at="2026-05-31T12:00:00Z",
        updated_at="2026-05-31T12:00:00Z",
    )

    async def _get(key, default=None, timeout=None):
        return task.model_dump_json()

    async def _eval_script(script, keys, args, timeout=None):
        captured["script"] = script
        captured["keys"] = keys
        captured["args"] = args
        captured["timeout"] = timeout
        return 1

    monkeypatch.setattr(task_pool.redis_service, "get", _get)
    monkeypatch.setattr(task_pool.redis_service, "eval_script", _eval_script)

    failed = await task_pool.fail_task(
        run_id="run-1",
        task_id="task-1",
        owner_agent="reviewer",
        lease_token="lease-abc",
        error_type="ValueError",
        error_message="executor exploded",
        updated_at="2026-05-31T12:10:00Z",
    )

    assert failed is True
    assert captured["keys"] == ["sc_v2:run:run-1:task:task-1"]
    assert captured["args"] == [
        "reviewer",
        "lease-abc",
        "ValueError",
        "executor exploded",
        "2026-05-31T12:20:00Z",
        "2026-05-31T12:10:00Z",
        "2026-05-31T12:10:00Z",
    ]
    assert "lease_token" in captured["script"]
    assert "failed" in captured["script"]
    assert "error_type" in captured["script"]
    assert "plan_revision" in captured["script"]
    assert "KEEPTTL" in captured["script"]


@pytest.mark.asyncio
async def test_fail_task_returns_false_when_lease_is_expired(monkeypatch) -> None:
    evals = []
    task = task_pool.Task(
        id="task-1",
        run_id="run-1",
        thread_id="thread-1",
        subject="Review",
        description="Review the draft.",
        status=TaskStatus.IN_PROGRESS,
        owner_agent="reviewer",
        lease_token="lease-abc",
        lease_expires_at="not-a-date",
        created_at="2026-05-31T12:00:00Z",
        updated_at="2026-05-31T12:00:00Z",
    )

    async def _get(key, default=None, timeout=None):
        return task.model_dump_json()

    async def _eval_script(script, keys, args, timeout=None):
        evals.append((script, keys, args, timeout))
        return 1

    monkeypatch.setattr(task_pool.redis_service, "get", _get)
    monkeypatch.setattr(task_pool.redis_service, "eval_script", _eval_script)

    failed = await task_pool.fail_task(
        run_id="run-1",
        task_id="task-1",
        owner_agent="reviewer",
        lease_token="lease-abc",
        error_type="ValueError",
        error_message="late failure",
        updated_at="2026-05-31T12:10:00Z",
        now="2026-05-31T12:10:00Z",
    )

    assert failed is False
    assert evals == []


@pytest.mark.asyncio
async def test_renew_task_lease_uses_atomic_owner_token_guard_and_keeps_ttl(
    monkeypatch,
) -> None:
    captured = {}
    task = task_pool.Task(
        id="task-1",
        run_id="run-1",
        thread_id="thread-1",
        subject="Review",
        description="Review the draft.",
        status=TaskStatus.IN_PROGRESS,
        owner_agent="reviewer",
        lease_token="lease-abc",
        lease_expires_at="2026-05-31T12:15:00Z",
        created_at="2026-05-31T12:00:00Z",
        updated_at="2026-05-31T12:00:00Z",
    )

    async def _get(key, default=None, timeout=None):
        return task.model_dump_json()

    async def _eval_script(script, keys, args, timeout=None):
        captured["script"] = script
        captured["keys"] = keys
        captured["args"] = args
        captured["timeout"] = timeout
        return 1

    monkeypatch.setattr(task_pool.redis_service, "get", _get)
    monkeypatch.setattr(task_pool.redis_service, "eval_script", _eval_script)

    renewed = await task_pool.renew_task_lease(
        run_id="run-1",
        task_id="task-1",
        owner_agent="reviewer",
        lease_token="lease-abc",
        lease_expires_at="2026-05-31T12:20:00Z",
        updated_at="2026-05-31T12:10:00Z",
        timeout=2.0,
    )

    assert renewed is True
    assert captured["keys"] == ["sc_v2:run:run-1:task:task-1"]
    assert captured["args"] == [
        "reviewer",
        "lease-abc",
        "2026-05-31T12:15:00Z",
        "2026-05-31T12:20:00Z",
        "2026-05-31T12:10:00Z",
        "2026-05-31T12:10:00Z",
    ]
    assert "lease_token" in captured["script"]
    assert "lease_expires_at" in captured["script"]
    assert "plan_revision" in captured["script"]
    assert "KEEPTTL" in captured["script"]
    assert captured["timeout"] == 2.0


@pytest.mark.asyncio
async def test_renew_task_lease_returns_false_when_lease_is_lost(
    monkeypatch,
) -> None:
    task = task_pool.Task(
        id="task-1",
        run_id="run-1",
        thread_id="thread-1",
        subject="Review",
        description="Review the draft.",
        status=TaskStatus.IN_PROGRESS,
        owner_agent="reviewer",
        lease_token="lease-abc",
        lease_expires_at="2026-05-31T12:15:00Z",
        created_at="2026-05-31T12:00:00Z",
        updated_at="2026-05-31T12:00:00Z",
    )

    async def _get(key, default=None, timeout=None):
        return task.model_dump_json()

    async def _eval_script(*_args, **_kwargs):
        return -2

    monkeypatch.setattr(task_pool.redis_service, "get", _get)
    monkeypatch.setattr(task_pool.redis_service, "eval_script", _eval_script)

    renewed = await task_pool.renew_task_lease(
        run_id="run-1",
        task_id="task-1",
        owner_agent="reviewer",
        lease_token="stale-lease",
        lease_expires_at="2026-05-31T12:20:00Z",
        updated_at="2026-05-31T12:10:00Z",
    )

    assert renewed is False


@pytest.mark.asyncio
async def test_renew_task_lease_rejects_malformed_existing_expiry_before_lua(
    monkeypatch,
) -> None:
    evals = []
    task = task_pool.Task(
        id="task-1",
        run_id="run-1",
        thread_id="thread-1",
        subject="Review",
        description="Review the draft.",
        status=TaskStatus.IN_PROGRESS,
        owner_agent="reviewer",
        lease_token="lease-abc",
        lease_expires_at="not-a-date",
        created_at="2026-05-31T12:00:00Z",
        updated_at="2026-05-31T12:00:00Z",
    )

    async def _get(key, default=None, timeout=None):
        return task.model_dump_json()

    async def _eval_script(*args, **kwargs):
        evals.append((args, kwargs))
        return 1

    monkeypatch.setattr(task_pool.redis_service, "get", _get)
    monkeypatch.setattr(task_pool.redis_service, "eval_script", _eval_script)

    renewed = await task_pool.renew_task_lease(
        run_id="run-1",
        task_id="task-1",
        owner_agent="reviewer",
        lease_token="lease-abc",
        lease_expires_at="2026-05-31T12:20:00Z",
        updated_at="2026-05-31T12:10:00Z",
        now="2026-05-31T12:10:00Z",
    )

    assert renewed is False
    assert evals == []


@pytest.mark.asyncio
async def test_renew_task_lease_rejects_blank_owner_or_token_before_lua(
    monkeypatch,
) -> None:
    evals = []

    async def _eval_script(*args, **kwargs):
        evals.append((args, kwargs))
        return 1

    monkeypatch.setattr(task_pool.redis_service, "eval_script", _eval_script)

    with pytest.raises(ValueError, match="owner_agent is required"):
        await task_pool.renew_task_lease(
            run_id="run-1",
            task_id="task-1",
            owner_agent=" ",
            lease_token="lease-abc",
            lease_expires_at="2026-05-31T12:20:00Z",
            updated_at="2026-05-31T12:10:00Z",
        )

    with pytest.raises(ValueError, match="lease_token is required"):
        await task_pool.renew_task_lease(
            run_id="run-1",
            task_id="task-1",
            owner_agent="reviewer",
            lease_token=" ",
            lease_expires_at="2026-05-31T12:20:00Z",
            updated_at="2026-05-31T12:10:00Z",
        )

    assert evals == []


@pytest.mark.asyncio
async def test_sweep_expired_task_leases_requeues_abandoned_task_before_max_attempts(
    monkeypatch,
) -> None:
    now = "2026-05-31T12:10:00+00:00"
    stored_task = task_pool.Task(
        id="task-1",
        run_id="run-1",
        thread_id="thread-1",
        subject="Review",
        description="Review the draft.",
        status="in_progress",
        owner_agent="reviewer",
        lease_token="lease-abc",
        lease_expires_at="2026-05-31T12:05:00+00:00",
        attempt=1,
        max_attempts=2,
        created_at="2026-05-31T12:00:00+00:00",
        updated_at="2026-05-31T12:00:00+00:00",
    )
    captured = {}
    recovered_task = stored_task.model_copy(
        update={
            "status": TaskStatus.PENDING,
            "owner_agent": None,
            "lease_token": None,
            "lease_expires_at": None,
            "plan_revision": 2,
            "updated_at": now,
            "error": {
                "error_type": "LeaseExpired",
                "message": "Task lease expired before completion.",
            },
        }
    )

    async def _scan_keys(pattern, *, count=1000, timeout=None):
        return ["sc_v2:run:run-1:task:task-1"]

    async def _get(key, default=None, timeout=None):
        return stored_task.model_dump_json()

    async def _eval_script(script, keys, args, timeout=None):
        captured["script"] = script
        captured["keys"] = keys
        captured["args"] = args
        return ["retried", recovered_task.model_dump_json()]

    monkeypatch.setattr(task_pool.redis_service, "scan_keys", _scan_keys)
    monkeypatch.setattr(task_pool.redis_service, "get", _get)
    monkeypatch.setattr(task_pool.redis_service, "eval_script", _eval_script)

    results = await task_pool.sweep_expired_task_leases(
        run_id="run-1",
        now=now,
        ttl_seconds=3600,
    )

    assert len(results) == 1
    result = results[0]
    assert result.action == "retried"
    assert result.task.status == "pending"
    assert result.previous_status == "in_progress"
    assert result.previous_owner_agent == "reviewer"
    assert result.previous_lease_expires_at == "2026-05-31T12:05:00+00:00"
    assert captured["keys"] == ["sc_v2:run:run-1:task:task-1"]
    assert captured["args"] == [
        "reviewer",
        "lease-abc",
        "2026-05-31T12:05:00+00:00",
        now,
        3600,
        0,
    ]
    assert "max_attempts" in captured["script"]
    assert "LeaseExpired" in captured["script"]
    assert "pending" in captured["script"]
    assert "failed" in captured["script"]


@pytest.mark.asyncio
async def test_sweep_expired_task_leases_fails_task_at_max_attempts(
    monkeypatch,
) -> None:
    now = "2026-05-31T12:10:00+00:00"
    stored_task = task_pool.Task(
        id="task-1",
        run_id="run-1",
        thread_id="thread-1",
        subject="Review",
        description="Review the draft.",
        status="in_progress",
        owner_agent="reviewer",
        lease_token="lease-abc",
        lease_expires_at="2026-05-31T12:05:00+00:00",
        attempt=2,
        max_attempts=2,
        created_at="2026-05-31T12:00:00+00:00",
        updated_at="2026-05-31T12:00:00+00:00",
    )
    failed_task = stored_task.model_copy(
        update={
            "status": TaskStatus.FAILED,
            "owner_agent": None,
            "lease_token": None,
            "lease_expires_at": None,
            "plan_revision": 2,
            "updated_at": now,
            "error": {
                "error_type": "LeaseExpired",
                "message": "Task lease expired after maximum attempts.",
            },
        }
    )

    async def _scan_keys(pattern, *, count=1000, timeout=None):
        return ["sc_v2:run:run-1:task:task-1"]

    async def _get(key, default=None, timeout=None):
        return stored_task.model_dump_json()

    async def _eval_script(*_args, **_kwargs):
        return ["failed", failed_task.model_dump_json()]

    monkeypatch.setattr(task_pool.redis_service, "scan_keys", _scan_keys)
    monkeypatch.setattr(task_pool.redis_service, "get", _get)
    monkeypatch.setattr(task_pool.redis_service, "eval_script", _eval_script)

    results = await task_pool.sweep_expired_task_leases(
        run_id="run-1",
        now=now,
        ttl_seconds=3600,
    )

    assert len(results) == 1
    assert results[0].action == "failed"
    assert results[0].task.status == "failed"
    assert results[0].task.error["error_type"] == "LeaseExpired"


@pytest.mark.asyncio
async def test_sweep_expired_task_leases_ignores_unexpired_tasks(
    monkeypatch,
) -> None:
    stored_task = task_pool.Task(
        id="task-1",
        run_id="run-1",
        thread_id="thread-1",
        subject="Review",
        description="Review the draft.",
        status="in_progress",
        owner_agent="reviewer",
        lease_token="lease-abc",
        lease_expires_at="2026-05-31T12:20:00+00:00",
        created_at="2026-05-31T12:00:00+00:00",
        updated_at="2026-05-31T12:00:00+00:00",
    )
    eval_calls = []

    async def _scan_keys(pattern, *, count=1000, timeout=None):
        return ["sc_v2:run:run-1:task:task-1"]

    async def _get(key, default=None, timeout=None):
        return stored_task.model_dump_json()

    async def _eval_script(*args, **kwargs):
        eval_calls.append((args, kwargs))
        return ["retried", stored_task.model_dump_json()]

    monkeypatch.setattr(task_pool.redis_service, "scan_keys", _scan_keys)
    monkeypatch.setattr(task_pool.redis_service, "get", _get)
    monkeypatch.setattr(task_pool.redis_service, "eval_script", _eval_script)

    results = await task_pool.sweep_expired_task_leases(
        run_id="run-1",
        now="2026-05-31T12:10:00+00:00",
        ttl_seconds=3600,
    )

    assert results == []
    assert eval_calls == []


@pytest.mark.asyncio
async def test_sweep_expired_task_leases_returns_blocked_when_blockers_unresolved(
    monkeypatch,
) -> None:
    now = "2026-05-31T12:10:00+00:00"
    stored_task = task_pool.Task(
        id="task-1",
        run_id="run-1",
        thread_id="thread-1",
        subject="Review",
        description="Review the draft.",
        status=TaskStatus.IN_PROGRESS,
        blocked_by=["task-0"],
        owner_agent="reviewer",
        lease_token="lease-abc",
        lease_expires_at="2026-05-31T12:05:00+00:00",
        attempt=1,
        max_attempts=2,
        created_at="2026-05-31T12:00:00+00:00",
        updated_at="2026-05-31T12:00:00+00:00",
    )
    recovered_task = stored_task.model_copy(
        update={
            "status": TaskStatus.BLOCKED,
            "owner_agent": None,
            "lease_token": None,
            "lease_expires_at": None,
            "plan_revision": 2,
            "updated_at": now,
            "error": {
                "error_type": "LeaseExpired",
                "message": "Task lease expired before completion.",
            },
        }
    )
    captured = {}

    async def _scan_keys(pattern, *, count=1000, timeout=None):
        return ["sc_v2:run:run-1:task:task-1"]

    async def _get(key, default=None, timeout=None):
        return stored_task.model_dump_json()

    async def _eval_script(script, keys, args, timeout=None):
        captured["keys"] = keys
        captured["args"] = args
        return ["retried", recovered_task.model_dump_json()]

    monkeypatch.setattr(task_pool.redis_service, "scan_keys", _scan_keys)
    monkeypatch.setattr(task_pool.redis_service, "get", _get)
    monkeypatch.setattr(task_pool.redis_service, "eval_script", _eval_script)

    results = await task_pool.sweep_expired_task_leases(
        run_id="run-1",
        now=now,
        ttl_seconds=3600,
    )

    assert results[0].task.status == TaskStatus.BLOCKED
    assert captured["keys"] == [
        "sc_v2:run:run-1:task:task-1",
        "sc_v2:run:run-1:task:task-0",
    ]
    assert captured["args"][-1] == 1


@pytest.mark.asyncio
async def test_sweep_expired_task_leases_rejects_unsafe_run_id_before_scan(
    monkeypatch,
) -> None:
    scans = []

    async def _scan_keys(*args, **kwargs):
        scans.append((args, kwargs))
        return []

    monkeypatch.setattr(task_pool.redis_service, "scan_keys", _scan_keys)

    with pytest.raises(ValueError, match="run_id must not contain"):
        await task_pool.sweep_expired_task_leases(
            run_id="run:bad",
            now="2026-05-31T12:10:00+00:00",
            ttl_seconds=3600,
        )

    assert scans == []


@pytest.mark.asyncio
async def test_list_tasks_scans_run_task_namespace_and_loads_tasks(monkeypatch) -> None:
    scanned = []
    stored = {}

    task_a = task_pool.Task(
        id="task-a",
        run_id="run-1",
        thread_id="thread-1",
        subject="A",
        description="First task.",
        created_at="2026-06-02T00:00:00Z",
        updated_at="2026-06-02T00:00:00Z",
    )
    task_b = task_pool.Task(
        id="task-b",
        run_id="run-1",
        thread_id="thread-1",
        subject="B",
        description="Second task.",
        created_at="2026-06-02T00:00:01Z",
        updated_at="2026-06-02T00:00:01Z",
    )
    stored["sc_v2:run:run-1:task:task-a"] = task_a.model_dump_json()
    stored["sc_v2:run:run-1:task:task-b"] = task_b.model_dump_json()

    async def _scan_keys(pattern, *, count=1000, timeout=None):
        scanned.append((pattern, count, timeout))
        return list(reversed(stored.keys()))

    async def _get(key, default=None, timeout=None):
        return stored.get(key, default)

    monkeypatch.setattr(task_pool.redis_service, "scan_keys", _scan_keys)
    monkeypatch.setattr(task_pool.redis_service, "get", _get)

    tasks = await task_pool.list_tasks(run_id="run-1", timeout=2.0)

    assert scanned == [("sc_v2:run:run-1:task:*", 1000, 2.0)]
    assert [task.id for task in tasks] == ["task-a", "task-b"]


@pytest.mark.asyncio
async def test_list_tasks_rejects_blank_run_id() -> None:
    with pytest.raises(ValueError, match="run_id is required"):
        await task_pool.list_tasks(run_id="   ")


@pytest.mark.asyncio
async def test_create_task_uses_create_only_set(monkeypatch) -> None:
    stored = []
    task = task_pool.Task(
        id="task-1",
        run_id="run-1",
        thread_id="thread-1",
        subject="Create-only",
        description="Do not overwrite.",
        created_at="2026-06-02T00:00:00Z",
        updated_at="2026-06-02T00:00:00Z",
    )

    async def _set(key, value, ex=None, nx=False, timeout=None):
        stored.append((key, value, ex, nx, timeout))
        return True

    monkeypatch.setattr(task_pool.redis_service, "set", _set)

    created = await task_pool.create_task(task, ttl_seconds=3600, timeout=2.0)

    assert created is True
    assert stored[0][0] == "sc_v2:run:run-1:task:task-1"
    assert stored[0][2:] == (3600, True, 2.0)


@pytest.mark.asyncio
async def test_create_task_returns_false_for_existing_task(monkeypatch) -> None:
    task = task_pool.Task(
        id="task-1",
        run_id="run-1",
        thread_id="thread-1",
        subject="Duplicate",
        description="Existing task.",
        created_at="2026-06-02T00:00:00Z",
        updated_at="2026-06-02T00:00:00Z",
    )

    async def _set(*_args, **_kwargs):
        return False

    monkeypatch.setattr(task_pool.redis_service, "set", _set)

    created = await task_pool.create_task(task, ttl_seconds=3600)

    assert created is False


@pytest.mark.asyncio
async def test_update_task_uses_atomic_version_guard(monkeypatch) -> None:
    captured = {}
    task = task_pool.Task(
        id="task-1",
        run_id="run-1",
        thread_id="thread-1",
        subject="Updated",
        description="Updated description.",
        plan_revision=2,
        created_at="2026-06-02T00:00:00Z",
        updated_at="2026-06-02T00:05:00Z",
    )

    async def _eval_script(script, keys, args, timeout=None):
        captured["script"] = script
        captured["keys"] = keys
        captured["args"] = args
        captured["timeout"] = timeout
        return 1

    monkeypatch.setattr(task_pool.redis_service, "eval_script", _eval_script)

    updated = await task_pool.update_task(
        task,
        expected_plan_revision=1,
        ttl_seconds=3600,
        timeout=2.0,
    )

    assert updated is True
    assert captured["keys"] == ["sc_v2:run:run-1:task:task-1"]
    assert captured["args"] == [1, task.model_dump_json(), 3600]
    assert "plan_revision" in captured["script"]
    assert "expected_revision" in captured["script"]


@pytest.mark.asyncio
async def test_update_task_returns_false_on_stale_version(monkeypatch) -> None:
    task = task_pool.Task(
        id="task-1",
        run_id="run-1",
        thread_id="thread-1",
        subject="Updated",
        description="Updated description.",
        plan_revision=2,
        created_at="2026-06-02T00:00:00Z",
        updated_at="2026-06-02T00:05:00Z",
    )

    async def _eval_script(*_args, **_kwargs):
        return -1

    monkeypatch.setattr(task_pool.redis_service, "eval_script", _eval_script)

    updated = await task_pool.update_task(
        task,
        expected_plan_revision=1,
        ttl_seconds=3600,
    )

    assert updated is False


@pytest.mark.asyncio
async def test_update_tasks_uses_single_lua_for_dependency_graph_patch(
    monkeypatch,
) -> None:
    captured = {}
    task_a = task_pool.Task(
        id="task-a",
        run_id="run-1",
        thread_id="thread-1",
        subject="A",
        description="A",
        plan_revision=2,
        blocked_by=["task-b"],
        created_at="2026-06-02T00:00:00Z",
        updated_at="2026-06-02T00:05:00Z",
    )
    task_b = task_pool.Task(
        id="task-b",
        run_id="run-1",
        thread_id="thread-1",
        subject="B",
        description="B",
        plan_revision=4,
        blocks=["task-a"],
        created_at="2026-06-02T00:00:00Z",
        updated_at="2026-06-02T00:05:00Z",
    )

    async def _eval_script(script, keys, args, timeout=None):
        captured["script"] = script
        captured["keys"] = keys
        captured["args"] = args
        captured["timeout"] = timeout
        return 1

    monkeypatch.setattr(task_pool.redis_service, "eval_script", _eval_script)

    updated = await task_pool.update_tasks(
        tasks=[task_a, task_b],
        expected_plan_revisions={"task-a": 1, "task-b": 3},
        ttl_seconds=3600,
    )

    assert updated is True
    assert captured["keys"] == [
        "sc_v2:run:run-1:task:task-a",
        "sc_v2:run:run-1:task:task-b",
    ]
    assert captured["args"][0] == 3600
    assert "expected_revision" in captured["script"]
    assert "for i = 1, #KEYS" in captured["script"]


@pytest.mark.asyncio
async def test_update_tasks_returns_false_when_any_version_conflicts(
    monkeypatch,
) -> None:
    task = task_pool.Task(
        id="task-a",
        run_id="run-1",
        thread_id="thread-1",
        subject="A",
        description="A",
        plan_revision=2,
        created_at="2026-06-02T00:00:00Z",
        updated_at="2026-06-02T00:05:00Z",
    )

    async def _eval_script(*_args, **_kwargs):
        return -1

    monkeypatch.setattr(task_pool.redis_service, "eval_script", _eval_script)

    updated = await task_pool.update_tasks(
        tasks=[task],
        expected_plan_revisions={"task-a": 1},
        ttl_seconds=3600,
    )

    assert updated is False


@pytest.mark.asyncio
async def test_list_tasks_rejects_unsafe_run_id_before_scan(monkeypatch) -> None:
    async def _scan_keys(*_args, **_kwargs):
        raise AssertionError("invalid run id should fail before scan")

    monkeypatch.setattr(task_pool.redis_service, "scan_keys", _scan_keys)

    with pytest.raises(ValueError, match="run_id must not contain"):
        await task_pool.list_tasks(run_id="run:bad")


@pytest.mark.asyncio
async def test_store_task_rejects_unsafe_task_identity_before_redis(
    monkeypatch,
) -> None:
    writes = []
    task = task_pool.Task(
        id="task:bad",
        run_id="run-1",
        thread_id="thread-1",
        subject="Unsafe",
        description="Unsafe delimiter.",
        created_at="2026-06-02T00:00:00Z",
        updated_at="2026-06-02T00:00:00Z",
    )

    async def _set(*args, **kwargs):
        writes.append((args, kwargs))
        return True

    monkeypatch.setattr(task_pool.redis_service, "set", _set)

    with pytest.raises(ValueError, match="task_id must not contain"):
        await task_pool.store_task(task, ttl_seconds=3600)

    assert writes == []


@pytest.mark.asyncio
async def test_read_task_rejects_unsafe_identity_before_redis(monkeypatch) -> None:
    reads = []

    async def _get(*args, **kwargs):
        reads.append((args, kwargs))
        return None

    monkeypatch.setattr(task_pool.redis_service, "get", _get)

    with pytest.raises(ValueError, match="run_id must not contain"):
        await task_pool.read_task(run_id="run:bad", task_id="task-1")

    with pytest.raises(ValueError, match="task_id must not contain"):
        await task_pool.read_task(run_id="run-1", task_id="task*bad")

    assert reads == []


@pytest.mark.asyncio
async def test_lease_mutations_reject_unsafe_identity_before_lua(monkeypatch) -> None:
    evals = []

    async def _eval_script(*args, **kwargs):
        evals.append((args, kwargs))
        return 1

    monkeypatch.setattr(task_pool.redis_service, "eval_script", _eval_script)

    with pytest.raises(ValueError, match="task_id must not contain"):
        await task_pool.claim_task(
            run_id="run-1",
            task_id="task:bad",
            agent_name="reviewer",
            lease_token="lease-1",
            lease_expires_at="2026-06-02T00:20:00Z",
            updated_at="2026-06-02T00:00:00Z",
        )

    with pytest.raises(ValueError, match="run_id must not contain"):
        await task_pool.complete_task(
            run_id="run*bad",
            task_id="task-1",
            owner_agent="reviewer",
            lease_token="lease-1",
            result_ref="result://task-1",
            updated_at="2026-06-02T00:01:00Z",
        )

    with pytest.raises(ValueError, match="task_id must not contain"):
        await task_pool.fail_task(
            run_id="run-1",
            task_id="task[bad]",
            owner_agent="reviewer",
            lease_token="lease-1",
            error_type="ValueError",
            error_message="bad",
            updated_at="2026-06-02T00:01:00Z",
        )

    assert evals == []
