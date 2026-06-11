import asyncio
import importlib
import threading

import pytest


def _reset_db_connection_singleton(postgresql_module) -> None:
    postgresql_module.DBConnection._instance = None


def _inline_event_wait(monkeypatch: pytest.MonkeyPatch, postgresql_module) -> None:
    async def _fake_to_thread(func, /, *args, **kwargs):
        bound_event = getattr(func, "__self__", None)
        if isinstance(bound_event, threading.Event) and getattr(func, "__name__", "") == "wait":
            timeout = args[0] if args else kwargs.get("timeout")
            loop = asyncio.get_running_loop()
            deadline = None if timeout is None else loop.time() + float(timeout)
            while not bound_event.is_set():
                if deadline is not None and loop.time() >= deadline:
                    return bound_event.is_set()
                await asyncio.sleep(0)
            return True
        return func(*args, **kwargs)

    monkeypatch.setattr(postgresql_module.asyncio, "to_thread", _fake_to_thread)


@pytest.mark.asyncio
async def test_db_initialize_singleflights_concurrent_calls(monkeypatch):
    postgresql_module = importlib.import_module("services.postgresql")
    _reset_db_connection_singleton(postgresql_module)
    _inline_event_wait(monkeypatch, postgresql_module)

    db = postgresql_module.DBConnection()
    monkeypatch.setenv("DATABASE_URL", "postgresql://test/singleflight")

    create_pool_calls = 0
    release_pool_creation = asyncio.Event()
    created_pool = object()

    async def _fake_create_pool(*_args, **_kwargs):
        nonlocal create_pool_calls
        create_pool_calls += 1
        if create_pool_calls > 1:
            raise AssertionError("create_pool should only run once per process initialization burst")
        await asyncio.sleep(0)
        await release_pool_creation.wait()
        return created_pool

    monkeypatch.setattr(postgresql_module.asyncpg, "create_pool", _fake_create_pool)

    first = asyncio.create_task(db.initialize())
    await asyncio.sleep(0)
    second = asyncio.create_task(db.initialize())
    await asyncio.sleep(0)
    release_pool_creation.set()

    await asyncio.wait_for(asyncio.gather(first, second), timeout=1.0)

    assert create_pool_calls == 1
    assert db._initialized is True
    assert db._pool is created_pool

    _reset_db_connection_singleton(postgresql_module)


@pytest.mark.asyncio
async def test_db_initialize_can_retry_after_failed_singleflight(monkeypatch):
    postgresql_module = importlib.import_module("services.postgresql")
    _reset_db_connection_singleton(postgresql_module)

    db = postgresql_module.DBConnection()
    monkeypatch.setenv("DATABASE_URL", "postgresql://test/retry")

    create_pool_calls = 0
    created_pool = object()

    async def _fake_create_pool(*_args, **_kwargs):
        nonlocal create_pool_calls
        create_pool_calls += 1
        await asyncio.sleep(0)
        if create_pool_calls == 1:
            raise RuntimeError("transient connect failure")
        return created_pool

    monkeypatch.setattr(postgresql_module.asyncpg, "create_pool", _fake_create_pool)

    with pytest.raises(RuntimeError, match="initialization failed"):
        await asyncio.wait_for(db.initialize(), timeout=1.0)

    assert db._initialized is False
    assert db._pool is None

    await asyncio.wait_for(db.initialize(), timeout=1.0)

    assert create_pool_calls == 2
    assert db._initialized is True
    assert db._pool is created_pool

    _reset_db_connection_singleton(postgresql_module)


@pytest.mark.asyncio
async def test_db_initialize_can_retry_after_cancelled_singleflight(monkeypatch):
    postgresql_module = importlib.import_module("services.postgresql")
    _reset_db_connection_singleton(postgresql_module)
    _inline_event_wait(monkeypatch, postgresql_module)

    db = postgresql_module.DBConnection()
    monkeypatch.setenv("DATABASE_URL", "postgresql://test/cancelled-retry")

    create_pool_calls = 0
    first_call_started = asyncio.Event()
    created_pool = object()

    async def _fake_create_pool(*_args, **_kwargs):
        nonlocal create_pool_calls
        create_pool_calls += 1
        first_call_started.set()
        await asyncio.sleep(3600)
        return created_pool

    monkeypatch.setattr(postgresql_module.asyncpg, "create_pool", _fake_create_pool)

    first = asyncio.create_task(db.initialize())
    await asyncio.wait_for(first_call_started.wait(), timeout=1.0)
    first.cancel()

    with pytest.raises(asyncio.CancelledError):
        await first

    async def _second_create_pool(*_args, **_kwargs):
        nonlocal create_pool_calls
        create_pool_calls += 1
        return created_pool

    monkeypatch.setattr(postgresql_module.asyncpg, "create_pool", _second_create_pool)

    await asyncio.wait_for(db.initialize(), timeout=1.0)

    assert create_pool_calls == 2
    assert db._initialized is True
    assert db._pool is created_pool

    _reset_db_connection_singleton(postgresql_module)
