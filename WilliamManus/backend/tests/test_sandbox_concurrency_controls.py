import asyncio
import threading
import time
from typing import Any, Callable

import pytest

from sandbox import api as sandbox_api
from sandbox.tool_base import SandboxToolsBase


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
    queue_timeout: float = 1.0,
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


import sandbox.tool_base as tool_base_module


def test_sandbox_io_global_capacity_is_process_global_across_event_loops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_sandbox_api_capacity_state(
        monkeypatch,
        global_limit=1,
        per_sandbox_limit=2,
    )

    holder_acquired = threading.Event()
    holder_release = threading.Event()
    holder_errors: list[BaseException] = []

    holder_thread = _run_coro_in_thread(
        lambda: _hold_sandbox_api_capacity("sb-global-1", holder_acquired, holder_release),
        errors=holder_errors,
    )
    assert holder_acquired.wait(1.0), f"holder did not acquire sandbox I/O capacity; errors={holder_errors!r}"

    async def _contender() -> None:
        with pytest.raises(sandbox_api.SandboxIOQueueTimeoutError):
            async with sandbox_api._sandbox_io_capacity_guard("sb-global-2", "download"):
                raise AssertionError("global capacity should block the second loop")

    asyncio.run(_contender())

    holder_release.set()
    _join_thread(holder_thread)
    assert holder_errors == []


def test_sandbox_io_per_sandbox_capacity_is_process_global_but_not_cross_sandbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _reset_sandbox_api_capacity_state(
        monkeypatch,
        global_limit=2,
        per_sandbox_limit=1,
    )

    holder_acquired = threading.Event()
    holder_release = threading.Event()
    holder_errors: list[BaseException] = []

    holder_thread = _run_coro_in_thread(
        lambda: _hold_sandbox_api_capacity("sb-shared", holder_acquired, holder_release),
        errors=holder_errors,
    )
    assert holder_acquired.wait(1.0), f"holder did not acquire per-sandbox capacity; errors={holder_errors!r}"

    async def _same_sandbox_contender() -> None:
        with pytest.raises(sandbox_api.SandboxIOQueueTimeoutError):
            async with sandbox_api._sandbox_io_capacity_guard("sb-shared", "download"):
                raise AssertionError("same sandbox capacity should be exhausted")

    async def _other_sandbox_contender() -> None:
        async with sandbox_api._sandbox_io_capacity_guard("sb-other", "download"):
            return None

    asyncio.run(_same_sandbox_contender())
    asyncio.run(_other_sandbox_contender())

    holder_release.set()
    _join_thread(holder_thread)
    assert holder_errors == []


def test_tool_call_global_capacity_is_process_global_across_event_loops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _inline_tool_to_thread(monkeypatch)
    _reset_tool_base_capacity_state(
        monkeypatch,
        global_limit=1,
        per_sandbox_limit=2,
    )

    tool_one = _build_tool("project-global-1", "tool-sb-1")
    tool_two = _build_tool("project-global-2", "tool-sb-2")

    first_entered = threading.Event()
    first_release = threading.Event()
    second_entered = threading.Event()
    errors: list[BaseException] = []

    holder_thread = _run_coro_in_thread(
        lambda: _run_tool_call(tool_one, first_entered, first_release),
        errors=errors,
    )
    assert first_entered.wait(1.0), f"holder did not start first tool call; errors={errors!r}"

    contender_thread = _run_coro_in_thread(
        lambda: _run_tool_call(tool_two, second_entered, None),
        errors=errors,
    )
    assert not second_entered.wait(0.15), "global tool capacity should block the second loop"

    first_release.set()
    _join_thread(holder_thread)
    _join_thread(contender_thread)
    assert second_entered.is_set(), "second tool call never entered after release"
    assert errors == []


def test_tool_call_per_sandbox_capacity_is_process_global_but_not_cross_sandbox(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _inline_tool_to_thread(monkeypatch)
    _reset_tool_base_capacity_state(
        monkeypatch,
        global_limit=2,
        per_sandbox_limit=1,
    )

    shared_holder = _build_tool("project-shared-1", "tool-shared")
    shared_contender = _build_tool("project-shared-2", "tool-shared")
    other_sandbox_tool = _build_tool("project-other", "tool-other")

    holder_entered = threading.Event()
    holder_release = threading.Event()
    same_entered = threading.Event()
    other_entered = threading.Event()
    errors: list[BaseException] = []

    holder_thread = _run_coro_in_thread(
        lambda: _run_tool_call(shared_holder, holder_entered, holder_release),
        errors=errors,
    )
    assert holder_entered.wait(1.0), f"holder did not start shared-sandbox call; errors={errors!r}"

    same_thread = _run_coro_in_thread(
        lambda: _run_tool_call(shared_contender, same_entered, None),
        errors=errors,
    )
    other_thread = _run_coro_in_thread(
        lambda: _run_tool_call(other_sandbox_tool, other_entered, None),
        errors=errors,
    )

    assert other_entered.wait(1.0), "other sandbox should not be blocked by per-sandbox limit"
    assert not same_entered.wait(0.15), "same sandbox should be blocked by per-sandbox limit"

    holder_release.set()
    _join_thread(holder_thread)
    _join_thread(same_thread)
    _join_thread(other_thread)
    assert same_entered.is_set(), "same-sandbox contender never entered after release"
    assert errors == []


def test_tool_call_queue_timeout_fails_fast_when_global_capacity_stays_saturated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _inline_tool_to_thread(monkeypatch)
    _reset_tool_base_capacity_state(
        monkeypatch,
        global_limit=1,
        per_sandbox_limit=2,
        queue_timeout=0.15,
    )

    tool_one = _build_tool("project-timeout-1", "tool-timeout-1")
    tool_two = _build_tool("project-timeout-2", "tool-timeout-2")

    holder_entered = threading.Event()
    holder_release = threading.Event()
    contender_entered = threading.Event()
    errors: list[BaseException] = []

    holder_thread = _run_coro_in_thread(
        lambda: _run_tool_call(tool_one, holder_entered, holder_release),
        errors=errors,
    )
    assert holder_entered.wait(1.0), f"holder did not start first tool call; errors={errors!r}"

    contender_errors: list[BaseException] = []
    contender_thread = _run_coro_in_thread(
        lambda: _run_tool_call(tool_two, contender_entered, None),
        errors=contender_errors,
    )

    contender_thread.join(0.5)
    assert not contender_thread.is_alive(), "second tool call should fail fast on queue timeout"
    assert contender_entered.is_set() is False, "timed out contender should never enter the blocking tool body"
    assert len(contender_errors) == 1
    assert type(contender_errors[0]).__name__ == "SandboxToolQueueTimeoutError"

    holder_release.set()
    _join_thread(holder_thread)
    assert errors == []

