"""Cross-module integration tests for sandbox persistence (pause/resume/renewal)."""

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

from sandbox.sandbox import pause_sandbox, resume_or_create_sandbox
from sandbox import renewal as renewal_mod


# ---------------------------------------------------------------------------
# Shared fake DB that tracks state across operations
# ---------------------------------------------------------------------------

class _FakeTableQuery:
    def __init__(self, client):
        self._client = client
        self._project_id = None
        self._pending_update = None

    def select(self, *a, **kw):
        return self

    def eq(self, field, value):
        if field == "project_id":
            self._project_id = value
        return self

    def update(self, payload):
        # Store for later - eq() may come after
        self._pending_update = payload
        return self

    async def execute(self):
        # If there's a pending update, persist it
        if self._pending_update is not None and self._project_id:
            sandbox_json = self._pending_update.get("sandbox")
            if sandbox_json and self._project_id in self._client._store:
                self._client._store[self._project_id] = json.loads(sandbox_json)
            self._pending_update = None
            return SimpleNamespace(data=[{}])

        if self._project_id and self._project_id in self._client._store:
            return SimpleNamespace(data=[{
                "project_id": self._project_id,
                "sandbox": json.dumps(self._client._store[self._project_id]),
            }])
        # For select-all (renewal scan)
        if self._project_id is None:
            rows = []
            for pid, info in self._client._store.items():
                rows.append({"project_id": pid, "sandbox": json.dumps(info)})
            return SimpleNamespace(data=rows)
        return SimpleNamespace(data=[])


class _IntegrationClient:
    def __init__(self, store=None):
        self._store = store or {}

    def table(self, name):
        return _FakeTableQuery(self)


class _FakeSandbox:
    def __init__(self, sandbox_id):
        self.sandbox_id = sandbox_id


# ---------------------------------------------------------------------------
# Integration test 1: full pause -> resume cycle
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_full_pause_resume_cycle(monkeypatch):
    """pause_sandbox -> DB state='paused' -> resume_or_create_sandbox -> resumed."""
    client = _IntegrationClient({
        "proj-1": {"id": "sb-1", "state": "running", "type": "desktop"},
    })

    # Step 1: Pause
    paused = await pause_sandbox("sb-1")
    assert paused is True

    # Simulate DB update (as the API endpoint would do)
    info = client._store["proj-1"]
    info["state"] = "paused"
    info["paused_at"] = datetime.now(timezone.utc).isoformat()

    # Step 2: Resume
    fake_sb = _FakeSandbox("sb-1")

    async def _get_or_start(sid, stype):
        return fake_sb

    monkeypatch.setattr("sandbox.sandbox.get_or_start_sandbox", _get_or_start)

    sandbox_info = client._store["proj-1"]
    obj, action = await resume_or_create_sandbox("pw", "proj-1", sandbox_info=sandbox_info)
    assert action == "resumed"
    assert obj.sandbox_id == "sb-1"


# ---------------------------------------------------------------------------
# Integration test 2: full renewal cycle
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_full_renewal_cycle(monkeypatch):
    """Expired sandbox -> renew_expiring_sandboxes -> new clone in DB, old deleted."""
    old_paused_at = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    client = _IntegrationClient({
        "proj-1": {
            "id": "sb-old",
            "state": "paused",
            "paused_at": old_paused_at,
            "type": "desktop",
        },
    })

    delete_calls = []

    async def _clone(sid, count=1, timeout=3600):
        return ("sb-renewed", "tmpl-snap")

    async def _pause(sid):
        return True

    async def _delete(sid):
        delete_calls.append(sid)

    monkeypatch.setattr(renewal_mod, "clone_sandbox", _clone)
    monkeypatch.setattr(renewal_mod, "pause_sandbox", _pause)
    monkeypatch.setattr(renewal_mod, "delete_sandbox", _delete)

    result = await renewal_mod.renew_expiring_sandboxes(client)

    assert len(result["renewed"]) == 1
    assert result["renewed"][0]["new_id"] == "sb-renewed"
    assert delete_calls == ["sb-old"]

    # Verify DB was updated
    stored = client._store["proj-1"]
    assert stored["id"] == "sb-renewed"
    assert stored["cloned_from"] == "sb-old"
    assert stored["snapshot_template_id"] == "tmpl-snap"


# ---------------------------------------------------------------------------
# Integration test 3: resume expired falls back to create
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_resume_expired_falls_back_to_create(monkeypatch):
    """Resume raises -> create_sandbox called -> returns new sandbox."""
    fake_new = _FakeSandbox("sb-fresh")

    async def _get_or_start(sid, stype):
        raise RuntimeError("sandbox expired")

    async def _create(pw, pid, stype):
        return fake_new

    monkeypatch.setattr("sandbox.sandbox.get_or_start_sandbox", _get_or_start)
    monkeypatch.setattr("sandbox.sandbox.create_sandbox", _create)

    sandbox_info = {"id": "sb-expired", "state": "paused"}
    obj, action = await resume_or_create_sandbox("pw", "proj-1", sandbox_info=sandbox_info)
    assert action == "created"
    assert obj.sandbox_id == "sb-fresh"
