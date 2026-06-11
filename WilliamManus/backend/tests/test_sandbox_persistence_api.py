"""Endpoint tests for sandbox/api.py – pause, state, renew-expiring endpoints."""

import json
import sys
import types
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

# ---------------------------------------------------------------------------
# Stubs
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

from fastapi import HTTPException
from sandbox import api as sandbox_api
from sandbox.api import (
    ensure_project_sandbox_active,
    get_sandbox_state,
    pause_project_sandbox,
    renew_expiring_sandboxes_endpoint,
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


class _FakeRequest:
    """Minimal stand-in for FastAPI Request."""
    pass


# ---------------------------------------------------------------------------
# pause endpoint
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_pause_endpoint_success(monkeypatch):
    sandbox_info = {"id": "sb-1", "state": "running", "type": "desktop"}
    db_client = _FakeDBClient({"proj-1": {"sandbox": json.dumps(sandbox_info)}})
    monkeypatch.setattr(sandbox_api, "db", _FakeDBConnection(db_client))

    async def _pause(sid):
        return True

    monkeypatch.setattr(sandbox_api, "pause_sandbox", _pause)

    result = await pause_project_sandbox("proj-1", _FakeRequest())
    assert result["status"] == "paused"
    assert result["sandbox_id"] == "sb-1"
    assert len(db_client._updates) == 1


@pytest.mark.asyncio
async def test_pause_endpoint_already_paused(monkeypatch):
    sandbox_info = {"id": "sb-1", "state": "paused"}
    db_client = _FakeDBClient({"proj-1": {"sandbox": json.dumps(sandbox_info)}})
    monkeypatch.setattr(sandbox_api, "db", _FakeDBConnection(db_client))

    pause_calls = []

    async def _pause(sid):
        pause_calls.append(sid)
        return True

    monkeypatch.setattr(sandbox_api, "pause_sandbox", _pause)

    result = await pause_project_sandbox("proj-1", _FakeRequest())
    assert result["status"] == "already_paused"
    assert pause_calls == []


@pytest.mark.asyncio
async def test_pause_endpoint_no_project(monkeypatch):
    db_client = _FakeDBClient({})
    monkeypatch.setattr(sandbox_api, "db", _FakeDBConnection(db_client))

    with pytest.raises(HTTPException) as exc_info:
        await pause_project_sandbox("missing", _FakeRequest())
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_pause_endpoint_no_sandbox(monkeypatch):
    db_client = _FakeDBClient({"proj-1": {"sandbox": "{}"}})
    monkeypatch.setattr(sandbox_api, "db", _FakeDBConnection(db_client))

    with pytest.raises(HTTPException) as exc_info:
        await pause_project_sandbox("proj-1", _FakeRequest())
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_pause_endpoint_sdk_failure(monkeypatch):
    sandbox_info = {"id": "sb-1", "state": "running"}
    db_client = _FakeDBClient({"proj-1": {"sandbox": json.dumps(sandbox_info)}})
    monkeypatch.setattr(sandbox_api, "db", _FakeDBConnection(db_client))

    async def _pause(sid):
        return False

    monkeypatch.setattr(sandbox_api, "pause_sandbox", _pause)

    with pytest.raises(HTTPException) as exc_info:
        await pause_project_sandbox("proj-1", _FakeRequest())
    assert exc_info.value.status_code == 500


# ---------------------------------------------------------------------------
# state endpoint
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_state_endpoint_returns_info(monkeypatch):
    sandbox_info = {
        "id": "sb-1", "state": "running", "type": "desktop",
        "paused_at": None, "created_at": "2026-01-01T00:00:00Z",
    }
    db_client = _FakeDBClient({"proj-1": {"sandbox": json.dumps(sandbox_info)}})
    monkeypatch.setattr(sandbox_api, "db", _FakeDBConnection(db_client))

    result = await get_sandbox_state("proj-1", _FakeRequest())
    assert result["id"] == "sb-1"
    assert result["state"] == "running"
    assert result["type"] == "desktop"
    assert result["created_at"] == "2026-01-01T00:00:00Z"


@pytest.mark.asyncio
async def test_state_endpoint_no_project(monkeypatch):
    db_client = _FakeDBClient({})
    monkeypatch.setattr(sandbox_api, "db", _FakeDBConnection(db_client))

    with pytest.raises(HTTPException) as exc_info:
        await get_sandbox_state("missing", _FakeRequest())
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_state_endpoint_empty_sandbox(monkeypatch):
    db_client = _FakeDBClient({"proj-1": {"sandbox": "{}"}})
    monkeypatch.setattr(sandbox_api, "db", _FakeDBConnection(db_client))

    result = await get_sandbox_state("proj-1", _FakeRequest())
    assert result["state"] == "unknown"


@pytest.mark.asyncio
async def test_ensure_active_returns_canonical_sandbox_metadata(monkeypatch):
    original_info = {
        "id": "sb-old",
        "state": "running",
        "type": "desktop",
        "vnc_preview": "https://old-vnc.example",
        "sandbox_url": "https://old-vnc.example",
    }
    canonical_info = {
        "id": "sb-new",
        "state": "running",
        "type": "desktop",
        "vnc_preview": "https://new-vnc.example",
        "sandbox_url": "https://new-vnc.example",
        "last_accessed_at": datetime.now(timezone.utc).isoformat(),
    }
    db_client = _FakeDBClient(
        {
            "proj-1": {
                "sandbox": json.dumps(original_info),
                "account_id": "user-1",
                "is_public": False,
            }
        }
    )
    monkeypatch.setattr(sandbox_api, "db", _FakeDBConnection(db_client))

    async def _get_sandbox(_client, sandbox_id: str, *, project_id: str | None = None):
        assert sandbox_id == "sb-old"
        assert project_id == "proj-1"
        db_client._data["proj-1"]["sandbox"] = json.dumps(canonical_info)
        return SimpleNamespace(sandbox_id="sb-new")

    async def _touch(*_args, **_kwargs):
        return None

    monkeypatch.setattr(sandbox_api, "get_sandbox_by_id_safely", _get_sandbox)
    monkeypatch.setattr(sandbox_api, "_touch_project_sandbox_access", _touch)

    result = await ensure_project_sandbox_active("proj-1", _FakeRequest(), user_id="user-1")

    assert result["status"] == "success"
    assert result["sandbox_id"] == "sb-new"
    assert result["resolved_sandbox_id"] == "sb-new"
    assert result["sandbox"]["id"] == "sb-new"
    assert result["sandbox"]["vnc_preview"] == "https://new-vnc.example"


@pytest.mark.asyncio
async def test_ensure_active_prefers_shadow_clone_bound_sandbox_without_recreate(monkeypatch):
    sandbox_info = {
        "id": "sb-bound",
        "state": "running",
        "type": "desktop",
        "vnc_preview": "https://bound-vnc.example",
        "sandbox_url": "https://bound-vnc.example",
    }
    db_client = _FakeDBClient(
        {
            "proj-1": {
                "sandbox": json.dumps(sandbox_info),
                "account_id": "user-1",
                "is_public": False,
            }
        }
    )
    monkeypatch.setattr(sandbox_api, "db", _FakeDBConnection(db_client))

    lease = {
        "run_id": "run-1",
        "project_id": "proj-1",
        "sandbox_id": "sb-bound",
        "sandbox_type": "desktop",
        "binding_state": "locked",
        "sandbox_info": sandbox_info,
    }

    async def _preferred(project_id: str, *, requested_sandbox_id: str = "", include_recent: bool = False):
        assert project_id == "proj-1"
        assert requested_sandbox_id == "sb-bound"
        assert include_recent is True
        return lease

    async def _attach(_client, *, lease):
        assert lease["sandbox_id"] == "sb-bound"
        return SimpleNamespace(sandbox_id="sb-bound")

    async def _touch(*_args, **_kwargs):
        return None

    async def _unexpected_safe_get(*_args, **_kwargs):
        raise AssertionError("generic sandbox recovery path should not be used for strict Shadow Clone lease")

    monkeypatch.setattr(sandbox_api, "get_preferred_project_sandbox_lease", _preferred)
    monkeypatch.setattr(sandbox_api, "attach_shadow_clone_bound_sandbox", _attach)
    monkeypatch.setattr(sandbox_api, "_touch_project_sandbox_access", _touch)
    monkeypatch.setattr(sandbox_api, "get_sandbox_by_id_safely", _unexpected_safe_get)

    result = await ensure_project_sandbox_active("proj-1", _FakeRequest(), user_id="user-1")

    assert result["status"] == "success"
    assert result["resolved_sandbox_id"] == "sb-bound"
    assert result["sandbox"]["id"] == "sb-bound"


# ---------------------------------------------------------------------------
# renew endpoint
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_renew_endpoint_calls_renewal(monkeypatch):
    renewal_calls = []

    async def _renew(client, max_age_days=29):
        renewal_calls.append(max_age_days)
        return {"renewed": [], "failed": [], "skipped": 0}

    db_client = _FakeDBClient({})
    monkeypatch.setattr(sandbox_api, "db", _FakeDBConnection(db_client))
    monkeypatch.setattr(sandbox_api, "renew_expiring_sandboxes", _renew)
    monkeypatch.setattr(sandbox_api, "config", SimpleNamespace(SANDBOX_CLONE_RENEWAL_DAYS=29))

    result = await renew_expiring_sandboxes_endpoint(_FakeRequest())
    assert renewal_calls == [29]
    assert result["skipped"] == 0


@pytest.mark.asyncio
async def test_renew_endpoint_returns_result(monkeypatch):
    expected = {"renewed": [{"project_id": "p1"}], "failed": [], "skipped": 2}

    async def _renew(client, max_age_days=29):
        return expected

    db_client = _FakeDBClient({})
    monkeypatch.setattr(sandbox_api, "db", _FakeDBConnection(db_client))
    monkeypatch.setattr(sandbox_api, "renew_expiring_sandboxes", _renew)
    monkeypatch.setattr(sandbox_api, "config", SimpleNamespace(SANDBOX_CLONE_RENEWAL_DAYS=29))

    result = await renew_expiring_sandboxes_endpoint(_FakeRequest())
    assert result == expected


@pytest.mark.asyncio
async def test_renew_endpoint_uses_config_days(monkeypatch):
    renewal_calls = []

    async def _renew(client, max_age_days=29):
        renewal_calls.append(max_age_days)
        return {"renewed": [], "failed": [], "skipped": 0}

    db_client = _FakeDBClient({})
    monkeypatch.setattr(sandbox_api, "db", _FakeDBConnection(db_client))
    monkeypatch.setattr(sandbox_api, "renew_expiring_sandboxes", _renew)
    monkeypatch.setattr(sandbox_api, "config", SimpleNamespace(SANDBOX_CLONE_RENEWAL_DAYS=15))

    await renew_expiring_sandboxes_endpoint(_FakeRequest())
    assert renewal_calls == [15]
