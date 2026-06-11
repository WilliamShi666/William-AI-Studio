"""Unit tests for sandbox/tool_base.py – _update_sandbox_state and paused-state branch."""

import asyncio
from contextlib import asynccontextmanager
import json
import sys
import types
import uuid
from datetime import datetime, timezone
from importlib import import_module
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------

if "structlog" not in sys.modules:
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

# Minimal stubs to avoid heavy side effects during sandbox.tool_base import.
if "langfuse" not in sys.modules:
    sys.modules["langfuse"] = types.SimpleNamespace()
if "services.langfuse" not in sys.modules:
    sys.modules["services.langfuse"] = types.SimpleNamespace(langfuse=None)
if "services" not in sys.modules:
    services_module = types.ModuleType("services")
    services_module.__path__ = [
        str(Path(__file__).resolve().parents[1] / "services")
    ]
    sys.modules["services"] = services_module
else:
    services_module = sys.modules["services"]
    if not hasattr(services_module, "__path__"):
        services_module.__path__ = [
            str(Path(__file__).resolve().parents[1] / "services")
        ]
if "services.redis" not in sys.modules:
    services_redis_stub = types.SimpleNamespace(
        REDIS_KEY_TTL=3600,
        get_client=lambda: None,
        set=lambda *args, **kwargs: None,
    )
    sys.modules["services.redis"] = services_redis_stub
    setattr(services_module, "redis", services_redis_stub)
if "services.postgresql" not in sys.modules:
    services_postgresql_stub = types.ModuleType("services.postgresql")
    services_postgresql_stub.DBConnection = type("DBConnection", (), {})
    sys.modules["services.postgresql"] = services_postgresql_stub
    setattr(services_module, "postgresql", services_postgresql_stub)
if "services.workspace_artifacts" not in sys.modules:
    workspace_artifacts_mod = types.ModuleType("services.workspace_artifacts")

    async def _noop_persist_artifact(**_kwargs):
        return None

    async def _noop_read_artifact_bytes(**_kwargs):
        return None

    async def _noop_list_entries(**_kwargs):
        return []

    async def _noop_rehydrate_project(**_kwargs):
        return {"total": 0, "rehydrated": 0}

    async def _noop_get_artifact_record(**_kwargs):
        return None

    async def _noop_collect_file_paths(**_kwargs):
        return []

    async def _noop_delete_artifact(**_kwargs):
        return None

    workspace_artifacts_mod.is_user_visible_workspace_artifact_path = (
        lambda path: str(path or "").startswith("/workspace")
    )
    workspace_artifacts_mod.guess_workspace_artifact_content_type = (
        lambda _path: "application/octet-stream"
    )
    workspace_artifacts_mod.workspace_artifacts = types.SimpleNamespace(
        persist_artifact=_noop_persist_artifact,
        read_artifact_bytes=_noop_read_artifact_bytes,
        list_entries=_noop_list_entries,
        rehydrate_project=_noop_rehydrate_project,
        get_artifact_record=_noop_get_artifact_record,
        collect_file_paths=_noop_collect_file_paths,
        delete_artifact=_noop_delete_artifact,
    )
    sys.modules["services.workspace_artifacts"] = workspace_artifacts_mod
    setattr(services_module, "workspace_artifacts", workspace_artifacts_mod)
if "services.sandbox_create_capacity" not in sys.modules:
    sandbox_create_capacity_mod = import_module("services.sandbox_create_capacity")
    sys.modules["services.sandbox_create_capacity"] = sandbox_create_capacity_mod
    setattr(services_module, "sandbox_create_capacity", sandbox_create_capacity_mod)
if "agentpress" not in sys.modules:
    sys.modules["agentpress"] = types.ModuleType("agentpress")
if "agentpress.adk_thread_manager" not in sys.modules:
    _adk_module = types.ModuleType("agentpress.adk_thread_manager")
    _adk_module.ADKThreadManager = type("ADKThreadManager", (), {})
    sys.modules["agentpress.adk_thread_manager"] = _adk_module
if "agentpress.tool" not in sys.modules:
    _tool_module = types.ModuleType("agentpress.tool")
    _tool_module.Tool = type("Tool", (), {"__init__": lambda self: None})
    sys.modules["agentpress.tool"] = _tool_module

from sandbox.session_control import (
    clear_shadow_clone_session_state,
    get_or_create_shadow_clone_session_state,
)
from sandbox.tool_base import SandboxToolsBase


# ---------------------------------------------------------------------------
# Fake DB chain
# ---------------------------------------------------------------------------

class _FakeTableQuery:
    def __init__(self, client):
        self._client = client
        self._project_id = None
        self._pending_update = None

    def eq(self, field, value):
        if field == "project_id":
            self._project_id = value
        return self

    def select(self, *a, **kw):
        return self

    def update(self, payload):
        # Two call patterns:
        # 1) table().update({}).eq().execute()  (_update_sandbox_state)
        # 2) table().eq().update({})  (new sandbox creation, awaited directly)
        if self._project_id is not None:
            # Pattern 2: eq() already called, update is the terminal call
            self._client._record_update(self._project_id, payload)
            # Return an awaitable-like object
            return _FakeUpdateResult(self._client)
        # Pattern 1: update before eq, store for later
        self._pending_update = payload
        return self

    async def execute(self):
        if self._pending_update is not None:
            self._client._record_update(self._project_id, self._pending_update)
            self._pending_update = None
            return SimpleNamespace(data=[{}])
        if self._client._select_data is not None:
            return SimpleNamespace(data=self._client._select_data)
        return SimpleNamespace(data=[])


class _FakeUpdateResult:
    """Awaitable result for the table().eq().update() pattern."""
    def __init__(self, client):
        self._client = client

    def __await__(self):
        return self._resolve().__await__()

    async def _resolve(self):
        return SimpleNamespace(data=[{}])


class _FakeDBClient:
    def __init__(self, select_data=None):
        self._updates = []
        self._select_data = [dict(row) for row in select_data] if select_data is not None else None

    def table(self, name):
        return _FakeTableQuery(self)

    def _record_update(self, project_id, payload):
        self._updates.append((project_id, payload))
        if self._select_data:
            self._select_data[0].update(payload)


class _FakeDB:
    def __init__(self, client):
        self._client = client

    @property
    async def client(self):
        return self._client


class _FakeThreadManager:
    def __init__(self, db_client):
        self.db = _FakeDB(db_client)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class _FakeStream:
    def start(self): return None
    def get_url(self): return "https://stream.example"


class _FakeSandbox:
    def __init__(self, sandbox_id, *, template_id=None):
        self.sandbox_id = sandbox_id
        self.stream = _FakeStream()
        self.template_id = template_id
        self.metadata = (
            {"template_id": template_id, "template_source": "test"}
            if template_id
            else {}
        )


class _FakeRedisClient:
    def __init__(self, *, set_results=None):
        self._data = {}
        self.set_results = list(set_results or [])
        self.set_calls = []
        self.eval_calls = []

    async def set(self, key, value, *, ex=None, nx=False):
        self.set_calls.append({"key": key, "value": value, "ex": ex, "nx": nx})
        if self.set_results:
            result = self.set_results.pop(0)
            if result:
                self._data[key] = value
            return result
        if nx and key in self._data:
            return False
        self._data[key] = value
        return True

    async def get(self, key):
        return self._data.get(key)

    async def delete(self, key):
        existed = key in self._data
        self._data.pop(key, None)
        return 1 if existed else 0

    async def eval(self, _script, _numkeys, key, token):
        self.eval_calls.append({"key": key, "token": token})
        if self._data.get(key) == token:
            self._data.pop(key, None)
            return 1
        return 0


def _make_tool(project_id, db_client, sandbox_type="desktop"):
    """Create a SandboxToolsBase with faked internals."""
    tool = SandboxToolsBase.__new__(SandboxToolsBase)
    tool.project_id = project_id
    tool.sandbox_type = sandbox_type
    tool.workspace_path = "/workspace"
    tool._sandbox = None
    tool._sandbox_id = None
    tool._sandbox_pass = None
    tool._shadow_clone_run_id = None
    tool._strict_sandbox_mode = False
    tool._shared_shadow_clone_session = None
    tool._ensure_lock = asyncio.Lock()
    tool._workspace_sync_snapshot = {}
    tool.thread_manager = _FakeThreadManager(db_client)
    return tool


@pytest.fixture(autouse=True)
def _inline_to_thread(monkeypatch):
    """Keep tests single-threaded; avoid executor shutdown hangs in pytest teardown."""

    async def _run_inline(func, /, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr("sandbox.tool_base.asyncio.to_thread", _run_inline)


@pytest.fixture(autouse=True)
def fake_redis_client(monkeypatch):
    client = _FakeRedisClient()

    async def _get_client():
        return client

    monkeypatch.setattr("sandbox.tool_base.redis.get_client", _get_client, raising=False)
    return client


@pytest.fixture(autouse=True)
def _clear_shadow_clone_session_registry():
    clear_shadow_clone_session_state("run-strict")
    clear_shadow_clone_session_state("run-shared")
    yield
    clear_shadow_clone_session_state("run-strict")
    clear_shadow_clone_session_state("run-shared")


# ---------------------------------------------------------------------------
# _update_sandbox_state
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_update_sandbox_state_merges_and_persists():
    db_client = _FakeDBClient()
    tool = _make_tool("proj-1", db_client)

    existing = {"id": "sb-1", "state": "running", "type": "desktop"}
    await tool._update_sandbox_state(existing, state="paused", paused_at="2026-01-01T00:00:00Z")

    assert len(db_client._updates) == 1
    pid, payload = db_client._updates[0]
    assert pid == "proj-1"
    merged = json.loads(payload["sandbox"])
    assert merged["state"] == "paused"
    assert merged["paused_at"] == "2026-01-01T00:00:00Z"
    assert merged["id"] == "sb-1"


# ---------------------------------------------------------------------------
# _ensure_sandbox – paused state branch
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_ensure_sandbox_resumes_paused(monkeypatch):
    fake_sb = _FakeSandbox("sb-resumed")

    async def _resume(password, project_id, sandbox_type, sandbox_info):
        return (fake_sb, "resumed")

    monkeypatch.setattr("sandbox.tool_base.resume_or_create_sandbox", _resume)

    sandbox_info = {"id": "sb-old", "state": "paused", "type": "desktop", "pass": "pw"}
    db_client = _FakeDBClient(select_data=[{"sandbox": json.dumps(sandbox_info)}])
    tool = _make_tool("proj-1", db_client, sandbox_type="desktop")

    # Patch _run_blocking_sandbox_call to just call the callable
    async def _run_blocking(op, call, **kw):
        return call()
    tool._run_blocking_sandbox_call = _run_blocking

    result = await tool._ensure_sandbox()
    assert result.sandbox_id == "sb-resumed"
    # DB should have been updated with state=running
    assert len(db_client._updates) >= 1
    _, payload = db_client._updates[-1]
    merged = json.loads(payload["sandbox"])
    assert merged["state"] == "running"


@pytest.mark.asyncio
async def test_ensure_sandbox_resumed_desktop_restarts_stream(monkeypatch):
    stream_started = []
    stream_url_calls = []

    class _TrackedStream:
        def start(self):
            stream_started.append(True)
            return None
        def get_url(self):
            stream_url_calls.append(True)
            return "https://vnc.example"

    fake_sb = _FakeSandbox("sb-resumed")
    fake_sb.stream = _TrackedStream()

    async def _resume(password, project_id, sandbox_type, sandbox_info):
        return (fake_sb, "resumed")

    monkeypatch.setattr("sandbox.tool_base.resume_or_create_sandbox", _resume)

    sandbox_info = {"id": "sb-old", "state": "paused", "type": "desktop", "pass": "pw"}
    db_client = _FakeDBClient(select_data=[{"sandbox": json.dumps(sandbox_info)}])
    tool = _make_tool("proj-1", db_client, sandbox_type="desktop")

    async def _run_blocking(op, call, **kw):
        return call()
    tool._run_blocking_sandbox_call = _run_blocking

    await tool._ensure_sandbox()
    assert len(stream_started) >= 1
    assert len(stream_url_calls) >= 1


@pytest.mark.asyncio
async def test_ensure_sandbox_resume_failure_creates_new(monkeypatch):
    fake_new = _FakeSandbox("sb-created")

    async def _resume(password, project_id, sandbox_type, sandbox_info):
        return (fake_new, "created")

    monkeypatch.setattr("sandbox.tool_base.resume_or_create_sandbox", _resume)

    sandbox_info = {"id": "sb-old", "state": "paused", "type": "desktop", "pass": "pw"}
    db_client = _FakeDBClient(select_data=[{"sandbox": json.dumps(sandbox_info)}])
    tool = _make_tool("proj-1", db_client, sandbox_type="desktop")

    async def _run_blocking(op, call, **kw):
        return call()
    tool._run_blocking_sandbox_call = _run_blocking

    result = await tool._ensure_sandbox()
    assert result.sandbox_id == "sb-created"
    # DB should have created_at since action='created'
    _, payload = db_client._updates[-1]
    merged = json.loads(payload["sandbox"])
    assert "created_at" in merged


@pytest.mark.asyncio
async def test_ensure_sandbox_running_uses_normal_path(monkeypatch):
    fake_sb = _FakeSandbox("sb-running")

    async def _get_or_start(sid, stype):
        return fake_sb

    monkeypatch.setattr("sandbox.tool_base.get_or_start_sandbox", _get_or_start)

    sandbox_info = {"id": "sb-running", "state": "running", "type": "desktop", "pass": "pw"}
    db_client = _FakeDBClient(select_data=[{"sandbox": json.dumps(sandbox_info)}])
    tool = _make_tool("proj-1", db_client, sandbox_type="desktop")

    result = await tool._ensure_sandbox()
    assert result.sandbox_id == "sb-running"


@pytest.mark.asyncio
async def test_ensure_sandbox_new_sandbox_has_state_and_created_at(monkeypatch):
    fake_sb = _FakeSandbox("sb-brand-new", template_id="tmpl-code-current")

    async def _create(pw, pid, stype):
        return fake_sb

    monkeypatch.setattr("sandbox.tool_base.create_sandbox", _create)
    monkeypatch.setattr(
        "sandbox.tool_base.resolve_sandbox_template_lineage",
        lambda sandbox_type: {
            "template_id": "tmpl-code-current",
            "template_type": sandbox_type,
            "template_source": "config_env",
        },
    )

    # No existing sandbox data
    db_client = _FakeDBClient(select_data=[{"sandbox": "{}"}])
    tool = _make_tool("proj-1", db_client, sandbox_type="code")

    async def _run_blocking(op, call, **kw):
        return call()
    tool._run_blocking_sandbox_call = _run_blocking

    result = await tool._ensure_sandbox()
    assert result.sandbox_id == "sb-brand-new"
    # Check DB was updated with state and created_at
    assert len(db_client._updates) >= 1
    _, payload = db_client._updates[-1]
    merged = json.loads(payload["sandbox"])
    assert merged["template_id"] == "tmpl-code-current"
    assert merged["template_type"] == "code"
    assert merged["template_source"] == "config_env"


@pytest.mark.asyncio
async def test_ensure_sandbox_new_sandbox_rehydrates_workspace_artifacts(monkeypatch):
    fake_sb = _FakeSandbox("sb-brand-new", template_id="tmpl-code-current")

    async def _create(_pw, _pid, _stype):
        return fake_sb

    monkeypatch.setattr("sandbox.tool_base.create_sandbox", _create)
    monkeypatch.setattr(
        "sandbox.tool_base.resolve_sandbox_template_lineage",
        lambda sandbox_type: {
            "template_id": "tmpl-code-current",
            "template_type": sandbox_type,
            "template_source": "config_env",
        },
    )

    db_client = _FakeDBClient(select_data=[{"sandbox": "{}"}])
    tool = _make_tool("proj-1", db_client, sandbox_type="code")

    async def _run_blocking(op, call, **kw):
        return call()

    rehydrate_mock = AsyncMock()
    tool._run_blocking_sandbox_call = _run_blocking
    tool._rehydrate_workspace_artifacts = rehydrate_mock

    await tool._ensure_sandbox()

    rehydrate_mock.assert_awaited_once()
    _, kwargs = rehydrate_mock.await_args
    assert kwargs["sandbox_id"] == "sb-brand-new"


@pytest.mark.asyncio
async def test_ensure_sandbox_running_runtime_template_drift_recreates_when_metadata_missing(monkeypatch):
    old_sb = _FakeSandbox("sb-old", template_id="tmpl-desktop-old")
    new_sb = _FakeSandbox("sb-new", template_id="tmpl-desktop-current")
    connect_calls = []
    create_calls = []

    async def _get_or_start(sid, stype):
        connect_calls.append((sid, stype))
        return old_sb

    async def _create(_pw, _pid, _stype):
        create_calls.append((_pid, _stype))
        return new_sb

    monkeypatch.setattr("sandbox.tool_base.get_or_start_sandbox", _get_or_start)
    monkeypatch.setattr("sandbox.tool_base.create_sandbox", _create)
    monkeypatch.setattr(
        "sandbox.tool_base.resolve_sandbox_template_lineage",
        lambda sandbox_type: {
            "template_id": "tmpl-desktop-current",
            "template_type": sandbox_type,
            "template_source": "config_env",
        },
    )

    sandbox_info = {"id": "sb-old", "state": "running", "type": "desktop", "pass": "pw"}
    db_client = _FakeDBClient(select_data=[{"sandbox": json.dumps(sandbox_info)}])
    tool = _make_tool("proj-1", db_client, sandbox_type="desktop")

    async def _run_blocking(op, call, **kw):
        return call()

    tool._run_blocking_sandbox_call = _run_blocking

    result = await tool._ensure_sandbox()

    assert connect_calls == [("sb-old", "desktop")]
    assert create_calls == [("proj-1", "desktop")]
    assert result.sandbox_id == "sb-new"
    _, payload = db_client._updates[-1]
    merged = json.loads(payload["sandbox"])
    assert merged["id"] == "sb-new"
    assert merged["template_id"] == "tmpl-desktop-current"
    assert merged["template_type"] == "desktop"


@pytest.mark.asyncio
async def test_ensure_sandbox_running_prechecked_template_drift_recreates_without_attach(monkeypatch):
    new_sb = _FakeSandbox("sb-new", template_id="tmpl-desktop-current")
    create_calls = []
    connect_mock = AsyncMock()

    async def _create(_pw, _pid, _stype):
        create_calls.append((_pid, _stype))
        return new_sb

    monkeypatch.setattr("sandbox.tool_base.get_or_start_sandbox", connect_mock)
    monkeypatch.setattr("sandbox.tool_base.create_sandbox", _create)
    monkeypatch.setattr(
        "sandbox.tool_base.resolve_sandbox_template_lineage",
        lambda sandbox_type: {
            "template_id": "tmpl-desktop-current",
            "template_type": sandbox_type,
            "template_source": "config_env",
        },
    )

    sandbox_info = {
        "id": "sb-old",
        "state": "running",
        "type": "desktop",
        "pass": "pw",
        "template_id": "tmpl-desktop-old",
        "template_type": "desktop",
    }
    db_client = _FakeDBClient(select_data=[{"sandbox": json.dumps(sandbox_info)}])
    tool = _make_tool("proj-1", db_client, sandbox_type="desktop")

    async def _run_blocking(op, call, **kw):
        return call()

    tool._run_blocking_sandbox_call = _run_blocking

    result = await tool._ensure_sandbox()

    connect_mock.assert_not_awaited()
    assert create_calls == [("proj-1", "desktop")]
    assert result.sandbox_id == "sb-new"


@pytest.mark.asyncio
async def test_ensure_sandbox_new_sandbox_uses_owner_token_lock(monkeypatch, fake_redis_client):
    fake_sb = _FakeSandbox("sb-locked")

    async def _create(pw, pid, stype):
        return fake_sb

    monkeypatch.setattr("sandbox.tool_base.create_sandbox", _create)

    db_client = _FakeDBClient(select_data=[{"sandbox": "{}"}])
    tool = _make_tool("proj-1", db_client, sandbox_type="code")

    async def _run_blocking(op, call, **kw):
        return call()
    tool._run_blocking_sandbox_call = _run_blocking

    result = await tool._ensure_sandbox()

    assert result.sandbox_id == "sb-locked"
    assert len(fake_redis_client.set_calls) == 1
    assert fake_redis_client.set_calls[0]["nx"] is True
    assert fake_redis_client.eval_calls[0]["token"] == fake_redis_client.set_calls[0]["value"]
    assert fake_redis_client._data == {}


@pytest.mark.asyncio
async def test_ensure_sandbox_new_sandbox_acquires_create_capacity(monkeypatch):
    fake_sb = _FakeSandbox("sb-throttled")
    capacity_calls: list[dict[str, object]] = []

    @asynccontextmanager
    async def _fake_acquire_create_capacity(
        *,
        holder_kind: str,
        project_id: str,
        sandbox_type: str,
        queue_timeout_seconds=None,
        lease_ttl_seconds=None,
    ):
        capacity_calls.append(
            {
                "holder_kind": holder_kind,
                "project_id": project_id,
                "sandbox_type": sandbox_type,
                "queue_timeout_seconds": queue_timeout_seconds,
                "lease_ttl_seconds": lease_ttl_seconds,
            }
        )
        yield {"acquired": True, "lease_id": "lease-1"}

    async def _create(pw, pid, stype):
        return fake_sb

    monkeypatch.setattr("sandbox.tool_base.create_sandbox", _create)
    monkeypatch.setattr(
        "sandbox.tool_base.sandbox_create_capacity.acquire_sandbox_create_capacity",
        _fake_acquire_create_capacity,
    )

    db_client = _FakeDBClient(select_data=[{"sandbox": "{}"}])
    tool = _make_tool("proj-1", db_client, sandbox_type="code")

    async def _run_blocking(op, call, **kw):
        return call()

    tool._run_blocking_sandbox_call = _run_blocking

    result = await tool._ensure_sandbox()

    assert result.sandbox_id == "sb-throttled"
    assert len(capacity_calls) == 1
    assert capacity_calls[0]["holder_kind"] == "tool_base_create"
    assert capacity_calls[0]["project_id"] == "proj-1"
    assert capacity_calls[0]["sandbox_type"] == "code"


@pytest.mark.asyncio
async def test_ensure_sandbox_running_reuse_does_not_acquire_create_capacity(monkeypatch):
    fake_sb = _FakeSandbox("sb-running")

    async def _get_or_start(sid, stype):
        return fake_sb

    @asynccontextmanager
    async def _unexpected_acquire_create_capacity(**_kwargs):
        raise AssertionError("create capacity should not be acquired for sandbox reuse")
        yield

    monkeypatch.setattr("sandbox.tool_base.get_or_start_sandbox", _get_or_start)
    monkeypatch.setattr(
        "sandbox.tool_base.sandbox_create_capacity.acquire_sandbox_create_capacity",
        _unexpected_acquire_create_capacity,
    )

    sandbox_info = {"id": "sb-running", "state": "running", "type": "desktop", "pass": "pw"}
    db_client = _FakeDBClient(select_data=[{"sandbox": json.dumps(sandbox_info)}])
    tool = _make_tool("proj-1", db_client, sandbox_type="desktop")

    result = await tool._ensure_sandbox()

    assert result.sandbox_id == "sb-running"


@pytest.mark.asyncio
async def test_ensure_sandbox_waits_for_other_creator_then_reuses_sandbox(monkeypatch, fake_redis_client):
    fake_redis_client.set_results = [False]
    fake_sb = _FakeSandbox("sb-peer")
    connect_calls = []

    async def _get_or_start(sid, stype):
        connect_calls.append((sid, stype))
        return fake_sb

    create_mock = AsyncMock()
    monkeypatch.setattr("sandbox.tool_base.get_or_start_sandbox", _get_or_start)
    monkeypatch.setattr("sandbox.tool_base.create_sandbox", create_mock)

    db_client = _FakeDBClient(select_data=[{"sandbox": "{}"}])
    tool = _make_tool("proj-1", db_client, sandbox_type="desktop")

    async def _sleep(_seconds):
        db_client._select_data[0]["sandbox"] = json.dumps(
            {"id": "sb-peer", "state": "running", "type": "desktop", "pass": "pw"},
        )

    monkeypatch.setattr("sandbox.tool_base.asyncio.sleep", _sleep)

    result = await tool._ensure_sandbox()

    assert result.sandbox_id == "sb-peer"
    assert connect_calls == [("sb-peer", "desktop")]
    create_mock.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_sandbox_code_tool_reuses_existing_desktop_sandbox(monkeypatch):
    fake_sb = _FakeSandbox("sb-desktop")
    connect_calls = []
    create_mock = AsyncMock()

    async def _get_or_start(sid, stype):
        connect_calls.append((sid, stype))
        return fake_sb

    monkeypatch.setattr("sandbox.tool_base.get_or_start_sandbox", _get_or_start)
    monkeypatch.setattr("sandbox.tool_base.create_sandbox", create_mock)

    sandbox_info = {"id": "sb-desktop", "state": "running", "type": "desktop", "pass": "pw"}
    db_client = _FakeDBClient(select_data=[{"sandbox": json.dumps(sandbox_info)}])
    tool = _make_tool("proj-1", db_client, sandbox_type="code")

    result = await tool._ensure_sandbox()

    assert result.sandbox_id == "sb-desktop"
    assert connect_calls == [("sb-desktop", "desktop")]
    create_mock.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_sandbox_running_attach_retries_before_recovery(monkeypatch):
    fake_sb = _FakeSandbox("sb-desktop")
    connect_calls = []
    resume_mock = AsyncMock()

    async def _get_or_start(sid, stype):
        connect_calls.append((sid, stype))
        if len(connect_calls) < 3:
            raise RuntimeError("temporary attach error")
        return fake_sb

    monkeypatch.setattr("sandbox.tool_base.SANDBOX_CONNECT_RETRY_COUNT", 2)
    monkeypatch.setattr("sandbox.tool_base.SANDBOX_CONNECT_RETRY_BACKOFF_SECONDS", 0.0)
    monkeypatch.setattr("sandbox.tool_base.get_or_start_sandbox", _get_or_start)
    monkeypatch.setattr("sandbox.tool_base.resume_or_create_sandbox", resume_mock)

    sandbox_info = {"id": "sb-desktop", "state": "running", "type": "desktop", "pass": "pw"}
    db_client = _FakeDBClient(select_data=[{"sandbox": json.dumps(sandbox_info)}])
    tool = _make_tool("proj-1", db_client, sandbox_type="code")

    result = await tool._ensure_sandbox()

    assert result.sandbox_id == "sb-desktop"
    assert connect_calls == [
        ("sb-desktop", "desktop"),
        ("sb-desktop", "desktop"),
        ("sb-desktop", "desktop"),
    ]
    resume_mock.assert_not_called()


@pytest.mark.asyncio
async def test_ensure_sandbox_recovery_keeps_existing_desktop_type(monkeypatch):
    mapping_updates = []
    resume_calls = []
    create_mock = AsyncMock()
    fake_sb = _FakeSandbox("sb-desktop-recovered")

    async def _get_or_start(_sid, _stype):
        raise RuntimeError("attach failed")

    async def _resume(password, project_id, sandbox_type, sandbox_info):
        resume_calls.append(
            {
                "password": password,
                "project_id": project_id,
                "sandbox_type": sandbox_type,
                "sandbox_info": sandbox_info,
            }
        )
        return (fake_sb, "created")

    async def _redis_set(key, value, ex=None, nx=False, timeout=None):
        mapping_updates.append({"key": key, "value": value, "ex": ex, "nx": nx})
        return True

    monkeypatch.setattr("sandbox.tool_base.get_or_start_sandbox", _get_or_start)
    monkeypatch.setattr("sandbox.tool_base.resume_or_create_sandbox", _resume)
    monkeypatch.setattr("sandbox.tool_base.create_sandbox", create_mock)
    monkeypatch.setattr("sandbox.tool_base.redis.set", _redis_set)

    sandbox_info = {"id": "sb-desktop", "state": "running", "type": "desktop", "pass": "pw"}
    db_client = _FakeDBClient(select_data=[{"sandbox": json.dumps(sandbox_info)}])
    tool = _make_tool("proj-1", db_client, sandbox_type="code")

    async def _run_blocking(op, call, **kw):
        return call()
    tool._run_blocking_sandbox_call = _run_blocking

    result = await tool._ensure_sandbox()

    assert result.sandbox_id == "sb-desktop-recovered"
    assert resume_calls and resume_calls[0]["sandbox_type"] == "desktop"
    create_mock.assert_not_called()
    assert mapping_updates == [
        {
            "key": "sandbox:idmap:sb-desktop",
            "value": "sb-desktop-recovered",
            "ex": 3600,
            "nx": False,
        }
    ]
    _, payload = db_client._updates[-1]
    merged = json.loads(payload["sandbox"])
    assert merged["id"] == "sb-desktop-recovered"
    assert merged["type"] == "desktop"
    assert merged["mode"] == "desktop_realtime"


@pytest.mark.asyncio
async def test_ensure_sandbox_recovery_prefers_resuming_original_desktop(monkeypatch):
    mapping_updates = []
    fake_sb = _FakeSandbox("sb-desktop")

    async def _get_or_start(_sid, _stype):
        raise RuntimeError("attach failed")

    async def _resume(password, project_id, sandbox_type, sandbox_info):
        assert project_id == "proj-1"
        assert sandbox_type == "desktop"
        assert sandbox_info["id"] == "sb-desktop"
        return (fake_sb, "resumed")

    async def _redis_set(key, value, ex=None, nx=False, timeout=None):
        mapping_updates.append({"key": key, "value": value, "ex": ex, "nx": nx})
        return True

    monkeypatch.setattr("sandbox.tool_base.get_or_start_sandbox", _get_or_start)
    monkeypatch.setattr("sandbox.tool_base.resume_or_create_sandbox", _resume)
    monkeypatch.setattr("sandbox.tool_base.redis.set", _redis_set)

    sandbox_info = {"id": "sb-desktop", "state": "running", "type": "desktop", "pass": "pw"}
    db_client = _FakeDBClient(select_data=[{"sandbox": json.dumps(sandbox_info)}])
    tool = _make_tool("proj-1", db_client, sandbox_type="code")

    async def _run_blocking(op, call, **kw):
        return call()
    tool._run_blocking_sandbox_call = _run_blocking

    result = await tool._ensure_sandbox()

    assert result.sandbox_id == "sb-desktop"
    assert mapping_updates == []
    _, payload = db_client._updates[-1]
    merged = json.loads(payload["sandbox"])
    assert merged["id"] == "sb-desktop"
    assert merged["type"] == "desktop"
    assert merged["state"] == "running"


@pytest.mark.asyncio
async def test_run_blocking_call_self_heals_once_for_not_found(monkeypatch):
    db_client = _FakeDBClient()
    tool = _make_tool("proj-1", db_client, sandbox_type="code")

    monkeypatch.setattr("sandbox.tool_base.get_agent_run_context", lambda: ("run-1", "thread-1"))

    tool._ensure_sandbox = AsyncMock(return_value=None)

    call_counter = {"count": 0}

    def _flaky_call():
        call_counter["count"] += 1
        if call_counter["count"] == 1:
            raise RuntimeError("The sandbox was not found")
        return "ok"

    result = await tool._run_blocking_sandbox_call("files.write", _flaky_call, timeout_seconds=1.0)

    assert result == "ok"
    assert call_counter["count"] == 2
    tool._ensure_sandbox.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_blocking_call_not_found_self_heals_for_non_qwen_model(monkeypatch):
    db_client = _FakeDBClient()
    tool = _make_tool("proj-1", db_client, sandbox_type="code")

    monkeypatch.setattr("sandbox.tool_base.get_agent_run_context", lambda: ("run-2", "thread-1"))

    tool._ensure_sandbox = AsyncMock(return_value=None)

    call_counter = {"count": 0}

    def _flaky_not_found():
        call_counter["count"] += 1
        if call_counter["count"] == 1:
            raise RuntimeError("sandbox not found")
        return "ok"

    result = await tool._run_blocking_sandbox_call("files.write", _flaky_not_found, timeout_seconds=1.0)

    assert result == "ok"
    assert call_counter["count"] == 2
    tool._ensure_sandbox.assert_awaited_once()


@pytest.mark.asyncio
async def test_run_blocking_call_non_not_found_error_does_not_self_heal(monkeypatch):
    db_client = _FakeDBClient()
    tool = _make_tool("proj-1", db_client, sandbox_type="code")

    monkeypatch.setattr("sandbox.tool_base.get_agent_run_context", lambda: ("run-3", "thread-1"))
    tool._ensure_sandbox = AsyncMock(return_value=None)

    call_counter = {"count": 0}

    def _always_fail_with_other_error():
        call_counter["count"] += 1
        raise RuntimeError("permission denied")

    with pytest.raises(RuntimeError):
        await tool._run_blocking_sandbox_call("files.write", _always_fail_with_other_error, timeout_seconds=1.0)

    assert call_counter["count"] == 1
    tool._ensure_sandbox.assert_not_awaited()


@pytest.mark.asyncio
async def test_ensure_sandbox_shadow_clone_strict_uses_bound_lease(monkeypatch):
    db_client = _FakeDBClient()
    tool = _make_tool("proj-1", db_client, sandbox_type="code")
    tool._shadow_clone_run_id = "run-strict"
    tool._strict_sandbox_mode = True
    tool._shared_shadow_clone_session = get_or_create_shadow_clone_session_state(
        project_id="proj-1",
        run_id="run-strict",
        sandbox_type="code",
    )
    tool._ensure_lock = tool._shared_shadow_clone_session.ensure_lock

    fake_sb = _FakeSandbox("sb-bound")
    lease = {
        "run_id": "run-strict",
        "project_id": "proj-1",
        "sandbox_id": "sb-bound",
        "sandbox_type": "desktop",
        "sandbox_info": {
            "id": "sb-bound",
            "type": "desktop",
            "pass": "pw",
        },
    }

    async def _get_lease(run_id):
        assert run_id == "run-strict"
        return lease

    async def _attach(_client, *, lease):
        assert lease["sandbox_id"] == "sb-bound"
        return fake_sb

    async def _unexpected_create(*_args, **_kwargs):
        raise AssertionError("strict Shadow Clone path must not create a new sandbox")

    async def _run_blocking(op, call, **kw):
        return call()

    monkeypatch.setattr(
        "agentscope_integration.shadow_clone.sandbox_lease.get_run_sandbox_lease",
        _get_lease,
    )
    monkeypatch.setattr("sandbox.api.attach_shadow_clone_bound_sandbox", _attach)
    monkeypatch.setattr("sandbox.tool_base.create_sandbox", _unexpected_create)
    monkeypatch.setattr("sandbox.tool_base.resume_or_create_sandbox", _unexpected_create)
    tool._run_blocking_sandbox_call = _run_blocking

    result = await tool._ensure_sandbox()

    assert result.sandbox_id == "sb-bound"
    assert tool.sandbox_id == "sb-bound"
    assert tool._get_runtime_sandbox_pass() == "pw"
    assert tool.sandbox_type == "desktop"


@pytest.mark.asyncio
async def test_ensure_sandbox_shadow_clone_strict_restores_skills_runtime_when_manifest_requires_it(monkeypatch):
    import sandbox.tool_base as tool_base_module

    db_client = _FakeDBClient()
    tool = _make_tool("proj-1", db_client, sandbox_type="code")
    tool._shadow_clone_run_id = "run-strict-skills"
    tool._strict_sandbox_mode = True
    tool._shared_shadow_clone_session = get_or_create_shadow_clone_session_state(
        project_id="proj-1",
        run_id="run-strict-skills",
        sandbox_type="code",
    )
    tool._ensure_lock = tool._shared_shadow_clone_session.ensure_lock

    runtime_state = {"skills_ready": False}
    writes = []
    commands = []
    command_timeouts = []
    blocking_calls = []

    class _FakeSkillFiles:
        def list(self, path):
            if path == "/workspace/skills" and runtime_state["skills_ready"]:
                return []
            raise FileNotFoundError(path)

        def read(self, path, format=None):
            if path == "/workspace/skills/skills_metadata.json" and runtime_state["skills_ready"]:
                return b'{"skills":[]}' if format == "bytes" else '{"skills":[]}'
            raise FileNotFoundError(path)

        def write(self, path, payload):
            writes.append((path, payload))
            return None

    class _FakeSkillCommands:
        def run(self, command, **kwargs):
            commands.append(command)
            command_timeouts.append(kwargs.get("timeout"))
            runtime_state["skills_ready"] = True
            return SimpleNamespace(exit_code=0, stdout="ok", stderr="")

    fake_sb = SimpleNamespace(
        sandbox_id="sb-bound-skills",
        files=_FakeSkillFiles(),
        commands=_FakeSkillCommands(),
    )
    lease = {
        "run_id": "run-strict-skills",
        "project_id": "proj-1",
        "sandbox_id": "sb-bound-skills",
        "sandbox_type": "code",
        "sandbox_info": {
            "id": "sb-bound-skills",
            "type": "code",
            "pass": "pw",
        },
        "environment_manifest": {
            "prepared_by": "shadow_clone_preconfirm_prepare",
            "bootstrap": {
                "status": "ready",
                "mode": "copy",
                "target": "/workspace/claude_skills_sandbox_bootstrap.py",
                "metadata_path": "/workspace/skills/skills_metadata.json",
            },
            "skills": {
                "ready": True,
                "workspace_dir": "/workspace/skills",
            },
        },
    }

    async def _get_lease(run_id):
        assert run_id == "run-strict-skills"
        return lease

    async def _attach(_client, *, lease):
        assert lease["sandbox_id"] == "sb-bound-skills"
        return fake_sb

    async def _persist_artifact(*args, **kwargs):
        return None

    async def _run_blocking(_op, call, **kwargs):
        blocking_calls.append((_op, kwargs))
        return call()

    monkeypatch.setattr(
        "agentscope_integration.shadow_clone.sandbox_lease.get_run_sandbox_lease",
        _get_lease,
    )
    monkeypatch.setattr("sandbox.api.attach_shadow_clone_bound_sandbox", _attach)
    monkeypatch.setattr(tool, "_persist_workspace_artifact", _persist_artifact)
    tool._run_blocking_sandbox_call = _run_blocking

    result = await tool._ensure_sandbox()

    assert result.sandbox_id == "sb-bound-skills"
    assert writes[0][0] == "/workspace/claude_skills_sandbox_bootstrap.py"
    assert "python3 /workspace/claude_skills_sandbox_bootstrap.py --mode copy --force" in commands
    assert command_timeouts == [tool_base_module.CLAUDE_SKILLS_BOOTSTRAP_REMOTE_TIMEOUT_SECONDS]
    assert (
        "commands.run",
        {"timeout_seconds": tool_base_module.CLAUDE_SKILLS_BOOTSTRAP_LOCAL_TIMEOUT_SECONDS},
    ) in blocking_calls
    assert runtime_state["skills_ready"] is True


@pytest.mark.asyncio
async def test_ensure_sandbox_shadow_clone_strict_rebinds_cached_handle_when_lease_sandbox_changes(monkeypatch):
    db_client = _FakeDBClient()
    tool = _make_tool("proj-1", db_client, sandbox_type="desktop")
    tool._shadow_clone_run_id = "run-strict-rebind"
    tool._strict_sandbox_mode = True
    tool._shared_shadow_clone_session = get_or_create_shadow_clone_session_state(
        project_id="proj-1",
        run_id="run-strict-rebind",
        sandbox_type="desktop",
    )
    tool._ensure_lock = tool._shared_shadow_clone_session.ensure_lock
    tool._bind_runtime_state(
        sandbox=_FakeSandbox("sb-old"),
        sandbox_id="sb-old",
        sandbox_pass="pw-old",
        sandbox_type="desktop",
    )

    lease = {
        "run_id": "run-strict-rebind",
        "project_id": "proj-1",
        "sandbox_id": "sb-new",
        "sandbox_type": "desktop",
        "sandbox_info": {
            "id": "sb-new",
            "type": "desktop",
            "pass": "pw-new",
        },
    }
    attach_calls = []

    async def _get_lease(run_id):
        assert run_id == "run-strict-rebind"
        return lease

    async def _attach(_client, *, lease):
        attach_calls.append(lease["sandbox_id"])
        return _FakeSandbox("sb-new")

    async def _run_blocking(_op, call, **_kw):
        return call()

    monkeypatch.setattr(
        "agentscope_integration.shadow_clone.sandbox_lease.get_run_sandbox_lease",
        _get_lease,
    )
    monkeypatch.setattr("sandbox.api.attach_shadow_clone_bound_sandbox", _attach)
    tool._run_blocking_sandbox_call = _run_blocking

    result = await tool._ensure_sandbox()

    assert attach_calls == ["sb-new"]
    assert result.sandbox_id == "sb-new"
    assert tool.sandbox_id == "sb-new"
    assert tool._get_runtime_sandbox_pass() == "pw-new"


@pytest.mark.asyncio
async def test_shadow_clone_strict_tools_share_runtime_session_state(monkeypatch):
    db_client = _FakeDBClient()
    shared_state = get_or_create_shadow_clone_session_state(
        project_id="proj-1",
        run_id="run-shared",
        sandbox_type="desktop",
    )

    first = _make_tool("proj-1", db_client, sandbox_type="desktop")
    first._shadow_clone_run_id = "run-shared"
    first._strict_sandbox_mode = True
    first._shared_shadow_clone_session = shared_state
    first._ensure_lock = shared_state.ensure_lock

    second = _make_tool("proj-1", db_client, sandbox_type="desktop")
    second._shadow_clone_run_id = "run-shared"
    second._strict_sandbox_mode = True
    second._shared_shadow_clone_session = shared_state
    second._ensure_lock = shared_state.ensure_lock

    sandbox_obj = _FakeSandbox("shared-sandbox")
    first._bind_runtime_state(
        sandbox=sandbox_obj,
        sandbox_id="shared-sandbox",
        sandbox_pass="shared-pass",
        sandbox_type="desktop",
    )

    assert second.sandbox is sandbox_obj
    assert second.sandbox_id == "shared-sandbox"
    assert second._get_runtime_sandbox_pass() == "shared-pass"

    second._clear_runtime_sandbox()
    assert first._get_runtime_sandbox() is None


@pytest.mark.asyncio
async def test_sync_workspace_artifacts_from_sandbox_persists_visible_files(monkeypatch):
    db_client = _FakeDBClient()
    tool = _make_tool("proj-sync", db_client, sandbox_type="code")

    class _FileEntry:
        def __init__(self, name: str, size: int, mod_time: str):
            self.name = name
            self.size = size
            self.mod_time = mod_time
            self.is_dir = False

    tool._sandbox = SimpleNamespace(
        files=SimpleNamespace(
            list=lambda path: [_FileEntry("out.txt", 5, "2026-03-17T00:00:00Z")]
            if path == "/workspace"
            else [],
            read=lambda path, format="bytes": b"hello",
        )
    )

    async def _run_blocking(_operation, call, **_kwargs):
        return call()

    persisted = []

    async def _fake_persist(path, content, *, source, **_kwargs):
        persisted.append((path, bytes(content), source))

    monkeypatch.setattr(tool, "_run_blocking_sandbox_call", _run_blocking, raising=False)
    monkeypatch.setattr(tool, "_persist_workspace_artifact", _fake_persist, raising=False)
    monkeypatch.setattr(
        "sandbox.tool_base.workspace_artifacts.get_artifact_record",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "sandbox.tool_base.workspace_artifacts.collect_file_paths",
        AsyncMock(return_value=[]),
    )

    await tool._sync_workspace_artifacts_from_sandbox(source="unit-test")

    assert persisted == [("/workspace/out.txt", b"hello", "unit-test")]


@pytest.mark.asyncio
async def test_sync_workspace_artifacts_from_desktop_sandbox_persists_visible_files(monkeypatch):
    db_client = _FakeDBClient()
    tool = _make_tool("proj-sync-desktop", db_client, sandbox_type="desktop")

    class _FileEntry:
        def __init__(self, name: str, size: int, mod_time: str):
            self.name = name
            self.size = size
            self.mod_time = mod_time
            self.is_dir = False

    tool._sandbox = SimpleNamespace(
        files=SimpleNamespace(
            list=lambda path: [_FileEntry("report.md", 7, "2026-03-17T00:01:00Z")]
            if path == "/workspace"
            else [],
            read=lambda path, format="bytes": b"desktop",
        )
    )

    async def _run_blocking(_operation, call, **_kwargs):
        return call()

    persisted = []

    async def _fake_persist(path, content, *, source, **_kwargs):
        persisted.append((path, bytes(content), source))

    monkeypatch.setattr(tool, "_run_blocking_sandbox_call", _run_blocking, raising=False)
    monkeypatch.setattr(tool, "_persist_workspace_artifact", _fake_persist, raising=False)
    monkeypatch.setattr(
        "sandbox.tool_base.workspace_artifacts.get_artifact_record",
        AsyncMock(return_value=None),
    )
    monkeypatch.setattr(
        "sandbox.tool_base.workspace_artifacts.collect_file_paths",
        AsyncMock(return_value=[]),
    )

    await tool._sync_workspace_artifacts_from_sandbox(source="desktop-sync")

    assert persisted == [("/workspace/report.md", b"desktop", "desktop-sync")]
