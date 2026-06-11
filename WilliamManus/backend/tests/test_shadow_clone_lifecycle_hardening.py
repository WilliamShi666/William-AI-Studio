import sys
import types
from pathlib import Path

import pytest


BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))
SERVICES_DIR = str(BACKEND_ROOT / "services")


if "structlog" not in sys.modules:
    class _DummyBoundLogger:
        def info(self, *args, **kwargs):
            return None

        def warning(self, *args, **kwargs):
            return None

        def error(self, *args, **kwargs):
            return None

        def debug(self, *args, **kwargs):
            return None

    class _DummyProcessorFormatter:
        @staticmethod
        def wrap_for_formatter(*args, **kwargs):
            return None

    sys.modules["structlog"] = types.SimpleNamespace(
        configure=lambda **kwargs: None,
        get_logger=lambda *args, **kwargs: _DummyBoundLogger(),
        stdlib=types.SimpleNamespace(
            add_log_level=lambda *args, **kwargs: None,
            PositionalArgumentsFormatter=lambda *args, **kwargs: None,
            ProcessorFormatter=_DummyProcessorFormatter,
            LoggerFactory=lambda *args, **kwargs: None,
            BoundLogger=_DummyBoundLogger,
        ),
        processors=types.SimpleNamespace(TimeStamper=lambda *args, **kwargs: None),
        contextvars=types.SimpleNamespace(
            clear_contextvars=lambda: None,
            bind_contextvars=lambda **kwargs: None,
            get_contextvars=lambda: {},
        ),
    )

if "dramatiq" not in sys.modules:
    _dramatiq_middleware = types.SimpleNamespace(
        AsyncIO=lambda *args, **kwargs: None,
        Retries=lambda *args, **kwargs: None,
        TimeLimit=lambda *args, **kwargs: None,
    )
    sys.modules["dramatiq"] = types.SimpleNamespace(
        actor=lambda *args, **kwargs: (lambda func: func),
        set_broker=lambda *args, **kwargs: None,
        middleware=_dramatiq_middleware,
    )

if "dramatiq.brokers.redis" not in sys.modules:
    sys.modules["dramatiq.brokers.redis"] = types.SimpleNamespace(
        RedisBroker=lambda *args, **kwargs: types.SimpleNamespace(),
    )

if "sentry" not in sys.modules:
    sys.modules["sentry"] = types.SimpleNamespace(
        sentry=types.SimpleNamespace(set_tag=lambda *args, **kwargs: None),
    )

if "sentry_sdk" not in sys.modules:
    sys.modules["sentry_sdk"] = types.SimpleNamespace(init=lambda *args, **kwargs: None)

if "services" not in sys.modules:
    services_pkg = types.ModuleType("services")
    services_pkg.__path__ = [SERVICES_DIR]
    sys.modules["services"] = services_pkg
elif not hasattr(sys.modules["services"], "__path__"):
    sys.modules["services"].__path__ = [SERVICES_DIR]
elif SERVICES_DIR not in sys.modules["services"].__path__:
    sys.modules["services"].__path__.append(SERVICES_DIR)

if "services.redis" not in sys.modules:
    async def _noop_async(*_args, **_kwargs):
        return 1

    async def _noop_lrange(*_args, **_kwargs):
        return []

    services_redis_stub = types.SimpleNamespace(
        REDIS_KEY_TTL=3600,
        initialize_async=_noop_async,
        set=_noop_async,
        get=_noop_async,
        delete=_noop_async,
        expire=_noop_async,
        publish=_noop_async,
        rpush=_noop_async,
        lrange=_noop_lrange,
        eval_script=_noop_async,
    )
    sys.modules["services.redis"] = services_redis_stub
    setattr(sys.modules["services"], "redis", services_redis_stub)

if "services.postgresql" not in sys.modules:
    class _DummyDBConnection:
        @property
        async def client(self):
            return None

        async def initialize(self):
            return None

    services_pg_stub = types.ModuleType("services.postgresql")
    services_pg_stub.DBConnection = _DummyDBConnection
    sys.modules["services.postgresql"] = services_pg_stub
    setattr(sys.modules["services"], "postgresql", services_pg_stub)

if "services.langfuse" not in sys.modules:
    class _DummyTrace:
        def span(self, *args, **kwargs):
            return types.SimpleNamespace(end=lambda *a, **kw: None)

    services_langfuse_stub = types.ModuleType("services.langfuse")
    services_langfuse_stub.langfuse = types.SimpleNamespace(
        trace=lambda *args, **kwargs: _DummyTrace(),
    )
    sys.modules["services.langfuse"] = services_langfuse_stub
    setattr(sys.modules["services"], "langfuse", services_langfuse_stub)

if "services.run_capacity" not in sys.modules:
    async def _capacity_snapshot(*_args, **_kwargs):
        return {
            "enabled": False,
            "budget": 0,
            "in_use": 0,
            "remaining": 0,
            "requested_cost": 1,
            "can_admit": True,
            "capacity_kind": "regular",
            "acquired": True,
            "refreshed": False,
            "lease_ttl_seconds": 0,
        }

    def _capacity_limit_detail(*, mode=None, snapshot=None):
        snapshot = snapshot or {}
        return {
            "code": "server_concurrency_limit",
            "message": "Server concurrency budget is exhausted. Please retry shortly.",
            "capacity_kind": "shadow_clone" if str(mode or "").lower() in {"on", "auto"} else "regular",
            "budget": int(snapshot.get("budget") or 0),
            "in_use": int(snapshot.get("in_use") or 0),
            "remaining": int(snapshot.get("remaining") or 0),
            "requested_cost": int(snapshot.get("requested_cost") or 1),
        }

    services_run_capacity_stub = types.ModuleType("services.run_capacity")
    services_run_capacity_stub.build_run_capacity_limit_detail = _capacity_limit_detail
    services_run_capacity_stub.get_run_capacity_cost = (
        lambda mode=None: 3 if str(mode or "").lower() in {"on", "auto"} else 1
    )
    services_run_capacity_stub.is_server_concurrency_budget_enabled = lambda: False
    services_run_capacity_stub.get_safe_run_capacity_snapshot = _capacity_snapshot
    services_run_capacity_stub.refresh_run_capacity_lease = _capacity_snapshot
    services_run_capacity_stub.release_run_capacity_lease = _capacity_snapshot
    services_run_capacity_stub.try_acquire_run_capacity_lease = _capacity_snapshot
    sys.modules["services.run_capacity"] = services_run_capacity_stub
    setattr(sys.modules["services"], "run_capacity", services_run_capacity_stub)

if "agent" not in sys.modules:
    sys.modules["agent"] = types.ModuleType("agent")

if "agent.run" not in sys.modules:
    async def _dummy_run_agent(*_args, **_kwargs):
        if False:
            yield {}
        return

    agent_run_stub = types.ModuleType("agent.run")
    agent_run_stub.run_agent = _dummy_run_agent
    sys.modules["agent.run"] = agent_run_stub
    setattr(sys.modules["agent"], "run", agent_run_stub)

if "utils.retry" not in sys.modules:
    async def _retry(func):
        return await func()

    sys.modules["utils.retry"] = types.SimpleNamespace(retry=lambda func: _retry(func))

if "utils.agent_run_context" not in sys.modules:
    sys.modules["utils.agent_run_context"] = types.SimpleNamespace(
        clear_agent_run_context=lambda: None,
        get_agent_model_context=lambda: None,
        set_agent_run_context=lambda *args, **kwargs: None,
    )


import run_agent_background as rab_module
import agentscope_integration.shadow_clone.sandbox_lease as sandbox_lease


@pytest.mark.asyncio
async def test_mark_run_sandbox_unattachable_prefers_recovery_when_standby_exists(
    monkeypatch,
):
    lease = {
        "run_id": "run-shadow",
        "project_id": "project-shadow",
        "sandbox_id": "sb-active",
        "binding_state": "locked",
        "standby_sandbox_id": "sb-standby",
        "environment_status": "ready",
        "environment_ready": True,
    }

    async def _get_lease(_run_id: str):
        return dict(lease)

    async def _update_lease(_run_id: str, **patch):
        lease.update(patch)
        return dict(lease)

    monkeypatch.setattr(sandbox_lease, "get_run_sandbox_lease", _get_lease)
    monkeypatch.setattr(sandbox_lease, "update_run_sandbox_lease", _update_lease)

    updated = await sandbox_lease.mark_run_sandbox_unattachable(
        "run-shadow",
        expected_sandbox_id="sb-active",
        last_error="heartbeat failed",
    )

    assert updated["binding_state"] == "recovery_required"
    assert updated["environment_status"] == "recovering"
    assert updated["environment_ready"] is False
    assert updated["last_error"] == "heartbeat failed"


@pytest.mark.asyncio
async def test_mark_run_sandbox_unattachable_marks_lost_without_standby(monkeypatch):
    lease = {
        "run_id": "run-shadow",
        "project_id": "project-shadow",
        "sandbox_id": "sb-active",
        "binding_state": "locked",
        "standby_sandbox_id": None,
        "environment_status": "ready",
        "environment_ready": True,
    }

    async def _get_lease(_run_id: str):
        return dict(lease)

    async def _update_lease(_run_id: str, **patch):
        lease.update(patch)
        return dict(lease)

    monkeypatch.setattr(sandbox_lease, "get_run_sandbox_lease", _get_lease)
    monkeypatch.setattr(sandbox_lease, "update_run_sandbox_lease", _update_lease)

    updated = await sandbox_lease.mark_run_sandbox_unattachable(
        "run-shadow",
        expected_sandbox_id="sb-active",
        last_error="heartbeat failed",
    )

    assert updated["binding_state"] == "lost"
    assert updated["environment_status"] == "failed"
    assert updated["environment_ready"] is False


@pytest.mark.asyncio
async def test_extend_shadow_clone_timeout_degrades_active_lease_before_standby_continues(
    monkeypatch,
):
    lease = {
        "project_id": "proj-shadow",
        "sandbox_id": "sb-active",
        "sandbox_type": "desktop",
        "sandbox_info": {"state": "running"},
        "binding_state": "locked",
        "standby_sandbox_id": "sb-standby",
        "standby_sandbox_type": "desktop",
        "standby_sandbox_info": {"state": "running"},
    }
    extend_calls = []
    degradation_calls = []

    async def _get_lease(_run_id: str):
        return dict(lease)

    async def _mark_unattachable(_run_id: str, **kwargs):
        degradation_calls.append(kwargs)
        return {
            "binding_state": "recovery_required",
        }

    async def _extend(sandbox_id: str, sandbox_type: str, **kwargs):
        extend_calls.append((sandbox_id, sandbox_type, kwargs))
        if sandbox_id == "sb-active":
            raise RuntimeError("active sandbox unavailable")
        return object()

    monkeypatch.setattr(sandbox_lease, "get_run_sandbox_lease", _get_lease)
    monkeypatch.setattr(
        sandbox_lease,
        "mark_run_sandbox_unattachable",
        _mark_unattachable,
    )
    monkeypatch.setattr("sandbox.sandbox.extend_sandbox_timeout", _extend)

    result = await rab_module._extend_shadow_clone_sandbox_timeouts_once(
        project_id="proj-shadow",
        agent_run_id="run-shadow",
    )

    assert result is True
    assert [call[0] for call in extend_calls] == ["sb-active", "sb-standby"]
    assert degradation_calls == [
        {
            "expected_sandbox_id": "sb-active",
            "last_error": "Active Shadow Clone sandbox heartbeat failed: active sandbox unavailable",
            "standby_available": True,
        }
    ]


@pytest.mark.asyncio
async def test_refresh_redis_run_lock_reports_owner_mismatch(monkeypatch):
    async def _eval_script(*_args, **_kwargs):
        return -1

    monkeypatch.setattr(rab_module.redis, "eval_script", _eval_script)

    result = await rab_module._refresh_redis_run_lock("run-shadow", "owner-1")

    assert result == "owner_mismatch"


@pytest.mark.asyncio
async def test_cleanup_redis_run_lock_uses_compare_delete(monkeypatch):
    delete_calls = []
    eval_calls = []

    async def _eval_script(script, keys, args):
        eval_calls.append((script, keys, args))
        return -1

    async def _delete(*args, **kwargs):
        delete_calls.append((args, kwargs))
        return 1

    monkeypatch.setattr(rab_module.redis, "eval_script", _eval_script)
    monkeypatch.setattr(rab_module.redis, "delete", _delete, raising=False)

    await rab_module._cleanup_redis_run_lock("run-shadow", "owner-1")

    assert eval_calls
    assert delete_calls == []
