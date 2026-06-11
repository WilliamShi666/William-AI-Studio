from __future__ import annotations

import asyncio
import json
import threading
from typing import Any

import pytest

from services import sandbox_create_capacity


def _install_stateful_create_capacity_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, Any]:
    leases: dict[str, dict[str, Any]] = {}
    start_state = {"next_available_at": 0.0}
    state_lock = threading.Lock()

    async def _stateful_eval(script, *, keys, args):
        with state_lock:
            now = float(args[-1])
            if script == sandbox_create_capacity._ACQUIRE_SANDBOX_CREATE_CAPACITY_LUA:
                assert keys == [
                    "sandbox_create_capacity:leases",
                    "sandbox_create_capacity:expiries",
                    "sandbox_create_capacity:meta",
                ]
                budget = int(args[0])
                lease_id = str(args[1])
                ttl_seconds = int(args[2])
                metadata_json = str(args[3])
                expired_ids = [
                    candidate_lease_id
                    for candidate_lease_id, payload in leases.items()
                    if float(payload["expires_at"]) <= now
                ]
                for expired_id in expired_ids:
                    del leases[expired_id]
                if lease_id in leases:
                    leases[lease_id]["expires_at"] = now + ttl_seconds
                    leases[lease_id]["metadata"] = json.loads(metadata_json)
                    return [2, len(leases), budget]
                if len(leases) >= budget:
                    return [0, len(leases), budget]
                leases[lease_id] = {
                    "expires_at": now + ttl_seconds,
                    "metadata": json.loads(metadata_json),
                }
                return [1, len(leases), budget]

            if script == sandbox_create_capacity._RELEASE_SANDBOX_CREATE_CAPACITY_LUA:
                assert keys == [
                    "sandbox_create_capacity:leases",
                    "sandbox_create_capacity:expiries",
                    "sandbox_create_capacity:meta",
                ]
                lease_id = str(args[0])
                released = 1 if lease_id in leases else 0
                leases.pop(lease_id, None)
                return [released, len(leases)]

            if script == sandbox_create_capacity._RESERVE_SANDBOX_CREATE_START_SLOT_LUA:
                assert keys == ["sandbox_create_capacity:start_window"]
                interval_seconds = float(args[0])
                if now >= float(start_state["next_available_at"]):
                    start_state["next_available_at"] = now + interval_seconds
                    return [1, 0]
                wait_ms = int(max(0.0, float(start_state["next_available_at"]) - now))
                return [0, wait_ms]

        raise AssertionError(f"Unexpected script: {script[:60]}")

    monkeypatch.setattr(sandbox_create_capacity.redis, "eval_script", _stateful_eval)
    return {
        "leases": leases,
        "start_state": start_state,
    }


def test_get_sandbox_create_concurrency_budget_is_disabled_without_explicit_env(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.delenv("SANDBOX_CREATE_MAX_CONCURRENT_GLOBAL", raising=False)

    assert sandbox_create_capacity.get_sandbox_create_concurrency_budget() == 0


def test_get_sandbox_create_concurrency_budget_reads_explicit_env(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("SANDBOX_CREATE_MAX_CONCURRENT_GLOBAL", "7")

    assert sandbox_create_capacity.get_sandbox_create_concurrency_budget() == 7


@pytest.mark.asyncio
async def test_try_acquire_and_release_sandbox_create_capacity_lease(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("SANDBOX_CREATE_MAX_CONCURRENT_GLOBAL", "2")
    monkeypatch.setenv("SANDBOX_CREATE_LEASE_TTL_SECONDS", "45")
    _install_stateful_create_capacity_backend(monkeypatch)
    now = {"value": 1234.0}
    monkeypatch.setattr(sandbox_create_capacity.time, "time", lambda: now["value"])

    first = await sandbox_create_capacity.try_acquire_sandbox_create_capacity_lease(
        holder_kind="regular",
        project_id="project-1",
        sandbox_type="code",
    )
    second = await sandbox_create_capacity.try_acquire_sandbox_create_capacity_lease(
        holder_kind="regular",
        project_id="project-2",
        sandbox_type="code",
    )
    denied = await sandbox_create_capacity.try_acquire_sandbox_create_capacity_lease(
        holder_kind="regular",
        project_id="project-3",
        sandbox_type="code",
    )

    assert first["acquired"] is True
    assert second["acquired"] is True
    assert denied["acquired"] is False
    assert denied["in_use"] == 2
    assert denied["budget"] == 2

    release_result = await sandbox_create_capacity.release_sandbox_create_capacity_lease(
        lease_id=str(first["lease_id"]),
    )

    assert release_result["released"] is True
    assert release_result["in_use"] == 1


@pytest.mark.asyncio
async def test_acquire_sandbox_create_capacity_times_out_when_budget_stays_full(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("SANDBOX_CREATE_MAX_CONCURRENT_GLOBAL", "1")

    async def _always_denied(*_args, **_kwargs):
        return {"acquired": False, "in_use": 1, "budget": 1, "lease_id": None}

    async def _fake_sleep(_seconds: float):
        return None

    monkeypatch.setattr(
        sandbox_create_capacity,
        "try_acquire_sandbox_create_capacity_lease",
        _always_denied,
    )
    monkeypatch.setattr(sandbox_create_capacity.asyncio, "sleep", _fake_sleep)

    with pytest.raises(sandbox_create_capacity.SandboxCreateCapacityTimeoutError):
        async with sandbox_create_capacity.acquire_sandbox_create_capacity(
            holder_kind="regular",
            project_id="project-1",
            sandbox_type="code",
            queue_timeout_seconds=0.05,
        ):
            raise AssertionError("context manager should not yield when timed out")


@pytest.mark.asyncio
async def test_wait_for_sandbox_create_start_slot_serializes_burst_attempts(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("SANDBOX_CREATE_MIN_START_INTERVAL_SECONDS", "0.5")
    _install_stateful_create_capacity_backend(monkeypatch)
    now = {"value": 1000.0}
    sleep_calls: list[float] = []

    async def _fake_sleep(seconds: float):
        sleep_calls.append(seconds)
        now["value"] += seconds

    monkeypatch.setattr(sandbox_create_capacity.time, "time", lambda: now["value"])
    monkeypatch.setattr(
        sandbox_create_capacity.time,
        "monotonic",
        lambda: now["value"],
    )
    monkeypatch.setattr(sandbox_create_capacity.asyncio, "sleep", _fake_sleep)

    first_wait = await sandbox_create_capacity.wait_for_sandbox_create_start_slot(
        project_id="project-1",
        sandbox_type="code",
        queue_timeout_seconds=2.0,
    )
    second_wait = await sandbox_create_capacity.wait_for_sandbox_create_start_slot(
        project_id="project-2",
        sandbox_type="code",
        queue_timeout_seconds=2.0,
    )

    assert first_wait == 0.0
    assert second_wait >= 0.5
    assert sleep_calls and sleep_calls[0] >= 0.5
