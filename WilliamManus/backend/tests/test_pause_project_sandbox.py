"""Unit tests for run_agent_background._pause_project_sandbox."""

import asyncio
import json
import os
import sys
import types
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio

# ---------------------------------------------------------------------------
# Stubs – avoid importing the full dramatiq chain
# ---------------------------------------------------------------------------

if "structlog" not in sys.modules:
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

    class _DummyBoundLogger:
        def info(self, *a, **kw): return None
        def warning(self, *a, **kw): return None
        def error(self, *a, **kw): return None
        def debug(self, *a, **kw): return None

    class _DummyProcessorFormatter:
        @staticmethod
        def wrap_for_formatter(*a, **kw): return None

    sys.modules["structlog"] = types.SimpleNamespace(
        configure=lambda **kw: None,
        get_logger=lambda *a, **kw: _DummyBoundLogger(),
        stdlib=types.SimpleNamespace(
            add_log_level=lambda *a, **kw: None,
            PositionalArgumentsFormatter=lambda *a, **kw: None,
            ProcessorFormatter=_DummyProcessorFormatter,
            LoggerFactory=lambda *a, **kw: None,
            BoundLogger=_DummyBoundLogger,
        ),
        processors=types.SimpleNamespace(TimeStamper=lambda *a, **kw: None),
        contextvars=types.SimpleNamespace(
            clear_contextvars=lambda: None,
            bind_contextvars=lambda **kw: None,
            get_contextvars=lambda: {},
        ),
    )

# Fake ppio_sandbox.core
_fake_ppio = types.ModuleType("ppio_sandbox")
_fake_ppio_core = types.ModuleType("ppio_sandbox.core")

class _StubPPIOSandbox:
    @classmethod
    def _cls_pause(cls, sid): pass
    @classmethod
    def clone(cls, sid, count, *, timeout=3600): pass

_fake_ppio_core.Sandbox = _StubPPIOSandbox
_fake_ppio.core = _fake_ppio_core
sys.modules.setdefault("ppio_sandbox", _fake_ppio)
sys.modules.setdefault("ppio_sandbox.core", _fake_ppio_core)


# ---------------------------------------------------------------------------
# Import _pause_project_sandbox via importlib to avoid dramatiq
# ---------------------------------------------------------------------------

import importlib.util
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]

# We need to stub heavy dependencies that run_agent_background imports at top level.
# Create minimal stubs for modules that aren't available in test env.

# dramatiq needs middleware.AsyncIO() and set_broker, actor decorator
_dramatiq_middleware = types.SimpleNamespace(
    AsyncIO=lambda *a, **kw: None,
    Retries=lambda *a, **kw: None,
    TimeLimit=lambda *a, **kw: None,
)
_dramatiq_stub = types.SimpleNamespace(
    actor=lambda *a, **kw: (lambda f: f),
    set_broker=lambda *a, **kw: None,
    middleware=_dramatiq_middleware,
)
_dramatiq_brokers_redis = types.SimpleNamespace(
    RedisBroker=lambda *a, **kw: types.SimpleNamespace(),
)

for mod_name, stub in [
    ("dramatiq", _dramatiq_stub),
    ("dramatiq.brokers", types.SimpleNamespace()),
    ("dramatiq.brokers.redis", _dramatiq_brokers_redis),
    ("sentry", types.SimpleNamespace(init=lambda *a, **kw: None)),
    ("sentry_sdk", types.SimpleNamespace(init=lambda *a, **kw: None)),
    ("langfuse", types.SimpleNamespace()),
]:
    sys.modules[mod_name] = stub

# Stub redis.asyncio dependency used by services.redis
if "redis.asyncio" not in sys.modules:
    _fake_redis_asyncio = types.SimpleNamespace(
        Redis=type("Redis", (), {}),
        ConnectionPool=type("ConnectionPool", (), {}),
    )
    _fake_redis_pkg = types.ModuleType("redis")
    _fake_redis_pkg.asyncio = _fake_redis_asyncio
    sys.modules.setdefault("redis", _fake_redis_pkg)
    sys.modules["redis.asyncio"] = _fake_redis_asyncio

# Stub asyncpg dependency used by services.postgresql
if "asyncpg" not in sys.modules:
    _fake_asyncpg = types.SimpleNamespace(
        Pool=type("Pool", (), {}),
        create_pool=lambda *a, **kw: None,
        connect=lambda *a, **kw: None,
    )
    sys.modules["asyncpg"] = _fake_asyncpg

# Stub services.langfuse
if "services.langfuse" not in sys.modules:
    sys.modules["services.langfuse"] = types.SimpleNamespace(langfuse=None)

# Stub agentpress modules
for mod_name in [
    "agentpress", "agentpress.thread_manager", "agentpress.adk_thread_manager",
    "agentpress.tool",
]:
    if mod_name not in sys.modules:
        _m = types.ModuleType(mod_name)
        _m.ThreadManager = type("ThreadManager", (), {})
        _m.ADKThreadManager = type("ADKThreadManager", (), {})
        _m.Tool = type("Tool", (), {"__init__": lambda self: None})
        _m.ToolResult = type("ToolResult", (), {})
        sys.modules[mod_name] = _m

# Stub agent.run
if "agent" not in sys.modules:
    sys.modules["agent"] = types.ModuleType("agent")
if "agent.run" not in sys.modules:
    sys.modules["agent.run"] = types.SimpleNamespace(run_agent=lambda *a, **kw: None)

# Stub utils.retry
if "utils.retry" not in sys.modules:
    sys.modules["utils.retry"] = types.SimpleNamespace(retry=lambda *a, **kw: (lambda f: f))

# Stub utils.agent_run_context
if "utils.agent_run_context" not in sys.modules:
    sys.modules["utils.agent_run_context"] = types.SimpleNamespace(
        clear_agent_run_context=lambda: None,
        get_agent_run_context=lambda: (None, None),
        set_agent_run_context=lambda *a, **kw: None,
        get_agent_model_context=lambda: None,
    )


# ---------------------------------------------------------------------------
# Fake DB
# ---------------------------------------------------------------------------

class _FakeTableQuery:
    def __init__(self, client):
        self._client = client
        self._project_id = None

    def select(self, *a, **kw):
        return self

    def eq(self, field, value):
        if field == "project_id":
            self._project_id = value
        return self

    def update(self, payload):
        self._client._updates.append((self._project_id, payload))
        return self

    async def execute(self):
        if self._project_id and self._project_id in self._client._data:
            return SimpleNamespace(data=[self._client._data[self._project_id]])
        return SimpleNamespace(data=[])


class _FakeDBClient:
    def __init__(self, data=None):
        self._data = data or {}
        self._updates = []

    def table(self, name):
        return _FakeTableQuery(self)


class _FakeDBConnection:
    def __init__(self, client):
        self._client = client

    @property
    async def client(self):
        return self._client


# ---------------------------------------------------------------------------
# Import the function under test
# ---------------------------------------------------------------------------

# We load run_agent_background and extract _pause_project_sandbox
import run_agent_background as rab_module


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture(autouse=True)
async def _reset_auto_pause_monitor_state(monkeypatch):
    async def _run_inline(func, /, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr("run_agent_background.asyncio.to_thread", _run_inline)

    async def _cancel_monitor_tasks() -> None:
        tasks = [
            task
            for task in list(rab_module._sandbox_pause_monitor_tasks.values())
            if task and not task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        rab_module._sandbox_pause_monitor_tasks.clear()
        rab_module._sandbox_pause_monitor_lock = None

    await _cancel_monitor_tasks()

    async def _no_active_run(_project_id: str):
        return False

    monkeypatch.setattr(rab_module, "_project_has_active_run", _no_active_run)
    yield
    await _cancel_monitor_tasks()

@pytest.mark.asyncio
async def test_pause_project_sandbox_success(monkeypatch):
    sandbox_info = {"id": "sb-1", "state": "running", "type": "desktop"}
    db_client = _FakeDBClient({"proj-1": {"sandbox": json.dumps(sandbox_info)}})
    fake_db = _FakeDBConnection(db_client)

    monkeypatch.setattr(rab_module, "db", fake_db)
    monkeypatch.setenv("SANDBOX_AUTO_PAUSE_ON_COMPLETE", "true")

    pause_calls = []

    async def _pause(sid):
        pause_calls.append(sid)
        return True

    monkeypatch.setattr("sandbox.sandbox.pause_sandbox", _pause)

    await rab_module._pause_project_sandbox("proj-1")

    assert pause_calls == ["sb-1"]
    assert len(db_client._updates) == 1
    _, payload = db_client._updates[0]
    updated = json.loads(payload["sandbox"])
    assert updated["state"] == "paused"
    assert "paused_at" in updated


@pytest.mark.asyncio
async def test_pause_skipped_when_env_disabled(monkeypatch):
    monkeypatch.setenv("SANDBOX_AUTO_PAUSE_ON_COMPLETE", "false")
    db_client = _FakeDBClient()
    fake_db = _FakeDBConnection(db_client)
    monkeypatch.setattr(rab_module, "db", fake_db)

    await rab_module._pause_project_sandbox("proj-1")
    assert db_client._updates == []


@pytest.mark.asyncio
async def test_pause_skipped_when_no_project(monkeypatch):
    monkeypatch.setenv("SANDBOX_AUTO_PAUSE_ON_COMPLETE", "true")
    db_client = _FakeDBClient({})
    fake_db = _FakeDBConnection(db_client)
    monkeypatch.setattr(rab_module, "db", fake_db)

    await rab_module._pause_project_sandbox("nonexistent")
    assert db_client._updates == []


@pytest.mark.asyncio
async def test_pause_skipped_when_no_sandbox_id(monkeypatch):
    monkeypatch.setenv("SANDBOX_AUTO_PAUSE_ON_COMPLETE", "true")
    db_client = _FakeDBClient({"proj-1": {"sandbox": "{}"}})
    fake_db = _FakeDBConnection(db_client)
    monkeypatch.setattr(rab_module, "db", fake_db)

    await rab_module._pause_project_sandbox("proj-1")
    assert db_client._updates == []


@pytest.mark.asyncio
async def test_pause_skipped_when_already_paused(monkeypatch):
    sandbox_info = {"id": "sb-1", "state": "paused"}
    db_client = _FakeDBClient({"proj-1": {"sandbox": json.dumps(sandbox_info)}})
    fake_db = _FakeDBConnection(db_client)
    monkeypatch.setattr(rab_module, "db", fake_db)
    monkeypatch.setenv("SANDBOX_AUTO_PAUSE_ON_COMPLETE", "true")

    pause_calls = []

    async def _pause(sid):
        pause_calls.append(sid)
        return True

    monkeypatch.setattr("sandbox.sandbox.pause_sandbox", _pause)

    await rab_module._pause_project_sandbox("proj-1")
    assert pause_calls == []
    assert db_client._updates == []


@pytest.mark.asyncio
async def test_pause_failure_non_fatal(monkeypatch):
    sandbox_info = {"id": "sb-1", "state": "running"}
    db_client = _FakeDBClient({"proj-1": {"sandbox": json.dumps(sandbox_info)}})
    fake_db = _FakeDBConnection(db_client)
    monkeypatch.setattr(rab_module, "db", fake_db)
    monkeypatch.setenv("SANDBOX_AUTO_PAUSE_ON_COMPLETE", "true")

    async def _pause(sid):
        return False

    monkeypatch.setattr("sandbox.sandbox.pause_sandbox", _pause)

    # Should not raise
    await rab_module._pause_project_sandbox("proj-1")
    # No DB update since pause returned False
    assert db_client._updates == []


@pytest.mark.asyncio
async def test_pause_exception_non_fatal(monkeypatch):
    monkeypatch.setenv("SANDBOX_AUTO_PAUSE_ON_COMPLETE", "true")

    class _BrokenDB:
        @property
        async def client(self):
            raise RuntimeError("db down")

    monkeypatch.setattr(rab_module, "db", _BrokenDB())

    # Should not raise
    await rab_module._pause_project_sandbox("proj-1")


@pytest.mark.asyncio
async def test_pause_updates_db_with_utc_timestamp(monkeypatch):
    sandbox_info = {"id": "sb-1", "state": "running"}
    db_client = _FakeDBClient({"proj-1": {"sandbox": json.dumps(sandbox_info)}})
    fake_db = _FakeDBConnection(db_client)
    monkeypatch.setattr(rab_module, "db", fake_db)
    monkeypatch.setenv("SANDBOX_AUTO_PAUSE_ON_COMPLETE", "true")

    async def _pause(sid):
        return True

    monkeypatch.setattr("sandbox.sandbox.pause_sandbox", _pause)

    await rab_module._pause_project_sandbox("proj-1")

    _, payload = db_client._updates[0]
    updated = json.loads(payload["sandbox"])
    paused_at = updated["paused_at"]
    # Should be valid ISO-8601 with UTC offset
    dt = datetime.fromisoformat(paused_at)
    assert dt.tzinfo is not None


@pytest.mark.asyncio
async def test_schedule_auto_pause_with_zero_grace_pauses_immediately(monkeypatch):
    sandbox_info = {"id": "sb-1", "state": "running"}
    db_client = _FakeDBClient({"proj-1": {"sandbox": json.dumps(sandbox_info)}})
    fake_db = _FakeDBConnection(db_client)
    monkeypatch.setattr(rab_module, "db", fake_db)
    monkeypatch.setenv("SANDBOX_AUTO_PAUSE_ON_COMPLETE", "true")
    monkeypatch.setenv("SANDBOX_AUTO_PAUSE_GRACE_SECONDS", "0")

    pause_calls = []

    async def _pause(sid):
        pause_calls.append(sid)
        return True

    monkeypatch.setattr("sandbox.sandbox.pause_sandbox", _pause)

    await rab_module._schedule_project_sandbox_auto_pause("proj-1")
    await asyncio.sleep(0.05)

    assert pause_calls == ["sb-1"]
    assert len(db_client._updates) >= 2  # touch last_accessed + paused update


@pytest.mark.asyncio
async def test_schedule_auto_pause_waits_while_project_has_active_run(monkeypatch):
    sandbox_info = {"id": "sb-1", "state": "running"}
    db_client = _FakeDBClient({"proj-1": {"sandbox": json.dumps(sandbox_info)}})
    fake_db = _FakeDBConnection(db_client)
    monkeypatch.setattr(rab_module, "db", fake_db)
    monkeypatch.setenv("SANDBOX_AUTO_PAUSE_ON_COMPLETE", "true")
    monkeypatch.setenv("SANDBOX_AUTO_PAUSE_GRACE_SECONDS", "0")
    monkeypatch.setattr(rab_module, "AUTO_PAUSE_ACTIVE_RUN_POLL_SECONDS", 0.01)

    active_states = iter([True, True, False, False])

    async def _active_run_probe(_project_id: str):
        return next(active_states, False)

    monkeypatch.setattr(rab_module, "_project_has_active_run", _active_run_probe)

    pause_calls = []

    async def _pause(sid):
        pause_calls.append(sid)
        return True

    monkeypatch.setattr("sandbox.sandbox.pause_sandbox", _pause)

    await rab_module._schedule_project_sandbox_auto_pause("proj-1")
    await asyncio.sleep(0.08)

    assert pause_calls == ["sb-1"]


@pytest.mark.asyncio
async def test_extend_project_sandbox_timeout_once_sets_timeout(monkeypatch):
    sandbox_info = {"id": "sb-1", "state": "running", "type": "desktop"}
    db_client = _FakeDBClient({"proj-1": {"sandbox": json.dumps(sandbox_info)}})
    fake_db = _FakeDBConnection(db_client)
    monkeypatch.setattr(rab_module, "db", fake_db)
    monkeypatch.setattr(rab_module, "SANDBOX_SET_TIMEOUT_WINDOW_SECONDS", 1234)
    monkeypatch.setattr(rab_module, "SANDBOX_SET_TIMEOUT_CONNECT_TIMEOUT_SECONDS", 1.0)

    timeout_calls = []

    class _LeaseSandbox:
        def set_timeout(self, seconds):
            timeout_calls.append(seconds)
            return True

    async def _get_or_start(_sandbox_id, _sandbox_type):
        return _LeaseSandbox()

    monkeypatch.setattr("sandbox.sandbox.get_or_start_sandbox", _get_or_start)

    result = await rab_module._extend_project_sandbox_timeout_once(
        project_id="proj-1",
        agent_run_id="run-lease-1",
    )

    assert result is True
    assert timeout_calls == [1234]


@pytest.mark.asyncio
async def test_extend_project_sandbox_timeout_once_skips_paused_sandbox(monkeypatch):
    sandbox_info = {"id": "sb-1", "state": "paused", "type": "desktop"}
    db_client = _FakeDBClient({"proj-1": {"sandbox": json.dumps(sandbox_info)}})
    fake_db = _FakeDBConnection(db_client)
    monkeypatch.setattr(rab_module, "db", fake_db)

    call_count = {"count": 0}

    async def _get_or_start(_sandbox_id, _sandbox_type):
        call_count["count"] += 1
        return object()

    monkeypatch.setattr("sandbox.sandbox.get_or_start_sandbox", _get_or_start)

    result = await rab_module._extend_project_sandbox_timeout_once(
        project_id="proj-1",
        agent_run_id="run-lease-2",
    )

    assert result is False
    assert call_count["count"] == 0


@pytest.mark.asyncio
async def test_extend_shadow_clone_sandbox_timeouts_once_extends_active_and_standby(monkeypatch):
    import agentscope_integration.shadow_clone.sandbox_lease as lease_module

    async def _get_lease(_run_id):
        return {
            "project_id": "proj-shadow",
            "sandbox_id": "sb-active",
            "sandbox_type": "desktop",
            "sandbox_info": {"state": "running"},
            "standby_sandbox_id": "sb-standby",
            "standby_sandbox_type": "desktop",
            "standby_sandbox_info": {"state": "running"},
        }

    extend_calls = []

    async def _extend(sandbox_id, sandbox_type, **kwargs):
        extend_calls.append((sandbox_id, sandbox_type, kwargs))
        return object()

    monkeypatch.setattr(lease_module, "get_run_sandbox_lease", _get_lease)
    monkeypatch.setattr("sandbox.sandbox.extend_sandbox_timeout", _extend)
    monkeypatch.setattr(rab_module, "SANDBOX_SET_TIMEOUT_WINDOW_SECONDS", 777)
    monkeypatch.setattr(rab_module, "SANDBOX_SET_TIMEOUT_CONNECT_TIMEOUT_SECONDS", 9.0)

    result = await rab_module._extend_shadow_clone_sandbox_timeouts_once(
        project_id="proj-shadow",
        agent_run_id="run-shadow",
    )

    assert result is True
    assert [call[0] for call in extend_calls] == ["sb-active", "sb-standby"]
    assert all(call[2]["timeout_window_seconds"] == 777 for call in extend_calls)
    assert all(call[2]["connect_timeout_seconds"] == 9.0 for call in extend_calls)
