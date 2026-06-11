"""Unit tests for sandbox/renewal.py – renew_expiring_sandboxes."""

import json
import sys
import types
from datetime import datetime, timezone, timedelta
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

# Fake ppio_sandbox.core so sandbox.sandbox can import
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

from sandbox import renewal as renewal_mod


# ---------------------------------------------------------------------------
# Fake DB client
# ---------------------------------------------------------------------------

class _FakeRenewalTableQuery:
    def __init__(self, client):
        self._client = client
        self._project_id = None
        self._pending_update = None

    def select(self, *_a, **_kw):
        return self

    def eq(self, field, value):
        if field == "project_id":
            self._project_id = value
        return self

    async def execute(self):
        # If there's a pending update, record it now that project_id is set
        if self._pending_update is not None:
            self._client._updates.append((self._project_id, self._pending_update))
            self._pending_update = None
        return SimpleNamespace(data=self._client._rows)

    def update(self, payload):
        self._pending_update = payload
        return self


class _FakeRenewalClient:
    def __init__(self, rows):
        self._rows = rows
        self._updates = []

    def table(self, name):
        assert name == "projects"
        return _FakeRenewalTableQuery(self)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_row(project_id, sandbox_info):
    return {"project_id": project_id, "sandbox": json.dumps(sandbox_info)}


def _days_ago(n):
    return (datetime.now(timezone.utc) - timedelta(days=n)).isoformat()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_renew_empty_table():
    client = _FakeRenewalClient([])
    result = await renewal_mod.renew_expiring_sandboxes(client)
    assert result == {"renewed": [], "failed": [], "skipped": 0}


@pytest.mark.asyncio
async def test_renew_skips_running_sandbox():
    client = _FakeRenewalClient([
        _make_row("p1", {"state": "running", "id": "sb-1"}),
    ])
    result = await renewal_mod.renew_expiring_sandboxes(client)
    assert result["skipped"] == 1
    assert result["renewed"] == []


@pytest.mark.asyncio
async def test_renew_skips_recently_paused():
    client = _FakeRenewalClient([
        _make_row("p1", {"state": "paused", "id": "sb-1", "paused_at": _days_ago(5)}),
    ])
    result = await renewal_mod.renew_expiring_sandboxes(client)
    assert result["skipped"] == 1


@pytest.mark.asyncio
async def test_renew_skips_no_paused_at():
    client = _FakeRenewalClient([
        _make_row("p1", {"state": "paused", "id": "sb-1"}),
    ])
    result = await renewal_mod.renew_expiring_sandboxes(client)
    assert result["skipped"] == 1


@pytest.mark.asyncio
async def test_renew_skips_invalid_paused_at():
    client = _FakeRenewalClient([
        _make_row("p1", {"state": "paused", "id": "sb-1", "paused_at": "garbage"}),
    ])
    result = await renewal_mod.renew_expiring_sandboxes(client)
    assert result["skipped"] == 1


@pytest.mark.asyncio
async def test_renew_skips_no_sandbox_id():
    client = _FakeRenewalClient([
        _make_row("p1", {"state": "paused", "paused_at": _days_ago(30)}),
    ])
    result = await renewal_mod.renew_expiring_sandboxes(client)
    assert result["skipped"] == 1


@pytest.mark.asyncio
async def test_renew_clones_expired_sandbox(monkeypatch):
    clone_calls = []
    pause_calls = []
    delete_calls = []

    async def _clone(sid, count=1, timeout=3600):
        clone_calls.append(sid)
        return ("sb-new-clone", "tmpl-snap")

    async def _pause(sid):
        pause_calls.append(sid)
        return True

    async def _delete(sid):
        delete_calls.append(sid)

    monkeypatch.setattr(renewal_mod, "clone_sandbox", _clone)
    monkeypatch.setattr(renewal_mod, "pause_sandbox", _pause)
    monkeypatch.setattr(renewal_mod, "delete_sandbox", _delete)

    client = _FakeRenewalClient([
        _make_row("p1", {"state": "paused", "id": "sb-old", "paused_at": _days_ago(30)}),
    ])
    result = await renewal_mod.renew_expiring_sandboxes(client)

    assert len(result["renewed"]) == 1
    assert result["renewed"][0]["old_id"] == "sb-old"
    assert result["renewed"][0]["new_id"] == "sb-new-clone"
    assert clone_calls == ["sb-old"]
    assert delete_calls == ["sb-old"]


@pytest.mark.asyncio
async def test_renew_pause_new_clone(monkeypatch):
    pause_calls = []

    async def _clone(sid, count=1, timeout=3600):
        return ("sb-cloned", "tmpl-1")

    async def _pause(sid):
        pause_calls.append(sid)
        return True

    async def _delete(sid):
        pass

    monkeypatch.setattr(renewal_mod, "clone_sandbox", _clone)
    monkeypatch.setattr(renewal_mod, "pause_sandbox", _pause)
    monkeypatch.setattr(renewal_mod, "delete_sandbox", _delete)

    client = _FakeRenewalClient([
        _make_row("p1", {"state": "paused", "id": "sb-x", "paused_at": _days_ago(30)}),
    ])
    await renewal_mod.renew_expiring_sandboxes(client)
    assert "sb-cloned" in pause_calls


@pytest.mark.asyncio
async def test_renew_clone_failure_recorded(monkeypatch):
    async def _clone(sid, count=1, timeout=3600):
        raise RuntimeError("clone boom")

    monkeypatch.setattr(renewal_mod, "clone_sandbox", _clone)

    client = _FakeRenewalClient([
        _make_row("p1", {"state": "paused", "id": "sb-fail", "paused_at": _days_ago(30)}),
    ])
    result = await renewal_mod.renew_expiring_sandboxes(client)
    assert len(result["failed"]) == 1
    assert "clone boom" in result["failed"][0]["error"]


@pytest.mark.asyncio
async def test_renew_delete_old_failure_non_fatal(monkeypatch):
    async def _clone(sid, count=1, timeout=3600):
        return ("sb-new", "tmpl-1")

    async def _pause(sid):
        return True

    async def _delete(sid):
        raise RuntimeError("delete failed")

    monkeypatch.setattr(renewal_mod, "clone_sandbox", _clone)
    monkeypatch.setattr(renewal_mod, "pause_sandbox", _pause)
    monkeypatch.setattr(renewal_mod, "delete_sandbox", _delete)

    client = _FakeRenewalClient([
        _make_row("p1", {"state": "paused", "id": "sb-old", "paused_at": _days_ago(30)}),
    ])
    result = await renewal_mod.renew_expiring_sandboxes(client)
    assert len(result["renewed"]) == 1


@pytest.mark.asyncio
async def test_renew_multiple_projects(monkeypatch):
    async def _clone(sid, count=1, timeout=3600):
        return (f"{sid}-clone", "tmpl")

    async def _pause(sid):
        return True

    async def _delete(sid):
        pass

    monkeypatch.setattr(renewal_mod, "clone_sandbox", _clone)
    monkeypatch.setattr(renewal_mod, "pause_sandbox", _pause)
    monkeypatch.setattr(renewal_mod, "delete_sandbox", _delete)

    client = _FakeRenewalClient([
        _make_row("p1", {"state": "paused", "id": "sb-1", "paused_at": _days_ago(30)}),
        _make_row("p2", {"state": "paused", "id": "sb-2", "paused_at": _days_ago(5)}),
        _make_row("p3", {"state": "running", "id": "sb-3"}),
    ])
    result = await renewal_mod.renew_expiring_sandboxes(client)
    assert len(result["renewed"]) == 1
    assert result["skipped"] == 2


@pytest.mark.asyncio
async def test_renew_updates_db_with_clone_metadata(monkeypatch):
    async def _clone(sid, count=1, timeout=3600):
        return ("sb-new", "tmpl-snap")

    async def _pause(sid):
        return True

    async def _delete(sid):
        pass

    monkeypatch.setattr(renewal_mod, "clone_sandbox", _clone)
    monkeypatch.setattr(renewal_mod, "pause_sandbox", _pause)
    monkeypatch.setattr(renewal_mod, "delete_sandbox", _delete)

    client = _FakeRenewalClient([
        _make_row("p1", {"state": "paused", "id": "sb-old", "paused_at": _days_ago(30)}),
    ])
    await renewal_mod.renew_expiring_sandboxes(client)

    assert len(client._updates) == 1
    pid, payload = client._updates[0]
    assert pid == "p1"
    updated = json.loads(payload["sandbox"])
    assert updated["id"] == "sb-new"
    assert updated["cloned_from"] == "sb-old"
    assert updated["snapshot_template_id"] == "tmpl-snap"
    assert updated["state"] == "paused"
