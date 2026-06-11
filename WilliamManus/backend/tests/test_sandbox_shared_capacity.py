from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import threading
import time
from typing import Any, Callable

import pytest

from sandbox import api as sandbox_api
import sandbox.tool_base as tool_base_module
from sandbox.tool_base import SandboxToolsBase
from services import sandbox_capacity


def _run_coro_in_thread(
    coro_factory: Callable[[], Any],
    *,
    errors: list[BaseException],
) -> threading.Thread:
    def _target() -> None:
        try:
            asyncio.run(coro_factory())
        except BaseException as exc:  # pragma: no cover - assertion path
            errors.append(exc)

    thread = threading.Thread(target=_target, daemon=True)
    thread.start()
    return thread


def _join_thread(thread: threading.Thread, *, timeout: float = 2.0) -> None:
    thread.join(timeout)
    assert not thread.is_alive(), "worker thread did not finish"


def _reset_sandbox_api_capacity_state(
    monkeypatch: pytest.MonkeyPatch,
    *,
    global_limit: int,
    per_sandbox_limit: int,
    queue_timeout: float = 0.15,
) -> None:
    monkeypatch.setattr(sandbox_api, "SANDBOX_IO_MAX_CONCURRENT_GLOBAL", global_limit)
    monkeypatch.setattr(
        sandbox_api,
        "SANDBOX_IO_MAX_CONCURRENT_PER_SANDBOX",
        per_sandbox_limit,
    )
    monkeypatch.setattr(sandbox_api, "SANDBOX_IO_QUEUE_TIMEOUT_SECONDS", queue_timeout)
    monkeypatch.setattr(sandbox_api, "_sandbox_global_io_semaphore", None, raising=False)
    monkeypatch.setattr(sandbox_api, "_sandbox_io_semaphores", {}, raising=False)
    monkeypatch.setattr(
        sandbox_api,
        "_sandbox_io_semaphores_lock",
        threading.Lock(),
        raising=False,
    )


def _reset_tool_base_capacity_state(
    monkeypatch: pytest.MonkeyPatch,
    *,
    global_limit: int,
    per_sandbox_limit: int,
    queue_timeout: float = 0.15,
) -> None:
    monkeypatch.setattr(tool_base_module, "SANDBOX_TOOL_MAX_CONCURRENT_GLOBAL", global_limit)
    monkeypatch.setattr(
        tool_base_module,
        "SANDBOX_TOOL_MAX_CONCURRENT_PER_SANDBOX",
        per_sandbox_limit,
    )
    monkeypatch.setattr(
        tool_base_module,
        "SANDBOX_TOOL_QUEUE_TIMEOUT_SECONDS",
        queue_timeout,
        raising=False,
    )
    monkeypatch.setattr(tool_base_module, "_tool_global_semaphore", None, raising=False)
    monkeypatch.setattr(tool_base_module, "_tool_per_sandbox_semaphores", {}, raising=False)
    monkeypatch.setattr(
        tool_base_module,
        "_tool_per_sandbox_lock",
        threading.Lock(),
        raising=False,
    )


async def _hold_sandbox_api_capacity(
    sandbox_id: str,
    acquired_event: threading.Event,
    release_event: threading.Event,
) -> None:
    async with sandbox_api._sandbox_io_capacity_guard(sandbox_id, "download"):
        acquired_event.set()
        deadline = time.monotonic() + 1.0
        while not release_event.is_set() and time.monotonic() < deadline:
            await asyncio.sleep(0.01)


def _build_tool(project_id: str, sandbox_id: str) -> SandboxToolsBase:
    tool = SandboxToolsBase(project_id=project_id)
    tool._bind_runtime_state(sandbox_id=sandbox_id)
    return tool


def _inline_tool_to_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _run_inline(func, /, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr(tool_base_module.asyncio, "to_thread", _run_inline)


async def _run_tool_call(
    tool: SandboxToolsBase,
    entered_event: threading.Event,
    release_event: threading.Event | None,
    *,
    timeout_seconds: float = 1.0,
) -> None:
    def _blocking_call() -> str:
        entered_event.set()
        if release_event is not None:
            deadline = time.monotonic() + 1.0
            while not release_event.is_set() and time.monotonic() < deadline:
                time.sleep(0.01)
        return "ok"

    await tool._run_blocking_sandbox_call_once(
        "tool-op",
        _blocking_call,
        timeout_seconds=timeout_seconds,
        attempt_tag="test",
    )


def _install_stateful_shared_capacity_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> dict[str, dict[str, Any]]:
    global_leases: dict[str, dict[str, Any]] = {}
    per_sandbox_leases: dict[str, dict[str, Any]] = {}
    state_lock = threading.Lock()

    async def _stateful_eval(script, *, keys, args):
        with state_lock:
            now = int(args[-1])
            if script == sandbox_capacity._PRUNE_EXPIRED_LEASES_LUA:
                assert keys == [
                    "sandbox_global_capacity:leases",
                    "sandbox_global_capacity:expiries",
                    "sandbox_global_capacity:meta",
                ]
                expired_ids = [
                    lease_id
                    for lease_id, payload in global_leases.items()
                    if int(payload["expires_at"]) <= now
                ]
                for expired_id in expired_ids:
                    del global_leases[expired_id]
                return len(global_leases)

            if script == sandbox_capacity._ACQUIRE_SANDBOX_CAPACITY_LUA:
                assert keys == [
                    "sandbox_global_capacity:leases",
                    "sandbox_global_capacity:expiries",
                    "sandbox_global_capacity:meta",
                ]
                budget = int(args[0])
                lease_id = args[1]
                ttl_seconds = int(args[2])
                metadata_json = args[3]
                expired_ids = [
                    expired_lease_id
                    for expired_lease_id, payload in global_leases.items()
                    if int(payload["expires_at"]) <= now
                ]
                for expired_id in expired_ids:
                    del global_leases[expired_id]
                if len(global_leases) >= budget:
                    return [0, len(global_leases), budget]
                global_leases[lease_id] = {
                    "expires_at": now + ttl_seconds,
                    "metadata": json.loads(metadata_json),
                }
                return [1, len(global_leases), budget]

            if script == sandbox_capacity._RELEASE_SANDBOX_CAPACITY_LUA:
                assert keys == [
                    "sandbox_global_capacity:leases",
                    "sandbox_global_capacity:expiries",
                    "sandbox_global_capacity:meta",
                ]
                lease_id = args[0]
                expired_ids = [
                    expired_lease_id
                    for expired_lease_id, payload in global_leases.items()
                    if int(payload["expires_at"]) <= now
                ]
                for expired_id in expired_ids:
                    del global_leases[expired_id]
                released = 1 if lease_id in global_leases else 0
                if released:
                    del global_leases[lease_id]
                return [released, len(global_leases)]

            if script == sandbox_capacity._PRUNE_SANDBOX_PER_SANDBOX_CAPACITY_LUA:
                assert keys == [
                    "sandbox_per_sandbox_capacity:leases",
                    "sandbox_per_sandbox_capacity:expiries",
                    "sandbox_per_sandbox_capacity:meta",
                    "sandbox_per_sandbox_capacity:counts",
                ]
                sandbox_id = str(args[0])
                expired_ids = [
                    lease_id
                    for lease_id, payload in per_sandbox_leases.items()
                    if int(payload["expires_at"]) <= now
                ]
                for expired_id in expired_ids:
                    del per_sandbox_leases[expired_id]
                in_use = sum(
                    1
                    for payload in per_sandbox_leases.values()
                    if payload["sandbox_id"] == sandbox_id
                )
                return in_use

            if script == sandbox_capacity._ACQUIRE_SANDBOX_PER_SANDBOX_CAPACITY_LUA:
                assert keys == [
                    "sandbox_per_sandbox_capacity:leases",
                    "sandbox_per_sandbox_capacity:expiries",
                    "sandbox_per_sandbox_capacity:meta",
                    "sandbox_per_sandbox_capacity:counts",
                ]
                budget = int(args[0])
                lease_id = args[1]
                sandbox_id = args[2]
                ttl_seconds = int(args[3])
                metadata_json = args[4]
                expired_ids = [
                    expired_lease_id
                    for expired_lease_id, payload in per_sandbox_leases.items()
                    if int(payload["expires_at"]) <= now
                ]
                for expired_id in expired_ids:
                    del per_sandbox_leases[expired_id]
                current_in_use = sum(
                    1
                    for payload in per_sandbox_leases.values()
                    if payload["sandbox_id"] == sandbox_id
                )
                if current_in_use >= budget:
                    return [0, current_in_use, budget]
                per_sandbox_leases[lease_id] = {
                    "expires_at": now + ttl_seconds,
                    "sandbox_id": sandbox_id,
                    "metadata": json.loads(metadata_json),
                }
                return [1, current_in_use + 1, budget]

            if script == sandbox_capacity._RELEASE_SANDBOX_PER_SANDBOX_CAPACITY_LUA:
                assert keys == [
                    "sandbox_per_sandbox_capacity:leases",
                    "sandbox_per_sandbox_capacity:expiries",
                    "sandbox_per_sandbox_capacity:meta",
                    "sandbox_per_sandbox_capacity:counts",
                ]
                lease_id = args[0]
                sandbox_id = args[1]
                expired_ids = [
                    expired_lease_id
                    for expired_lease_id, payload in per_sandbox_leases.items()
                    if int(payload["expires_at"]) <= now
                ]
                for expired_id in expired_ids:
                    del per_sandbox_leases[expired_id]
                released = 0
                payload = per_sandbox_leases.get(lease_id)
                if payload and payload["sandbox_id"] == sandbox_id:
                    del per_sandbox_leases[lease_id]
                    released = 1
                current_in_use = sum(
                    1
                    for current_payload in per_sandbox_leases.values()
                    if current_payload["sandbox_id"] == sandbox_id
                )
                return [released, current_in_use]

        raise AssertionError(f"Unexpected script: {script[:40]}")

    monkeypatch.setattr(sandbox_capacity.redis, "eval_script", _stateful_eval)
    return {
        "global": global_leases,
        "per_sandbox": per_sandbox_leases,
    }


def test_get_sandbox_global_concurrency_budget_prefers_explicit_env(monkeypatch):
    monkeypatch.setenv("SANDBOX_SHARED_MAX_CONCURRENT_GLOBAL", "37")
    monkeypatch.setenv("SANDBOX_IO_MAX_CONCURRENT_GLOBAL", "48")
    monkeypatch.setenv("SANDBOX_TOOL_MAX_CONCURRENT_GLOBAL", "32")

    assert sandbox_capacity.get_sandbox_global_concurrency_budget() == 37


def test_get_sandbox_global_concurrency_budget_is_disabled_without_explicit_env(monkeypatch):
    monkeypatch.delenv("SANDBOX_SHARED_MAX_CONCURRENT_GLOBAL", raising=False)
    monkeypatch.setenv("SANDBOX_IO_MAX_CONCURRENT_GLOBAL", "48")
    monkeypatch.setenv("SANDBOX_TOOL_MAX_CONCURRENT_GLOBAL", "32")

    assert sandbox_capacity.get_sandbox_global_concurrency_budget() == 0


@pytest.mark.asyncio
async def test_try_acquire_sandbox_global_capacity_lease_parses_acquired_eval_result(monkeypatch):
    monkeypatch.setenv("SANDBOX_SHARED_MAX_CONCURRENT_GLOBAL", "7")
    monkeypatch.setenv("SANDBOX_SHARED_LEASE_TTL_SECONDS", "45")
    monkeypatch.setattr(sandbox_capacity.time, "time", lambda: 1234)

    captured: dict[str, Any] = {}

    async def _fake_eval_script(*_args, **kwargs):
        captured["keys"] = list(kwargs.get("keys") or [])
        captured["args"] = list(kwargs.get("args") or [])
        return [1, 5, 7]

    monkeypatch.setattr(sandbox_capacity.redis, "eval_script", _fake_eval_script)

    result = await sandbox_capacity.try_acquire_sandbox_global_capacity_lease(
        lease_id="lease-1",
        holder_kind="tool_call",
        sandbox_id="sb-1",
        operation="read_file",
    )

    assert result == {
        "enabled": True,
        "budget": 7,
        "in_use": 5,
        "remaining": 2,
        "acquired": True,
        "lease_ttl_seconds": 45,
    }
    assert captured["keys"] == [
        "sandbox_global_capacity:leases",
        "sandbox_global_capacity:expiries",
        "sandbox_global_capacity:meta",
    ]
    assert captured["args"][:3] == ["7", "lease-1", "45"]
    assert captured["args"][4] == "1234"
    assert json.loads(captured["args"][3]) == {
        "holder_kind": "tool_call",
        "sandbox_id": "sb-1",
        "operation": "read_file",
    }


@pytest.mark.asyncio
async def test_try_acquire_sandbox_global_capacity_lease_uses_explicit_ttl_override(monkeypatch):
    monkeypatch.setenv("SANDBOX_SHARED_MAX_CONCURRENT_GLOBAL", "7")
    monkeypatch.setenv("SANDBOX_SHARED_LEASE_TTL_SECONDS", "45")
    monkeypatch.setattr(sandbox_capacity.time, "time", lambda: 1234)

    captured: dict[str, Any] = {}

    async def _fake_eval_script(*_args, **kwargs):
        captured["args"] = list(kwargs.get("args") or [])
        return [1, 1, 7]

    monkeypatch.setattr(sandbox_capacity.redis, "eval_script", _fake_eval_script)

    await sandbox_capacity.try_acquire_sandbox_global_capacity_lease(
        lease_id="lease-ttl-override",
        holder_kind="tool_call",
        sandbox_id="sb-ttl",
        operation="bootstrap",
        lease_ttl_seconds=305,
    )

    assert captured["args"][2] == "305"


@pytest.mark.asyncio
async def test_release_sandbox_global_capacity_lease_parses_release_eval_result(monkeypatch):
    monkeypatch.setenv("SANDBOX_SHARED_MAX_CONCURRENT_GLOBAL", "7")
    monkeypatch.setattr(sandbox_capacity.time, "time", lambda: 1234)

    async def _fake_eval_script(*_args, **_kwargs):
        return [1, 4]

    monkeypatch.setattr(sandbox_capacity.redis, "eval_script", _fake_eval_script)

    result = await sandbox_capacity.release_sandbox_global_capacity_lease(
        lease_id="lease-1",
    )

    assert result == {
        "enabled": True,
        "budget": 7,
        "in_use": 4,
        "remaining": 3,
        "released": True,
    }


@pytest.mark.asyncio
async def test_acquire_sandbox_global_capacity_times_out_when_budget_stays_full(monkeypatch):
    monkeypatch.setenv("SANDBOX_SHARED_MAX_CONCURRENT_GLOBAL", "1")
    monkeypatch.setenv("SANDBOX_SHARED_LEASE_TTL_SECONDS", "120")
    _install_stateful_shared_capacity_backend(monkeypatch)

    holder_started = asyncio.Event()
    holder_release = asyncio.Event()

    async def _holder() -> None:
        async with sandbox_capacity.acquire_sandbox_global_capacity(
            holder_kind="sandbox_io",
            sandbox_id="sb-holder",
            operation="download",
            queue_timeout_seconds=0.5,
        ):
            holder_started.set()
            await holder_release.wait()

    holder_task = asyncio.create_task(_holder())
    await asyncio.wait_for(holder_started.wait(), timeout=1.0)

    with pytest.raises(sandbox_capacity.SandboxGlobalCapacityTimeoutError):
        async with sandbox_capacity.acquire_sandbox_global_capacity(
            holder_kind="tool_call",
            sandbox_id="sb-contender",
            operation="read_file",
            queue_timeout_seconds=0.15,
        ):
            raise AssertionError("shared capacity timeout should block the contender")

    holder_release.set()
    await asyncio.wait_for(holder_task, timeout=1.0)


def test_sandbox_api_and_tool_calls_share_global_capacity_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SANDBOX_SHARED_MAX_CONCURRENT_GLOBAL", "1")
    monkeypatch.setenv("SANDBOX_SHARED_LEASE_TTL_SECONDS", "120")
    _install_stateful_shared_capacity_backend(monkeypatch)
    _inline_tool_to_thread(monkeypatch)
    _reset_sandbox_api_capacity_state(
        monkeypatch,
        global_limit=2,
        per_sandbox_limit=2,
        queue_timeout=0.5,
    )
    _reset_tool_base_capacity_state(
        monkeypatch,
        global_limit=2,
        per_sandbox_limit=2,
        queue_timeout=0.5,
    )

    holder_acquired = threading.Event()
    holder_release = threading.Event()
    tool_entered = threading.Event()
    holder_errors: list[BaseException] = []
    contender_errors: list[BaseException] = []

    holder_thread = _run_coro_in_thread(
        lambda: _hold_sandbox_api_capacity("shared-sb-api", holder_acquired, holder_release),
        errors=holder_errors,
    )
    assert holder_acquired.wait(1.0), f"API holder did not acquire shared capacity; errors={holder_errors!r}"

    tool = _build_tool("project-shared", "shared-sb-tool")
    contender_thread = _run_coro_in_thread(
        lambda: _run_tool_call(tool, tool_entered, None),
        errors=contender_errors,
    )
    assert not tool_entered.wait(0.15), "tool contender should be blocked by shared global capacity"

    holder_release.set()
    _join_thread(holder_thread)
    _join_thread(contender_thread)
    assert tool_entered.is_set(), "tool contender never entered after shared capacity was released"
    assert holder_errors == []
    assert contender_errors == []


def test_tool_calls_force_api_queue_timeout_when_shared_budget_is_exhausted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SANDBOX_SHARED_MAX_CONCURRENT_GLOBAL", "1")
    monkeypatch.setenv("SANDBOX_SHARED_LEASE_TTL_SECONDS", "120")
    _install_stateful_shared_capacity_backend(monkeypatch)
    _inline_tool_to_thread(monkeypatch)
    _reset_sandbox_api_capacity_state(
        monkeypatch,
        global_limit=2,
        per_sandbox_limit=2,
        queue_timeout=0.15,
    )
    _reset_tool_base_capacity_state(
        monkeypatch,
        global_limit=2,
        per_sandbox_limit=2,
        queue_timeout=0.15,
    )

    tool = _build_tool("project-holder", "holder-sb")
    holder_entered = threading.Event()
    holder_release = threading.Event()
    holder_errors: list[BaseException] = []
    holder_thread = _run_coro_in_thread(
        lambda: _run_tool_call(tool, holder_entered, holder_release),
        errors=holder_errors,
    )
    assert holder_entered.wait(1.0), f"tool holder did not acquire shared capacity; errors={holder_errors!r}"

    async def _api_contender() -> None:
        with pytest.raises(sandbox_api.SandboxIOQueueTimeoutError):
            async with sandbox_api._sandbox_io_capacity_guard("api-sb", "download"):
                raise AssertionError("API contender should time out on shared global capacity")

    asyncio.run(_api_contender())

    holder_release.set()
    _join_thread(holder_thread)
    assert holder_errors == []


def test_tool_call_extends_shared_capacity_ttl_for_long_operations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SANDBOX_SHARED_MAX_CONCURRENT_GLOBAL", "1")
    monkeypatch.setenv("SANDBOX_SHARED_LEASE_TTL_SECONDS", "120")
    _inline_tool_to_thread(monkeypatch)
    _reset_tool_base_capacity_state(
        monkeypatch,
        global_limit=2,
        per_sandbox_limit=2,
        queue_timeout=3.0,
    )

    captured: dict[str, Any] = {}

    async def _capturing_eval(script, *, keys, args):
        if script == sandbox_capacity._ACQUIRE_SANDBOX_CAPACITY_LUA:
            captured["keys"] = list(keys)
            captured["args"] = list(args)
            return [1, 1, 1]
        if script == sandbox_capacity._RELEASE_SANDBOX_CAPACITY_LUA:
            return [1, 0]
        raise AssertionError(f"Unexpected script: {script[:40]}")

    monkeypatch.setattr(sandbox_capacity.redis, "eval_script", _capturing_eval)

    tool = _build_tool("project-long-ttl", "tool-long-ttl")
    entered_event = threading.Event()
    asyncio.run(_run_tool_call(tool, entered_event, None, timeout_seconds=300.0))

    assert entered_event.is_set()
    assert captured["keys"] == [
        "sandbox_global_capacity:leases",
        "sandbox_global_capacity:expiries",
        "sandbox_global_capacity:meta",
    ]
    assert int(captured["args"][2]) >= 313


@pytest.mark.asyncio
async def test_try_acquire_sandbox_per_sandbox_capacity_lease_reclaims_expired_leases(monkeypatch):
    monkeypatch.setenv("SANDBOX_SHARED_MAX_CONCURRENT_PER_SANDBOX", "1")
    monkeypatch.setenv("SANDBOX_SHARED_LEASE_TTL_SECONDS", "30")
    _install_stateful_shared_capacity_backend(monkeypatch)

    now = {"value": 1000}
    monkeypatch.setattr(sandbox_capacity.time, "time", lambda: now["value"])

    first = await sandbox_capacity.try_acquire_sandbox_per_sandbox_capacity_lease(
        lease_id="per-sandbox-lease-1",
        holder_kind="tool_call",
        sandbox_id="shared-sb",
        operation="read_file",
    )
    denied = await sandbox_capacity.try_acquire_sandbox_per_sandbox_capacity_lease(
        lease_id="per-sandbox-lease-2",
        holder_kind="tool_call",
        sandbox_id="shared-sb",
        operation="read_file",
    )

    now["value"] = 1031
    recovered = await sandbox_capacity.try_acquire_sandbox_per_sandbox_capacity_lease(
        lease_id="per-sandbox-lease-3",
        holder_kind="tool_call",
        sandbox_id="shared-sb",
        operation="read_file",
    )

    assert first["acquired"] is True
    assert denied["acquired"] is False
    assert recovered["acquired"] is True
    assert recovered["in_use"] == 1
    assert recovered["remaining"] == 0


def test_sandbox_api_and_tool_calls_share_per_sandbox_capacity_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SANDBOX_SHARED_MAX_CONCURRENT_GLOBAL", "2")
    monkeypatch.setenv("SANDBOX_SHARED_MAX_CONCURRENT_PER_SANDBOX", "1")
    monkeypatch.setenv("SANDBOX_SHARED_LEASE_TTL_SECONDS", "120")
    _install_stateful_shared_capacity_backend(monkeypatch)
    _inline_tool_to_thread(monkeypatch)
    _reset_sandbox_api_capacity_state(
        monkeypatch,
        global_limit=2,
        per_sandbox_limit=2,
        queue_timeout=0.5,
    )
    _reset_tool_base_capacity_state(
        monkeypatch,
        global_limit=2,
        per_sandbox_limit=2,
        queue_timeout=0.5,
    )

    holder_acquired = threading.Event()
    holder_release = threading.Event()
    tool_entered = threading.Event()
    holder_errors: list[BaseException] = []
    contender_errors: list[BaseException] = []

    holder_thread = _run_coro_in_thread(
        lambda: _hold_sandbox_api_capacity("shared-per-sandbox", holder_acquired, holder_release),
        errors=holder_errors,
    )
    assert holder_acquired.wait(1.0), f"API holder did not acquire shared per-sandbox capacity; errors={holder_errors!r}"

    tool = _build_tool("project-shared-per-sandbox", "shared-per-sandbox")
    contender_thread = _run_coro_in_thread(
        lambda: _run_tool_call(tool, tool_entered, None),
        errors=contender_errors,
    )
    assert not tool_entered.wait(0.15), "tool contender should be blocked by shared per-sandbox capacity"

    holder_release.set()
    _join_thread(holder_thread)
    _join_thread(contender_thread)
    assert tool_entered.is_set(), "tool contender never entered after shared per-sandbox capacity was released"
    assert holder_errors == []
    assert contender_errors == []


def test_sandbox_api_falls_back_to_process_local_global_limiter_when_shared_backend_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SANDBOX_SHARED_MAX_CONCURRENT_GLOBAL", "1")
    _reset_sandbox_api_capacity_state(
        monkeypatch,
        global_limit=1,
        per_sandbox_limit=2,
        queue_timeout=0.15,
    )

    @asynccontextmanager
    async def _broken_shared_global(*args, **kwargs):
        raise sandbox_capacity.SandboxGlobalCapacityBackendUnavailableError("redis down")
        yield

    monkeypatch.setattr(
        sandbox_capacity,
        "acquire_sandbox_global_capacity",
        _broken_shared_global,
    )

    holder_acquired = threading.Event()
    holder_release = threading.Event()
    holder_errors: list[BaseException] = []

    holder_thread = _run_coro_in_thread(
        lambda: _hold_sandbox_api_capacity("sb-fallback-global-1", holder_acquired, holder_release),
        errors=holder_errors,
    )
    assert holder_acquired.wait(1.0), (
        f"holder did not acquire fallback global capacity; errors={holder_errors!r}"
    )

    async def _contender() -> None:
        with pytest.raises(sandbox_api.SandboxIOQueueTimeoutError):
            async with sandbox_api._sandbox_io_capacity_guard(
                "sb-fallback-global-2",
                "download",
            ):
                raise AssertionError("fallback global limiter should block the second loop")

    asyncio.run(_contender())

    holder_release.set()
    _join_thread(holder_thread)
    assert holder_errors == []


def test_sandbox_api_falls_back_to_process_local_per_sandbox_limiter_when_shared_backend_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SANDBOX_SHARED_MAX_CONCURRENT_PER_SANDBOX", "1")
    _reset_sandbox_api_capacity_state(
        monkeypatch,
        global_limit=2,
        per_sandbox_limit=1,
        queue_timeout=0.15,
    )

    @asynccontextmanager
    async def _broken_shared_per_sandbox(*args, **kwargs):
        raise sandbox_capacity.SandboxPerSandboxCapacityBackendUnavailableError("redis down")
        yield

    monkeypatch.setattr(
        sandbox_capacity,
        "acquire_sandbox_per_sandbox_capacity",
        _broken_shared_per_sandbox,
    )

    holder_acquired = threading.Event()
    holder_release = threading.Event()
    holder_errors: list[BaseException] = []

    holder_thread = _run_coro_in_thread(
        lambda: _hold_sandbox_api_capacity("sb-fallback-shared", holder_acquired, holder_release),
        errors=holder_errors,
    )
    assert holder_acquired.wait(1.0), (
        f"holder did not acquire fallback per-sandbox capacity; errors={holder_errors!r}"
    )

    async def _same_sandbox_contender() -> None:
        with pytest.raises(sandbox_api.SandboxIOQueueTimeoutError):
            async with sandbox_api._sandbox_io_capacity_guard(
                "sb-fallback-shared",
                "download",
            ):
                raise AssertionError("fallback per-sandbox limiter should exhaust same sandbox capacity")

    async def _other_sandbox_contender() -> None:
        async with sandbox_api._sandbox_io_capacity_guard("sb-fallback-other", "download"):
            return None

    asyncio.run(_same_sandbox_contender())
    asyncio.run(_other_sandbox_contender())

    holder_release.set()
    _join_thread(holder_thread)
    assert holder_errors == []


def test_tool_calls_fall_back_to_process_local_global_limiter_when_shared_backend_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SANDBOX_SHARED_MAX_CONCURRENT_GLOBAL", "1")
    _inline_tool_to_thread(monkeypatch)
    _reset_tool_base_capacity_state(
        monkeypatch,
        global_limit=1,
        per_sandbox_limit=2,
        queue_timeout=0.5,
    )

    @asynccontextmanager
    async def _broken_shared_global(*args, **kwargs):
        raise sandbox_capacity.SandboxGlobalCapacityBackendUnavailableError("redis down")
        yield

    monkeypatch.setattr(
        sandbox_capacity,
        "acquire_sandbox_global_capacity",
        _broken_shared_global,
    )

    tool_one = _build_tool("project-fallback-global-1", "tool-fallback-global-1")
    tool_two = _build_tool("project-fallback-global-2", "tool-fallback-global-2")

    first_entered = threading.Event()
    first_release = threading.Event()
    second_entered = threading.Event()
    errors: list[BaseException] = []

    holder_thread = _run_coro_in_thread(
        lambda: _run_tool_call(tool_one, first_entered, first_release),
        errors=errors,
    )
    assert first_entered.wait(1.0), (
        f"holder did not acquire fallback tool global capacity; errors={errors!r}"
    )

    contender_thread = _run_coro_in_thread(
        lambda: _run_tool_call(tool_two, second_entered, None),
        errors=errors,
    )
    assert not second_entered.wait(0.15), (
        "fallback tool global limiter should block the second loop"
    )

    first_release.set()
    _join_thread(holder_thread)
    _join_thread(contender_thread)
    assert second_entered.is_set(), "second tool call never entered after fallback release"
    assert errors == []


def test_tool_calls_fall_back_to_process_local_per_sandbox_limiter_when_shared_backend_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SANDBOX_SHARED_MAX_CONCURRENT_PER_SANDBOX", "1")
    _inline_tool_to_thread(monkeypatch)
    _reset_tool_base_capacity_state(
        monkeypatch,
        global_limit=2,
        per_sandbox_limit=1,
        queue_timeout=0.5,
    )

    @asynccontextmanager
    async def _broken_shared_per_sandbox(*args, **kwargs):
        raise sandbox_capacity.SandboxPerSandboxCapacityBackendUnavailableError("redis down")
        yield

    monkeypatch.setattr(
        sandbox_capacity,
        "acquire_sandbox_per_sandbox_capacity",
        _broken_shared_per_sandbox,
    )

    shared_holder = _build_tool("project-fallback-shared-1", "tool-fallback-shared")
    shared_contender = _build_tool("project-fallback-shared-2", "tool-fallback-shared")
    other_sandbox_tool = _build_tool("project-fallback-other", "tool-fallback-other")

    holder_entered = threading.Event()
    holder_release = threading.Event()
    same_entered = threading.Event()
    other_entered = threading.Event()
    errors: list[BaseException] = []

    holder_thread = _run_coro_in_thread(
        lambda: _run_tool_call(shared_holder, holder_entered, holder_release),
        errors=errors,
    )
    assert holder_entered.wait(1.0), (
        f"holder did not acquire fallback per-sandbox tool capacity; errors={errors!r}"
    )

    same_thread = _run_coro_in_thread(
        lambda: _run_tool_call(shared_contender, same_entered, None),
        errors=errors,
    )
    other_thread = _run_coro_in_thread(
        lambda: _run_tool_call(other_sandbox_tool, other_entered, None),
        errors=errors,
    )
    assert not same_entered.wait(0.15), (
        "fallback per-sandbox tool limiter should block the same sandbox"
    )
    assert other_entered.wait(1.0), "different sandbox should not be blocked by fallback limiter"

    holder_release.set()
    _join_thread(holder_thread)
    _join_thread(same_thread)
    _join_thread(other_thread)
    assert same_entered.is_set(), "same sandbox tool call never entered after fallback release"
    assert errors == []


@pytest.mark.asyncio
async def test_sandbox_api_timeout_retains_shared_global_capacity_until_ttl_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SANDBOX_SHARED_MAX_CONCURRENT_GLOBAL", "1")
    monkeypatch.delenv("SANDBOX_SHARED_MAX_CONCURRENT_PER_SANDBOX", raising=False)
    monkeypatch.setenv("SANDBOX_SHARED_LEASE_TTL_SECONDS", "120")
    now = {"value": 1000}
    monkeypatch.setattr(sandbox_capacity.time, "time", lambda: now["value"])
    state = _install_stateful_shared_capacity_backend(monkeypatch)
    _reset_sandbox_api_capacity_state(
        monkeypatch,
        global_limit=2,
        per_sandbox_limit=2,
        queue_timeout=0.1,
    )
    detached_threads: list[threading.Thread] = []

    entered_event = threading.Event()
    release_event = threading.Event()

    def _blocking_call() -> str:
        entered_event.set()
        deadline = time.monotonic() + 1.0
        while not release_event.is_set() and time.monotonic() < deadline:
            time.sleep(0.01)
        return "ok"

    async def _timeouting_run_blocking(
        operation: str,
        call: Callable[[], Any],
        *,
        timeout_seconds: float,
        sandbox_id: str,
        path: str | None = None,
    ) -> Any:
        assert operation == "download"
        assert sandbox_id == "sb-timeout-holder"
        thread = threading.Thread(target=call, daemon=True)
        detached_threads.append(thread)
        thread.start()
        await asyncio.sleep(0)
        raise sandbox_api.SandboxIOTimeoutError(
            operation,
            timeout_seconds,
            sandbox_id,
            path=path,
        )

    monkeypatch.setattr(
        sandbox_api,
        "_run_blocking_sandbox_call",
        _timeouting_run_blocking,
    )

    with pytest.raises(sandbox_api.SandboxIOTimeoutError):
        await sandbox_api._run_guarded_sandbox_io(
            "sb-timeout-holder",
            "download",
            _blocking_call,
            timeout_seconds=0.05,
        )

    assert entered_event.wait(1.0), "timed-out sandbox call never entered the blocking body"
    assert len(state["global"]) == 1

    with pytest.raises(sandbox_api.SandboxIOQueueTimeoutError):
        async with sandbox_api._sandbox_io_capacity_guard("sb-timeout-contender", "download"):
            raise AssertionError("contender should stay blocked while timed-out work is still active")

    release_event.set()
    for thread in detached_threads:
        thread.join(1.0)
        assert not thread.is_alive(), "detached sandbox I/O thread did not finish"

    now["value"] += 500
    acquire_result = await sandbox_capacity.try_acquire_sandbox_global_capacity_lease(
        lease_id="lease-cleanup-global",
        holder_kind="sandbox_io",
        sandbox_id="sb-cleanup",
        operation="download",
    )
    assert acquire_result["acquired"] is True
    assert len(state["global"]) == 1
    await sandbox_capacity.release_sandbox_global_capacity_lease(
        lease_id="lease-cleanup-global",
    )
    assert len(state["global"]) == 0


@pytest.mark.asyncio
async def test_tool_timeout_retains_shared_per_sandbox_capacity_until_ttl_expiry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SANDBOX_SHARED_MAX_CONCURRENT_GLOBAL", "2")
    monkeypatch.setenv("SANDBOX_SHARED_MAX_CONCURRENT_PER_SANDBOX", "1")
    monkeypatch.setenv("SANDBOX_SHARED_LEASE_TTL_SECONDS", "120")
    now = {"value": 1000}
    monkeypatch.setattr(sandbox_capacity.time, "time", lambda: now["value"])
    state = _install_stateful_shared_capacity_backend(monkeypatch)
    _reset_tool_base_capacity_state(
        monkeypatch,
        global_limit=2,
        per_sandbox_limit=2,
        queue_timeout=0.1,
    )
    detached_threads: list[threading.Thread] = []

    holder_tool = _build_tool("project-timeout-holder", "shared-timeout-sandbox")
    contender_tool = _build_tool("project-timeout-contender", "shared-timeout-sandbox")
    holder_entered = threading.Event()
    holder_release = threading.Event()

    class _DetachedToolAwaitable:
        def __init__(self, func: Callable[[], Any]) -> None:
            self.func = func

    def _fake_to_thread(func, /, *args, **kwargs):
        assert not args
        assert not kwargs
        return _DetachedToolAwaitable(func)

    async def _timeouting_wait_for(awaitable, timeout):
        assert timeout == 0.05
        assert isinstance(awaitable, _DetachedToolAwaitable)
        thread = threading.Thread(target=awaitable.func, daemon=True)
        detached_threads.append(thread)
        thread.start()
        await asyncio.sleep(0)
        raise asyncio.TimeoutError()

    monkeypatch.setattr(tool_base_module.asyncio, "to_thread", _fake_to_thread)
    monkeypatch.setattr(tool_base_module.asyncio, "wait_for", _timeouting_wait_for)

    with pytest.raises(TimeoutError):
        await _run_tool_call(
            holder_tool,
            holder_entered,
            holder_release,
            timeout_seconds=0.05,
        )

    assert holder_entered.wait(1.0), "timed-out tool call never entered the blocking body"
    assert len(state["per_sandbox"]) == 1

    with pytest.raises(tool_base_module.SandboxToolQueueTimeoutError):
        await _run_tool_call(
            contender_tool,
            threading.Event(),
            None,
            timeout_seconds=1.0,
        )

    holder_release.set()
    for thread in detached_threads:
        thread.join(1.0)
        assert not thread.is_alive(), "detached sandbox tool thread did not finish"

    now["value"] += 500
    acquire_result = await sandbox_capacity.try_acquire_sandbox_per_sandbox_capacity_lease(
        lease_id="lease-cleanup-per-sandbox",
        holder_kind="tool_call",
        sandbox_id="shared-timeout-sandbox",
        operation="tool-op",
    )
    assert acquire_result["acquired"] is True
    assert len(state["per_sandbox"]) == 1
    await sandbox_capacity.release_sandbox_per_sandbox_capacity_lease(
        lease_id="lease-cleanup-per-sandbox",
        sandbox_id="shared-timeout-sandbox",
    )
    assert len(state["per_sandbox"]) == 0


def test_shared_per_sandbox_capacity_does_not_block_different_sandbox_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SANDBOX_SHARED_MAX_CONCURRENT_GLOBAL", "2")
    monkeypatch.setenv("SANDBOX_SHARED_MAX_CONCURRENT_PER_SANDBOX", "1")
    monkeypatch.setenv("SANDBOX_SHARED_LEASE_TTL_SECONDS", "120")
    _install_stateful_shared_capacity_backend(monkeypatch)
    _inline_tool_to_thread(monkeypatch)
    _reset_sandbox_api_capacity_state(
        monkeypatch,
        global_limit=2,
        per_sandbox_limit=2,
        queue_timeout=0.5,
    )
    _reset_tool_base_capacity_state(
        monkeypatch,
        global_limit=2,
        per_sandbox_limit=2,
        queue_timeout=0.5,
    )

    holder_acquired = threading.Event()
    holder_release = threading.Event()
    tool_entered = threading.Event()
    holder_errors: list[BaseException] = []
    contender_errors: list[BaseException] = []

    holder_thread = _run_coro_in_thread(
        lambda: _hold_sandbox_api_capacity("sandbox-a", holder_acquired, holder_release),
        errors=holder_errors,
    )
    assert holder_acquired.wait(1.0), f"API holder did not acquire shared per-sandbox capacity; errors={holder_errors!r}"

    tool = _build_tool("project-sandbox-b", "sandbox-b")
    contender_thread = _run_coro_in_thread(
        lambda: _run_tool_call(tool, tool_entered, None),
        errors=contender_errors,
    )
    assert tool_entered.wait(1.0), f"tool contender for different sandbox did not enter; errors={contender_errors!r}"

    holder_release.set()
    _join_thread(holder_thread)
    _join_thread(contender_thread)
    assert holder_errors == []
    assert contender_errors == []
