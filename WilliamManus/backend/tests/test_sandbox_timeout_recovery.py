import asyncio
from contextlib import asynccontextmanager
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from sandbox import api as sandbox_api


class _FakeTableQuery:
    def __init__(self, client):
        self._client = client
        self._sandbox_id = None
        self._project_id = None

    def select(self, *_args, **_kwargs):
        return self

    def eq(self, field: str, value: str):
        if field == "(sandbox::jsonb ->> 'id')":
            self._sandbox_id = value
        elif field == "project_id":
            self._project_id = value
        return self

    async def execute(self):
        row = None
        if self._project_id:
            for candidate in self._client.rows_by_sandbox_id.values():
                if candidate.get("project_id") == self._project_id:
                    row = candidate
                    break
        elif self._sandbox_id:
            row = self._client.rows_by_sandbox_id.get(self._sandbox_id)
        data = [row] if row else []
        return SimpleNamespace(data=data)

    async def update(self, payload):
        self._client.updated_payloads.append((self._project_id, payload))

        sandbox_payload = payload.get("sandbox")
        if not sandbox_payload:
            return SimpleNamespace(data=[])

        sandbox_info = json.loads(sandbox_payload)
        project_id = self._project_id
        if not project_id:
            raise AssertionError("project_id filter must be set before update")

        for key, row in list(self._client.rows_by_sandbox_id.items()):
            if row.get("project_id") == project_id:
                del self._client.rows_by_sandbox_id[key]
                break

        self._client.rows_by_sandbox_id[str(sandbox_info.get("id"))] = {
            "project_id": project_id,
            "sandbox": sandbox_info,
        }
        return SimpleNamespace(data=[payload])


class _FakeClient:
    def __init__(self, rows_by_sandbox_id):
        self.rows_by_sandbox_id = rows_by_sandbox_id
        self.updated_payloads = []

    def table(self, name: str):
        assert name == "projects"
        return _FakeTableQuery(self)


class _FakeStream:
    def start(self):
        return None

    def get_url(self):
        return "https://stream.example"


class _FakeSandbox:
    def __init__(self, sandbox_id: str, *, running: bool = True):
        self.sandbox_id = sandbox_id
        self.stream = _FakeStream()
        self._running = running

    def is_running(self):
        return self._running


@pytest.fixture(autouse=True)
def _sandbox_recovery_defaults(monkeypatch):
    monkeypatch.setattr(sandbox_api, "SANDBOX_CONNECT_RETRY_COUNT", 0)
    monkeypatch.setattr(sandbox_api, "SANDBOX_CONNECT_RETRY_BACKOFF_SECONDS", 0.0)
    monkeypatch.setattr(sandbox_api, "SANDBOX_RECOVERY_MAX_ATTEMPTS", 1)

    async def _noop_store_mapping(*_args, **_kwargs):
        return None

    @asynccontextmanager
    async def _local_singleflight(_project_id: str):
        yield

    monkeypatch.setattr(sandbox_api, "_store_sandbox_id_mapping", _noop_store_mapping)
    monkeypatch.setattr(sandbox_api, "_project_recovery_singleflight", _local_singleflight)


@pytest.fixture(autouse=True)
def _inline_to_thread(monkeypatch):
    async def _run_inline(func, /, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr("sandbox.api.asyncio.to_thread", _run_inline)


@pytest.mark.asyncio
async def test_resolve_sandbox_id_flattens_multi_hop_chain(monkeypatch):
    stored_mappings = []

    async def _redis_get(key: str):
        chain = {
            "sandbox:idmap:sb-a": "sb-b",
            "sandbox:idmap:sb-b": "sb-c",
        }
        return chain.get(key)

    async def _store_mapping(old_id: str, new_id: str):
        stored_mappings.append((old_id, new_id))

    monkeypatch.setattr(sandbox_api.redis, "get", _redis_get)
    monkeypatch.setattr(sandbox_api, "_store_sandbox_id_mapping", _store_mapping)

    resolved = await sandbox_api._resolve_sandbox_id("sb-a")

    assert resolved == "sb-c"
    assert ("sb-a", "sb-c") in stored_mappings
    assert ("sb-b", "sb-c") in stored_mappings


@pytest.mark.asyncio
async def test_timeout_triggers_recreate_and_returns_new_sandbox(monkeypatch):
    client = _FakeClient(
        {
            "sb-old": {
                "project_id": "project-1",
                "sandbox": {"id": "sb-old", "type": "desktop"},
            }
        }
    )

    async def _resolve_sandbox_id(sandbox_id: str):
        return sandbox_id

    async def _always_timeout(*_args, **_kwargs):
        raise asyncio.TimeoutError("connect timeout")

    async def _create_sandbox(_password: str, project_id: str, sandbox_type: str):
        assert project_id == "project-1"
        assert sandbox_type == "desktop"
        return _FakeSandbox("sb-new")

    async def _resume_or_create(*_args, **_kwargs):
        raise sandbox_api.SandboxOriginalReuseExhausted(
            "sb-old",
            "desktop",
            "project-1",
            last_error=asyncio.TimeoutError("resume timeout"),
        )

    async def _run_blocking(_operation, call, **_kwargs):
        return call()

    mapping_updates = []

    async def _store_mapping(old_id: str, new_id: str):
        mapping_updates.append((old_id, new_id))

    monkeypatch.setattr(sandbox_api, "_resolve_sandbox_id", _resolve_sandbox_id)
    monkeypatch.setattr(sandbox_api, "get_or_start_sandbox", _always_timeout)
    monkeypatch.setattr(sandbox_api, "resume_or_create_sandbox", _resume_or_create)
    monkeypatch.setattr(sandbox_api, "create_sandbox", _create_sandbox)
    monkeypatch.setattr(sandbox_api, "_run_blocking_sandbox_call", _run_blocking)
    monkeypatch.setattr(sandbox_api, "_store_sandbox_id_mapping", _store_mapping)

    sandbox = await sandbox_api.get_sandbox_by_id_safely(client, "sb-old")

    assert getattr(sandbox, "sandbox_id", None) == "sb-new"
    assert client.updated_payloads, "project sandbox metadata should be updated after recreation"
    assert ("sb-old", "sb-new") in mapping_updates


@pytest.mark.asyncio
async def test_resume_timeout_then_original_reattach_succeeds_without_recreate(monkeypatch):
    client = _FakeClient(
        {
            "sb-old": {
                "project_id": "project-1",
                "sandbox": {"id": "sb-old", "type": "desktop", "state": "running"},
            }
        }
    )

    async def _resolve_sandbox_id(sandbox_id: str):
        return sandbox_id

    async def _always_timeout(*_args, **_kwargs):
        raise asyncio.TimeoutError("connect timeout")

    async def _resume_or_create(*_args, **_kwargs):
        return (_FakeSandbox("sb-old", running=True), "resumed")

    create_mock = AsyncMock()

    async def _run_blocking(_operation, call, **_kwargs):
        return call()

    mapping_updates = []

    async def _store_mapping(old_id: str, new_id: str):
        mapping_updates.append((old_id, new_id))

    monkeypatch.setattr(sandbox_api, "_resolve_sandbox_id", _resolve_sandbox_id)
    monkeypatch.setattr(sandbox_api, "get_or_start_sandbox", _always_timeout)
    monkeypatch.setattr(sandbox_api, "resume_or_create_sandbox", _resume_or_create)
    monkeypatch.setattr(sandbox_api, "create_sandbox", create_mock)
    monkeypatch.setattr(sandbox_api, "_run_blocking_sandbox_call", _run_blocking)
    monkeypatch.setattr(sandbox_api, "_store_sandbox_id_mapping", _store_mapping)

    sandbox = await sandbox_api.get_sandbox_by_id_safely(client, "sb-old")

    assert getattr(sandbox, "sandbox_id", None) == "sb-old"
    create_mock.assert_not_called()
    assert mapping_updates == []
    _, payload = client.updated_payloads[-1]
    updated = json.loads(payload["sandbox"])
    assert updated["id"] == "sb-old"
    assert updated["type"] == "desktop"
    assert updated["state"] == "running"


@pytest.mark.asyncio
async def test_connect_timeout_without_recovery_returns_504(monkeypatch):
    monkeypatch.setattr(sandbox_api, "SANDBOX_RECOVERY_MAX_ATTEMPTS", 0)

    client = _FakeClient(
        {
            "sb-timeout": {
                "project_id": "project-timeout",
                "sandbox": {"id": "sb-timeout", "type": "desktop"},
            }
        }
    )

    async def _resolve_sandbox_id(sandbox_id: str):
        return sandbox_id

    async def _always_timeout(*_args, **_kwargs):
        raise asyncio.TimeoutError("connect timeout")

    monkeypatch.setattr(sandbox_api, "_resolve_sandbox_id", _resolve_sandbox_id)
    monkeypatch.setattr(sandbox_api, "get_or_start_sandbox", _always_timeout)

    with pytest.raises(HTTPException) as exc_info:
        await sandbox_api.get_sandbox_by_id_safely(client, "sb-timeout")

    assert exc_info.value.status_code == 504
    assert exc_info.value.detail["error_code"] == "SANDBOX_CONNECT_TIMEOUT"
    assert exc_info.value.detail["recoverable"] is True


@pytest.mark.asyncio
async def test_resolved_id_fallback_uses_canonical_metadata_id(monkeypatch):
    client = _FakeClient(
        {
            "sb-requested": {
                "project_id": "project-2",
                "sandbox": {"id": "sb-canonical", "type": "code"},
            }
        }
    )

    async def _resolve_sandbox_id(_sandbox_id: str):
        return "sb-resolved"

    connect_calls = []

    async def _connect(sandbox_id: str, sandbox_type: str):
        connect_calls.append((sandbox_id, sandbox_type))
        return _FakeSandbox(sandbox_id)

    mapping_updates = []

    async def _store_mapping(old_id: str, new_id: str):
        mapping_updates.append((old_id, new_id))

    monkeypatch.setattr(sandbox_api, "_resolve_sandbox_id", _resolve_sandbox_id)
    monkeypatch.setattr(sandbox_api, "get_or_start_sandbox", _connect)
    monkeypatch.setattr(sandbox_api, "_store_sandbox_id_mapping", _store_mapping)

    sandbox = await sandbox_api.get_sandbox_by_id_safely(client, "sb-requested")

    assert getattr(sandbox, "sandbox_id", None) == "sb-canonical"
    assert connect_calls == [("sb-canonical", "code")]
    assert ("sb-resolved", "sb-canonical") in mapping_updates


@pytest.mark.asyncio
async def test_connected_but_not_running_triggers_resume_or_create(monkeypatch):
    client = _FakeClient(
        {
            "sb-dead": {
                "project_id": "project-3",
                "sandbox": {"id": "sb-dead", "type": "desktop", "state": "running"},
            }
        }
    )

    async def _resolve_sandbox_id(sandbox_id: str):
        return sandbox_id

    async def _connect(*_args, **_kwargs):
        return _FakeSandbox("sb-dead", running=False)

    resume_calls = []

    async def _resume_or_create(*_args, **_kwargs):
        resume_calls.append(True)
        return (_FakeSandbox("sb-resumed", running=True), "resumed")

    async def _run_blocking(_operation, call, **_kwargs):
        return call()

    monkeypatch.setattr(sandbox_api, "_resolve_sandbox_id", _resolve_sandbox_id)
    monkeypatch.setattr(sandbox_api, "get_or_start_sandbox", _connect)
    monkeypatch.setattr(sandbox_api, "resume_or_create_sandbox", _resume_or_create)
    monkeypatch.setattr(sandbox_api, "_run_blocking_sandbox_call", _run_blocking)

    sandbox = await sandbox_api.get_sandbox_by_id_safely(client, "sb-dead")

    assert getattr(sandbox, "sandbox_id", None) == "sb-resumed"
    assert resume_calls, "resume_or_create should run when connect handle is not alive"
    assert client.updated_payloads, "project sandbox metadata should be updated after recovery"
    _, payload = client.updated_payloads[-1]
    updated = json.loads(payload["sandbox"])
    assert updated["state"] == "running"
    assert updated["id"] == "sb-resumed"


@pytest.mark.asyncio
async def test_reactivation_dead_handle_forces_recreate(monkeypatch):
    client = _FakeClient(
        {
            "sb-stale": {
                "project_id": "project-5",
                "sandbox": {"id": "sb-stale", "type": "desktop", "state": "running"},
            }
        }
    )

    async def _resolve_sandbox_id(sandbox_id: str):
        return sandbox_id

    async def _connect(*_args, **_kwargs):
        return _FakeSandbox("sb-stale", running=False)

    async def _resume_or_create(*_args, **_kwargs):
        return (_FakeSandbox("sb-stale", running=False), "resumed")

    async def _create_sandbox(_password: str, project_id: str, sandbox_type: str):
        assert project_id == "project-5"
        assert sandbox_type == "desktop"
        return _FakeSandbox("sb-new", running=True)

    async def _run_blocking(_operation, call, **_kwargs):
        return call()

    mapping_updates = []

    async def _store_mapping(old_id: str, new_id: str):
        mapping_updates.append((old_id, new_id))

    monkeypatch.setattr(sandbox_api, "_resolve_sandbox_id", _resolve_sandbox_id)
    monkeypatch.setattr(sandbox_api, "get_or_start_sandbox", _connect)
    monkeypatch.setattr(sandbox_api, "resume_or_create_sandbox", _resume_or_create)
    monkeypatch.setattr(sandbox_api, "create_sandbox", _create_sandbox)
    monkeypatch.setattr(sandbox_api, "_run_blocking_sandbox_call", _run_blocking)
    monkeypatch.setattr(sandbox_api, "_store_sandbox_id_mapping", _store_mapping)

    sandbox = await sandbox_api.get_sandbox_by_id_safely(client, "sb-stale")

    assert getattr(sandbox, "sandbox_id", None) == "sb-new"
    assert ("sb-stale", "sb-new") in mapping_updates


@pytest.mark.asyncio
async def test_preclone_rotates_long_running_sandbox_before_connect(monkeypatch):
    started_at = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    client = _FakeClient(
        {
            "sb-old": {
                "project_id": "project-4",
                "sandbox": {
                    "id": "sb-old",
                    "type": "code",
                    "state": "running",
                    "created_at": started_at,
                    "lifecycle_started_at": started_at,
                },
            }
        }
    )

    async def _resolve_sandbox_id(sandbox_id: str):
        return sandbox_id

    connect_calls = []

    async def _connect(sandbox_id: str, sandbox_type: str):
        connect_calls.append((sandbox_id, sandbox_type))
        return _FakeSandbox(sandbox_id, running=True)

    async def _clone(_sandbox_id: str, count: int = 1, timeout: int = 3600):
        return ("sb-clone", "tmpl-1")

    async def _run_blocking(_operation, call, **_kwargs):
        return call()

    mapping_updates = []

    async def _store_mapping(old_id: str, new_id: str):
        mapping_updates.append((old_id, new_id))

    monkeypatch.setenv("SANDBOX_PRECLONE_ENABLED", "true")
    monkeypatch.setenv("SANDBOX_PRECLONE_THRESHOLD_SECONDS", "60")
    monkeypatch.setenv("SANDBOX_PRECLONE_GRACE_CLEANUP_SECONDS", "0")
    monkeypatch.setattr(sandbox_api, "_resolve_sandbox_id", _resolve_sandbox_id)
    monkeypatch.setattr(sandbox_api, "get_or_start_sandbox", _connect)
    monkeypatch.setattr(sandbox_api, "clone_sandbox", _clone)
    monkeypatch.setattr(sandbox_api, "_run_blocking_sandbox_call", _run_blocking)
    monkeypatch.setattr(sandbox_api, "_store_sandbox_id_mapping", _store_mapping)

    sandbox = await sandbox_api.get_sandbox_by_id_safely(client, "sb-old")

    assert getattr(sandbox, "sandbox_id", None) == "sb-clone"
    assert connect_calls == [("sb-clone", "code")]
    assert ("sb-old", "sb-clone") in mapping_updates
    assert "sb-clone" in client.rows_by_sandbox_id
    updated = client.rows_by_sandbox_id["sb-clone"]["sandbox"]
    assert updated["clone_parent_id"] == "sb-old"
    assert updated["preclone_status"] == "ok"


@pytest.mark.asyncio
async def test_get_sandbox_refreshes_project_context_inside_lock(monkeypatch):
    class _DummyClient:
        pass

    connect_calls = []

    async def _load_context(_client, _sandbox_id):
        return {
            "project_id": "project-refresh",
            "sandbox_id": "sb-old",
            "sandbox_type": "code",
            "alias_ids": ["sb-old"],
            "sandbox_info": {"id": "sb-old", "type": "code", "state": "running"},
        }

    async def _load_context_by_project(_client, project_id, *, fallback_sandbox_id=None):
        assert project_id == "project-refresh"
        assert fallback_sandbox_id == "sb-old"
        return {
            "project_id": "project-refresh",
            "sandbox_id": "sb-new",
            "sandbox_type": "code",
            "alias_ids": ["sb-old", "sb-new"],
            "sandbox_info": {"id": "sb-new", "type": "code", "state": "running"},
        }

    async def _preclone_context(_client, **kwargs):
        return {
            "sandbox_info": kwargs["sandbox_info"],
            "sandbox_id": kwargs["canonical_sandbox_id"],
            "alias_ids": kwargs["alias_ids"],
        }

    async def _connect(sandbox_id, sandbox_type):
        connect_calls.append((sandbox_id, sandbox_type))
        return _FakeSandbox(sandbox_id, running=True)

    monkeypatch.setattr(sandbox_api, "_load_project_sandbox_context", _load_context)
    monkeypatch.setattr(sandbox_api, "_load_project_sandbox_context_by_project_id", _load_context_by_project)
    monkeypatch.setattr(sandbox_api, "_maybe_preclone_sandbox_context", _preclone_context)
    monkeypatch.setattr(sandbox_api, "_connect_sandbox_with_retry", _connect)

    sandbox = await sandbox_api.get_sandbox_by_id_safely(_DummyClient(), "sb-old")

    assert sandbox.sandbox_id == "sb-new"
    assert connect_calls == [("sb-new", "code")]


@pytest.mark.asyncio
async def test_singleflight_recovery_reuses_switched_sandbox(monkeypatch):
    class _DummyClient:
        pass

    async def _load_context_by_project(_client, project_id, *, fallback_sandbox_id=None):
        assert project_id == "project-race"
        assert fallback_sandbox_id == "sb-old"
        return {
            "project_id": "project-race",
            "sandbox_id": "sb-new",
            "sandbox_type": "code",
            "alias_ids": ["sb-old", "sb-new"],
            "sandbox_info": {"id": "sb-new", "type": "code", "state": "running"},
        }

    async def _connect_if_running(sandbox_id, sandbox_type):
        assert (sandbox_id, sandbox_type) == ("sb-new", "code")
        return _FakeSandbox("sb-new", running=True)

    async def _finalize_should_not_run(*_args, **_kwargs):
        raise AssertionError("resume/create should not run when switched sandbox is already healthy")

    mapping_updates = []

    async def _store_mapping(old_id, new_id):
        mapping_updates.append((old_id, new_id))

    monkeypatch.setattr(sandbox_api, "_load_project_sandbox_context_by_project_id", _load_context_by_project)
    monkeypatch.setattr(sandbox_api, "_connect_if_running", _connect_if_running)
    monkeypatch.setattr(sandbox_api, "_resume_or_create_and_finalize", _finalize_should_not_run)
    monkeypatch.setattr(sandbox_api, "_store_sandbox_id_mapping", _store_mapping)

    sandbox = await sandbox_api._recover_stale_sandbox_with_singleflight(
        _DummyClient(),
        project_id="project-race",
        sandbox_type="code",
        sandbox_info={"id": "sb-old", "type": "code", "state": "running"},
        canonical_sandbox_id="sb-old",
        alias_ids=["sb-old"],
        reason="test",
    )

    assert sandbox.sandbox_id == "sb-new"
    assert ("sb-old", "sb-new") in mapping_updates


@pytest.mark.asyncio
async def test_attach_shadow_clone_bound_sandbox_requests_clone_recovery_when_standby_exists(monkeypatch):
    client = _FakeClient(
        {
            "sb-active": {
                "project_id": "project-shadow",
                "sandbox": {"id": "sb-active", "type": "code", "state": "running"},
            }
        }
    )
    lease = {
        "run_id": "run-shadow",
        "project_id": "project-shadow",
        "thread_id": "thread-shadow",
        "sandbox_id": "sb-active",
        "sandbox_type": "code",
        "sandbox_info": {"id": "sb-active", "type": "code", "state": "running"},
        "standby_sandbox_id": "sb-clone",
        "standby_sandbox_type": "code",
        "standby_sandbox_info": {"id": "sb-clone", "type": "code", "state": "running"},
    }
    lease_updates = []

    async def _get_lease(_run_id: str):
        return dict(lease)

    async def _connect(*_args, **_kwargs):
        raise RuntimeError("sandbox gone")

    async def _resume_or_create(*_args, **_kwargs):
        raise sandbox_api.SandboxOriginalReuseExhausted(
            "sb-active",
            "code",
            "project-shadow",
            last_error=RuntimeError("reattach failed"),
        )

    async def _update_lease(_run_id: str, **patch):
        lease_updates.append(dict(patch))
        lease.update(patch)
        return dict(lease)

    async def _set_binding_state(_run_id: str, binding_state: str, *, last_error=None):
        lease["binding_state"] = binding_state
        lease["last_error"] = last_error
        return dict(lease)

    monkeypatch.setattr(sandbox_api, "get_run_sandbox_lease", _get_lease)
    monkeypatch.setattr(sandbox_api, "_connect_sandbox_with_retry", _connect)
    monkeypatch.setattr(sandbox_api, "resume_or_create_sandbox", _resume_or_create)
    monkeypatch.setattr(sandbox_api, "update_run_sandbox_lease", _update_lease)
    monkeypatch.setattr(sandbox_api, "set_run_sandbox_binding_state", _set_binding_state)

    with pytest.raises(sandbox_api.ShadowCloneStrictSandboxError) as exc_info:
        await sandbox_api.attach_shadow_clone_bound_sandbox(client, lease=lease)

    assert exc_info.value.recoverable is True
    assert (
        exc_info.value.error_code
        == "SHADOW_CLONE_SANDBOX_CLONE_RECOVERY_REQUIRED"
    )
    assert any(
        patch.get("binding_state") == "recovery_required" for patch in lease_updates
    )


@pytest.mark.asyncio
async def test_promote_shadow_clone_standby_sandbox_switches_active_lease(monkeypatch):
    client = _FakeClient(
        {
            "sb-active": {
                "project_id": "project-shadow",
                "sandbox": {"id": "sb-active", "type": "code", "state": "running"},
            }
        }
    )
    lease = {
        "run_id": "run-shadow",
        "project_id": "project-shadow",
        "thread_id": "thread-shadow",
        "sandbox_id": "sb-active",
        "sandbox_type": "code",
        "sandbox_info": {"id": "sb-active", "type": "code", "state": "running"},
        "standby_sandbox_id": "sb-clone",
        "standby_sandbox_type": "code",
        "standby_sandbox_info": {"id": "sb-clone", "type": "code", "state": "running"},
    }
    mapping_updates = []

    async def _connect_if_running(sandbox_id: str, sandbox_type: str):
        assert (sandbox_id, sandbox_type) == ("sb-clone", "code")
        return _FakeSandbox("sb-clone", running=True)

    async def _finalize_reactivation(
        _client,
        _sandbox_obj,
        *,
        project_id: str,
        sandbox_info,
        **_kwargs,
    ):
        for key, row in list(client.rows_by_sandbox_id.items()):
            if row.get("project_id") == project_id:
                del client.rows_by_sandbox_id[key]
        client.rows_by_sandbox_id[str(sandbox_info["id"])] = {
            "project_id": project_id,
            "sandbox": dict(sandbox_info),
        }
        return str(sandbox_info["id"])

    async def _update_lease(_run_id: str, **patch):
        lease.update(patch)
        return dict(lease)

    async def _store_mapping(old_id: str, new_id: str):
        mapping_updates.append((old_id, new_id))

    monkeypatch.setattr(sandbox_api, "_connect_if_running", _connect_if_running)
    monkeypatch.setattr(sandbox_api, "_finalize_sandbox_reactivation", _finalize_reactivation)
    monkeypatch.setattr(sandbox_api, "update_run_sandbox_lease", _update_lease)
    monkeypatch.setattr(sandbox_api, "_store_sandbox_id_mapping", _store_mapping)

    promoted = await sandbox_api.promote_shadow_clone_standby_sandbox(
        client,
        lease=lease,
    )

    assert promoted["sandbox_id"] == "sb-clone"
    assert promoted["standby_sandbox_id"] is None
    assert promoted["approved_sandbox_ids"] == ["sb-clone"]
    assert ("sb-active", "sb-clone") in mapping_updates


@pytest.mark.asyncio
async def test_create_shadow_clone_standby_sandbox_validates_clone_timeout(monkeypatch):
    lease = {
        "run_id": "run-shadow",
        "project_id": "project-shadow",
        "thread_id": "thread-shadow",
        "sandbox_id": "sb-active",
        "sandbox_type": "code",
        "sandbox_info": {
            "id": "sb-active",
            "type": "code",
            "state": "running",
            "template_id": "tmpl-code-current",
            "template_type": "code",
            "template_source": "config_env",
        },
    }
    validation_calls = []

    async def _clone(_sandbox_id: str, count: int = 1, timeout: int = 3600):
        assert (_sandbox_id, count, timeout) == ("sb-active", 1, timeout)
        return ("sb-clone", "snapshot-1")

    async def _extend(sandbox_id: str, sandbox_type: str, **kwargs):
        validation_calls.append((sandbox_id, sandbox_type, kwargs))
        return object()

    monkeypatch.setattr(sandbox_api, "clone_sandbox", _clone)
    monkeypatch.setattr(sandbox_api, "extend_sandbox_timeout", _extend)

    payload = await sandbox_api.create_shadow_clone_standby_sandbox(
        object(),
        lease=lease,
        layer_index=0,
        completed_subtask_ids=["task-1"],
    )

    assert payload["standby_sandbox_id"] == "sb-clone"
    assert payload["standby_snapshot_template_id"] == "snapshot-1"
    assert payload["standby_sandbox_info"]["template_id"] == "tmpl-code-current"
    assert payload["standby_sandbox_info"]["template_type"] == "code"
    assert validation_calls
    assert validation_calls[0][0:2] == ("sb-clone", "code")
