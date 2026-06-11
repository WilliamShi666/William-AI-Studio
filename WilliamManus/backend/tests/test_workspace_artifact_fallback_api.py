import asyncio
import json
import sys
import types
import zipfile
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

# ---------------------------------------------------------------------------
# Minimal stubs to avoid heavy side effects during sandbox.api import.
# ---------------------------------------------------------------------------

if "structlog" not in sys.modules:
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

from sandbox import api as sandbox_api


# ---------------------------------------------------------------------------
# Fake DB
# ---------------------------------------------------------------------------

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
        return SimpleNamespace(data=[payload])


class _FakeClient:
    def __init__(self, rows_by_sandbox_id):
        self.rows_by_sandbox_id = rows_by_sandbox_id
        self.updated_payloads = []
        self.pool = SimpleNamespace(acquire=_fake_acquire)

    def table(self, name: str):
        assert name == "projects"
        return _FakeTableQuery(self)


class _AcquireContext:
    def __init__(self, conn):
        self._conn = conn

    async def __aenter__(self):
        return self._conn

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeConn:
    def __init__(self):
        self.calls = []

    async def fetch(self, *_args, **_kwargs):
        self.calls.append(("fetch", _args))
        return []

    async def fetchrow(self, *_args, **_kwargs):
        self.calls.append(("fetchrow", _args))
        return None


def _fake_acquire():
    return _AcquireContext(_FakeConn())


class _FakeDBConnection:
    def __init__(self, client):
        self._client = client

    @property
    async def client(self):
        return self._client


class _FakeRequest:
    def __init__(self, headers=None):
        self.headers = headers or {}
        self.state = SimpleNamespace()


class _CapturingLogger:
    def __init__(self):
        self.records = []

    def info(self, *args, **kwargs):
        self.records.append(("info", args, kwargs))

    def warning(self, *args, **kwargs):
        self.records.append(("warning", args, kwargs))

    def error(self, *args, **kwargs):
        self.records.append(("error", args, kwargs))

    def debug(self, *args, **kwargs):
        self.records.append(("debug", args, kwargs))


async def _invoke_workspace_action(
    action: str,
    sandbox_id: str,
    path: str,
    request: _FakeRequest,
    user_id: str,
    *,
    original_sandbox_id: str,
):
    if action == "list":
        return await sandbox_api._list_files_internal(
            sandbox_id,
            path,
            request,
            user_id,
            original_sandbox_id=original_sandbox_id,
        )
    if action == "read":
        return await sandbox_api._read_file_internal(
            sandbox_id,
            path,
            request,
            user_id,
            original_sandbox_id=original_sandbox_id,
        )
    raise AssertionError(f"Unexpected action: {action}")


@pytest.fixture(autouse=True)
def _sandbox_api_defaults(monkeypatch):
    # Keep tests single-threaded
    async def _run_inline(func, /, *args, **kwargs):
        return func(*args, **kwargs)

    monkeypatch.setattr("sandbox.api.asyncio.to_thread", _run_inline)

    @asynccontextmanager
    async def _local_singleflight(_project_id: str):
        yield

    monkeypatch.setattr(sandbox_api, "_project_recovery_singleflight", _local_singleflight)


@pytest.mark.asyncio
async def test_list_files_falls_back_to_artifacts_when_sandbox_list_fails(monkeypatch):
    project_id = "proj-1"
    sandbox_id = "sb-1"
    sandbox_info = {"id": sandbox_id, "type": "desktop", "state": "running"}
    client = _FakeClient({sandbox_id: {"project_id": project_id, "sandbox": json.dumps(sandbox_info)}})
    monkeypatch.setattr(sandbox_api, "db", _FakeDBConnection(client))

    async def _verify_access(_client, _sandbox_id, _user_id, **_kw):
        return {"project_id": project_id, "sandbox": json.dumps(sandbox_info)}

    async def _get_sandbox_for_request(_client, *, project_id: str, requested_sandbox_id: str, **_kw):
        class _FakeSandbox:
            class files:
                @staticmethod
                def list(_path):
                    raise RuntimeError("reattach failed")
        return _FakeSandbox(), requested_sandbox_id, None

    async def _touch(*_a, **_k):
        return None

    async def _artifact_list(*_a, **_k):
        return [{"name": "a.txt", "path": "/workspace/a.txt", "is_dir": False, "size": 3, "mod_time": "now"}]

    monkeypatch.setattr(sandbox_api, "verify_sandbox_access", _verify_access)
    monkeypatch.setattr(sandbox_api, "_get_project_sandbox_for_request", _get_sandbox_for_request)
    monkeypatch.setattr(sandbox_api, "_touch_project_sandbox_access", _touch)
    monkeypatch.setattr(sandbox_api.workspace_artifacts, "list_entries", _artifact_list)

    result = await sandbox_api._list_files_internal(
        sandbox_id,
        "/workspace",
        _FakeRequest(),
        "user-1",
        original_sandbox_id=sandbox_id,
    )
    assert result["files"]
    assert result["files"][0]["path"] == "/workspace/a.txt"


@pytest.mark.asyncio
async def test_list_files_completion_log_includes_correlation_and_response_source(monkeypatch):
    project_id = "proj-1"
    sandbox_id = "sb-1"
    sandbox_info = {"id": sandbox_id, "type": "desktop", "state": "running"}
    client = _FakeClient({sandbox_id: {"project_id": project_id, "sandbox": json.dumps(sandbox_info)}})
    monkeypatch.setattr(sandbox_api, "db", _FakeDBConnection(client))

    async def _verify_access(_client, _sandbox_id, _user_id, **_kw):
        return {"project_id": project_id, "sandbox": json.dumps(sandbox_info)}

    async def _get_sandbox_for_request(_client, *, project_id: str, requested_sandbox_id: str, **_kw):
        class _FakeSandbox:
            class files:
                @staticmethod
                def list(_path):
                    raise RuntimeError("reattach failed")
        return _FakeSandbox(), requested_sandbox_id, None

    async def _touch(*_a, **_k):
        return None

    async def _artifact_list(*_a, **_k):
        return [{"name": "a.txt", "path": "/workspace/a.txt", "is_dir": False, "size": 3, "mod_time": "now"}]

    logger = _CapturingLogger()

    monkeypatch.setattr(sandbox_api, "verify_sandbox_access", _verify_access)
    monkeypatch.setattr(sandbox_api, "_get_project_sandbox_for_request", _get_sandbox_for_request)
    monkeypatch.setattr(sandbox_api, "_touch_project_sandbox_access", _touch)
    monkeypatch.setattr(sandbox_api.workspace_artifacts, "list_entries", _artifact_list)
    monkeypatch.setattr(sandbox_api, "logger", logger)

    request = _FakeRequest(
        headers={
            "x-request-id": "req-list-1",
            "x-client-operation-id": "file-list-test-1",
        }
    )
    result = await sandbox_api._list_files_internal(
        sandbox_id,
        "/workspace",
        request,
        "user-1",
        original_sandbox_id=sandbox_id,
    )

    assert result["files"][0]["path"] == "/workspace/a.txt"
    completion = [
        kwargs.get("extra", {})
        for level, args, kwargs in logger.records
        if level == "info" and args and args[0] == "List files completed"
    ]
    assert completion
    assert completion[-1]["client_operation_id"] == "file-list-test-1"
    assert completion[-1]["request_id"] == "req-list-1"
    assert completion[-1]["response_source"] == "artifact"
    assert completion[-1]["status_code"] == 200
    assert completion[-1]["outcome"] == "fallback_success"




@pytest.mark.asyncio
async def test_claude_local_list_filters_artifacts_to_requested_run(monkeypatch):
    expected_project_id = "proj-run-scope"
    run_id = "run-a"
    requested_sandbox_id = f"claude-local:{run_id}"
    project_sandbox_id = "sb-project"
    sandbox_info = {"id": project_sandbox_id, "type": "desktop", "state": "running"}
    client = _FakeClient(
        {
            project_sandbox_id: {
                "project_id": expected_project_id,
                "account_id": "user-1",
                "sandbox": json.dumps(sandbox_info),
            }
        }
    )
    monkeypatch.setattr(sandbox_api, "db", _FakeDBConnection(client))

    async def _verify_access(_client, _sandbox_id, _user_id, **_kw):
        raise HTTPException(status_code=404, detail="Synthetic sandbox not found")

    async def _artifact_hint(*, sandbox_ids, client=None):
        assert requested_sandbox_id in sandbox_ids
        return {"project_id": expected_project_id, "sandbox_id": requested_sandbox_id}

    async def _get_sandbox_for_request(*_args, **_kwargs):
        raise AssertionError("claude-local artifact requests must not attach live sandbox")

    calls = []

    async def _artifact_list(**kwargs):
        calls.append(kwargs)
        assert kwargs["project_id"] == expected_project_id
        assert kwargs["agent_run_id"] == run_id
        return [
            {
                "name": "run-a.txt",
                "path": "/workspace/run-a.txt",
                "is_dir": False,
                "size": 5,
                "mod_time": "now",
                "delivery_source": "artifact",
                "downloadable": True,
                "artifact_state": "available",
            }
        ]

    monkeypatch.setattr(sandbox_api, "verify_sandbox_access", _verify_access)
    monkeypatch.setattr(sandbox_api.workspace_artifacts, "resolve_project_for_sandbox_hints", _artifact_hint)
    monkeypatch.setattr(sandbox_api, "_get_project_sandbox_for_request", _get_sandbox_for_request)
    monkeypatch.setattr(sandbox_api.workspace_artifacts, "list_entries", _artifact_list)

    result = await sandbox_api._list_files_internal(
        requested_sandbox_id,
        "/workspace",
        _FakeRequest(),
        "user-1",
        original_sandbox_id=requested_sandbox_id,
    )

    assert calls
    assert result["files"][0]["path"] == "/workspace/run-a.txt"
    assert result["diagnostics"]["artifact_scope"] == "agent_run"
    assert result["diagnostics"]["agent_run_id"] == run_id


@pytest.mark.asyncio
async def test_claude_local_read_filters_artifacts_to_requested_run(monkeypatch):
    expected_project_id = "proj-run-read"
    run_id = "run-read"
    requested_sandbox_id = f"claude-local:{run_id}"
    project_sandbox_id = "sb-project-read"
    sandbox_info = {"id": project_sandbox_id, "type": "desktop", "state": "running"}
    client = _FakeClient(
        {
            project_sandbox_id: {
                "project_id": expected_project_id,
                "account_id": "user-1",
                "sandbox": json.dumps(sandbox_info),
            }
        }
    )
    monkeypatch.setattr(sandbox_api, "db", _FakeDBConnection(client))

    async def _verify_access(_client, _sandbox_id, _user_id, **_kw):
        raise HTTPException(status_code=404, detail="Synthetic sandbox not found")

    async def _artifact_hint(*, sandbox_ids, client=None):
        assert requested_sandbox_id in sandbox_ids
        return {"project_id": expected_project_id, "sandbox_id": requested_sandbox_id}

    async def _get_sandbox_for_request(*_args, **_kwargs):
        raise AssertionError("claude-local artifact reads must not attach live sandbox")

    calls = []

    async def _artifact_read(**kwargs):
        calls.append(kwargs)
        assert kwargs["project_id"] == expected_project_id
        assert kwargs["agent_run_id"] == run_id
        return SimpleNamespace(content_type="text/plain"), b"run-body"

    monkeypatch.setattr(sandbox_api, "verify_sandbox_access", _verify_access)
    monkeypatch.setattr(sandbox_api.workspace_artifacts, "resolve_project_for_sandbox_hints", _artifact_hint)
    monkeypatch.setattr(sandbox_api, "_get_project_sandbox_for_request", _get_sandbox_for_request)
    monkeypatch.setattr(sandbox_api.workspace_artifacts, "read_artifact_bytes", _artifact_read)

    response = await sandbox_api._read_file_internal(
        requested_sandbox_id,
        "/workspace/run-read.txt",
        _FakeRequest(),
        "user-1",
        original_sandbox_id=requested_sandbox_id,
    )

    assert calls
    payload = getattr(response, "body", None)
    if payload is None:
        payload = getattr(response, "content", None)
    assert payload == b"run-body"
    assert response.headers["x-workspace-response-source"] == "artifact"

@pytest.mark.asyncio
async def test_list_files_can_resolve_project_context_from_artifact_hint_when_access_lookup_fails(monkeypatch):
    expected_project_id = "proj-1"
    requested_sandbox_id = "sb-stale"
    project_sandbox_id = "sb-current"
    sandbox_info = {"id": project_sandbox_id, "type": "desktop", "state": "running"}
    client = _FakeClient(
        {
            project_sandbox_id: {
                "project_id": expected_project_id,
                "account_id": "user-1",
                "sandbox": json.dumps(sandbox_info),
            }
        }
    )
    monkeypatch.setattr(sandbox_api, "db", _FakeDBConnection(client))

    async def _verify_access(_client, _sandbox_id, _user_id, **_kw):
        raise HTTPException(status_code=404, detail="Sandbox not found")

    async def _artifact_hint(*, sandbox_ids, client=None):
        assert sandbox_ids == [requested_sandbox_id]
        return {"project_id": expected_project_id, "sandbox_id": requested_sandbox_id}

    async def _get_sandbox_for_request(_client, *, project_id: str, requested_sandbox_id: str, **_kw):
        assert project_id == expected_project_id
        assert requested_sandbox_id == project_sandbox_id

        class _FakeSandbox:
            class files:
                @staticmethod
                def list(_path):
                    raise RuntimeError("reattach failed")

        return _FakeSandbox(), requested_sandbox_id, None

    async def _touch(*_a, **_k):
        return None

    async def _artifact_list(*, project_id: str, **_kw):
        assert project_id == "proj-1"
        return [
            {
                "name": "a.txt",
                "path": "/workspace/a.txt",
                "is_dir": False,
                "size": 3,
                "mod_time": "now",
                "delivery_source": "artifact",
                "downloadable": True,
                "artifact_state": "available",
            }
        ]

    monkeypatch.setattr(sandbox_api, "verify_sandbox_access", _verify_access)
    monkeypatch.setattr(
        sandbox_api.workspace_artifacts,
        "resolve_project_for_sandbox_hints",
        _artifact_hint,
    )
    monkeypatch.setattr(sandbox_api, "_get_project_sandbox_for_request", _get_sandbox_for_request)
    monkeypatch.setattr(sandbox_api, "_touch_project_sandbox_access", _touch)
    monkeypatch.setattr(sandbox_api.workspace_artifacts, "list_entries", _artifact_list)

    result = await sandbox_api._list_files_internal(
        requested_sandbox_id,
        "/workspace",
        _FakeRequest(),
        "user-1",
        original_sandbox_id=requested_sandbox_id,
    )

    assert result["files"][0]["path"] == "/workspace/a.txt"
    assert result["diagnostics"]["identity_source"] == "artifact_hint"
    assert result["diagnostics"]["sandbox_available"] is False
    assert result["diagnostics"]["response_source"] == "artifact"


@pytest.mark.asyncio
async def test_list_files_prefers_artifacts_without_live_attach_for_released_shadow_clone_lease(monkeypatch):
    project_id = "proj-released-list"
    sandbox_id = "sb-released-list"
    sandbox_info = {"id": sandbox_id, "type": "desktop", "state": "running"}
    client = _FakeClient(
        {
            sandbox_id: {
                "project_id": project_id,
                "account_id": "user-1",
                "sandbox": json.dumps(sandbox_info),
            }
        }
    )
    monkeypatch.setattr(sandbox_api, "db", _FakeDBConnection(client))

    async def _verify_access(_client, _sandbox_id, _user_id, **_kw):
        return client.rows_by_sandbox_id[sandbox_id]

    async def _preferred_lease(*_args, **_kwargs):
        return {
            "run_id": "run-released-list",
            "project_id": project_id,
            "sandbox_id": sandbox_id,
            "binding_state": "released",
        }

    attach_calls = []

    async def _get_sandbox_for_request(*_args, **_kwargs):
        attach_calls.append("called")
        raise AssertionError("released lease should not attempt live attach")

    async def _artifact_list(*_a, **_k):
        return [
            {
                "name": "artifact.txt",
                "path": "/workspace/artifact.txt",
                "is_dir": False,
                "size": 8,
                "mod_time": "now",
            }
        ]

    monkeypatch.setattr(sandbox_api, "verify_sandbox_access", _verify_access)
    monkeypatch.setattr(sandbox_api, "get_preferred_project_sandbox_lease", _preferred_lease)
    monkeypatch.setattr(sandbox_api, "_get_project_sandbox_for_request", _get_sandbox_for_request)
    monkeypatch.setattr(sandbox_api.workspace_artifacts, "list_entries", _artifact_list)

    result = await sandbox_api._list_files_internal(
        sandbox_id,
        "/workspace",
        _FakeRequest(),
        "user-1",
        original_sandbox_id=sandbox_id,
    )

    assert attach_calls == []
    assert result["files"] == [
        {
            "name": "artifact.txt",
            "path": "/workspace/artifact.txt",
            "is_dir": False,
            "size": 8,
            "mod_time": "now",
            "delivery_source": "artifact",
            "downloadable": True,
            "artifact_state": "available",
        }
    ]
    assert result["diagnostics"]["response_source"] == "artifact"
    assert result["diagnostics"]["sandbox_available"] is False


@pytest.mark.asyncio
async def test_list_files_released_shadow_clone_without_artifacts_returns_404(monkeypatch):
    project_id = "proj-released-empty"
    sandbox_id = "sb-released-empty"
    sandbox_info = {"id": sandbox_id, "type": "desktop", "state": "running"}
    client = _FakeClient(
        {
            sandbox_id: {
                "project_id": project_id,
                "account_id": "user-1",
                "sandbox": json.dumps(sandbox_info),
            }
        }
    )
    monkeypatch.setattr(sandbox_api, "db", _FakeDBConnection(client))

    async def _verify_access(_client, _sandbox_id, _user_id, **_kw):
        return client.rows_by_sandbox_id[sandbox_id]

    async def _preferred_lease(*_args, **_kwargs):
        return {
            "run_id": "run-released-empty",
            "project_id": project_id,
            "sandbox_id": sandbox_id,
            "binding_state": "released",
        }

    async def _unexpected_attach(*_args, **_kwargs):
        raise AssertionError("released lease should not attempt live attach")

    async def _artifact_list(*_a, **_k):
        return []

    monkeypatch.setattr(sandbox_api, "verify_sandbox_access", _verify_access)
    monkeypatch.setattr(sandbox_api, "get_preferred_project_sandbox_lease", _preferred_lease)
    monkeypatch.setattr(sandbox_api, "_get_project_sandbox_for_request", _unexpected_attach)
    monkeypatch.setattr(sandbox_api.workspace_artifacts, "list_entries", _artifact_list)

    with pytest.raises(HTTPException) as exc_info:
        await sandbox_api._list_files_internal(
            sandbox_id,
            "/workspace",
            _FakeRequest(),
            "user-1",
            original_sandbox_id=sandbox_id,
        )

    assert exc_info.value.status_code == 404
    detail = exc_info.value.detail
    assert detail["error_code"] == "WORKSPACE_ARTIFACT_FALLBACK_NOT_FOUND"
    assert detail["path"] == "/workspace"
    assert detail["binding_state"] == "released"


@pytest.mark.asyncio
async def test_list_files_strict_shadow_clone_without_artifacts_returns_404(monkeypatch):
    project_id = "proj-strict-empty"
    sandbox_id = "sb-strict-empty"
    sandbox_info = {"id": sandbox_id, "type": "desktop", "state": "running"}
    client = _FakeClient(
        {
            sandbox_id: {
                "project_id": project_id,
                "account_id": "user-1",
                "sandbox": json.dumps(sandbox_info),
            }
        }
    )
    monkeypatch.setattr(sandbox_api, "db", _FakeDBConnection(client))

    async def _verify_access(_client, _sandbox_id, _user_id, **_kw):
        return client.rows_by_sandbox_id[sandbox_id]

    async def _touch(*_args, **_kwargs):
        return None

    async def _strict_attach(*_args, **_kwargs):
        raise sandbox_api.ShadowCloneStrictSandboxError(
            "lease released",
            error_code="SHADOW_CLONE_SANDBOX_REQUIRED",
        )

    async def _artifact_list(*_a, **_k):
        return []

    monkeypatch.setattr(sandbox_api, "verify_sandbox_access", _verify_access)
    monkeypatch.setattr(sandbox_api, "_touch_project_sandbox_access", _touch)
    monkeypatch.setattr(sandbox_api, "_get_project_sandbox_for_request", _strict_attach)
    monkeypatch.setattr(sandbox_api.workspace_artifacts, "list_entries", _artifact_list)

    with pytest.raises(HTTPException) as exc_info:
        await sandbox_api._list_files_internal(
            sandbox_id,
            "/workspace",
            _FakeRequest(),
            "user-1",
            original_sandbox_id=sandbox_id,
        )

    assert exc_info.value.status_code == 404
    detail = exc_info.value.detail
    assert detail["error_code"] == "WORKSPACE_ARTIFACT_FALLBACK_NOT_FOUND"
    assert detail["path"] == "/workspace"
    assert detail["sandbox_available"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("action", "status_code", "detail"),
    [
        ("list", 401, "Authentication required for this resource"),
        ("read", 403, "Not authorized to access this sandbox"),
    ],
)
async def test_workspace_requests_preserve_auth_http_errors_without_artifact_fallback(
    monkeypatch,
    action,
    status_code,
    detail,
):
    monkeypatch.setattr(sandbox_api, "db", _FakeDBConnection(_FakeClient({})))

    async def _verify_access(_client, _sandbox_id, _user_id, **_kw):
        raise HTTPException(status_code=status_code, detail=detail)

    async def _unexpected_artifact_list(*_a, **_k):
        raise AssertionError("artifact list fallback should not run")

    async def _unexpected_artifact_read(*_a, **_k):
        raise AssertionError("artifact read fallback should not run")

    monkeypatch.setattr(sandbox_api, "verify_sandbox_access", _verify_access)
    monkeypatch.setattr(sandbox_api.workspace_artifacts, "list_entries", _unexpected_artifact_list)
    monkeypatch.setattr(sandbox_api.workspace_artifacts, "read_artifact_bytes", _unexpected_artifact_read)

    with pytest.raises(HTTPException) as exc_info:
        await _invoke_workspace_action(
            action,
            "sb-auth",
            "/workspace/protected.txt",
            _FakeRequest(),
            "user-2",
            original_sandbox_id="sb-auth",
        )

    assert exc_info.value.status_code == status_code
    assert exc_info.value.detail == detail


@pytest.mark.asyncio
async def test_read_directory_preserves_directory_error_without_artifact_fallback(monkeypatch):
    project_id = "proj-1"
    sandbox_id = "sb-dir"
    sandbox_info = {"id": sandbox_id, "type": "desktop", "state": "running"}
    client = _FakeClient({sandbox_id: {"project_id": project_id, "sandbox": json.dumps(sandbox_info)}})
    monkeypatch.setattr(sandbox_api, "db", _FakeDBConnection(client))

    async def _verify_access(_client, _sandbox_id, _user_id, **_kw):
        return {"project_id": project_id, "sandbox": json.dumps(sandbox_info)}

    async def _get_sandbox_for_request(_client, *, project_id: str, requested_sandbox_id: str, **_kw):
        return SimpleNamespace(files=SimpleNamespace()), requested_sandbox_id, None

    async def _touch(*_a, **_k):
        return None

    async def _directory_error(*_a, **_k):
        raise IsADirectoryError("/workspace/reports is a directory")

    async def _unexpected_artifact_read(*_a, **_k):
        raise AssertionError("artifact read fallback should not run for directories")

    monkeypatch.setattr(sandbox_api, "verify_sandbox_access", _verify_access)
    monkeypatch.setattr(sandbox_api, "_get_project_sandbox_for_request", _get_sandbox_for_request)
    monkeypatch.setattr(sandbox_api, "_touch_project_sandbox_access", _touch)
    monkeypatch.setattr(sandbox_api, "_run_guarded_sandbox_io", _directory_error)
    monkeypatch.setattr(sandbox_api.workspace_artifacts, "read_artifact_bytes", _unexpected_artifact_read)

    with pytest.raises(HTTPException) as exc_info:
        await sandbox_api._read_file_internal(
            sandbox_id,
            "/workspace/reports",
            _FakeRequest(),
            "user-1",
            original_sandbox_id=sandbox_id,
        )

    assert exc_info.value.status_code == 409
    detail = exc_info.value.detail
    assert detail["error_code"] == "WORKSPACE_PATH_IS_DIRECTORY"
    assert detail["path"] == "/workspace/reports"
    assert detail["path_kind"] == "directory"


@pytest.mark.asyncio
async def test_read_file_prefers_artifacts_without_live_attach_for_released_shadow_clone_lease(monkeypatch):
    project_id = "proj-released-read"
    sandbox_id = "sb-released-read"
    sandbox_info = {"id": sandbox_id, "type": "desktop", "state": "running"}
    client = _FakeClient(
        {
            sandbox_id: {
                "project_id": project_id,
                "account_id": "user-1",
                "sandbox": json.dumps(sandbox_info),
            }
        }
    )
    monkeypatch.setattr(sandbox_api, "db", _FakeDBConnection(client))

    async def _verify_access(_client, _sandbox_id, _user_id, **_kw):
        return client.rows_by_sandbox_id[sandbox_id]

    async def _preferred_lease(*_args, **_kwargs):
        return {
            "run_id": "run-released-read",
            "project_id": project_id,
            "sandbox_id": sandbox_id,
            "binding_state": "released",
        }

    attach_calls = []

    async def _get_sandbox_for_request(*_args, **_kwargs):
        attach_calls.append("called")
        raise AssertionError("released lease should not attempt live attach")

    async def _artifact_read(*_a, **_k):
        return SimpleNamespace(content_type="text/plain"), b"artifact-body"

    monkeypatch.setattr(sandbox_api, "verify_sandbox_access", _verify_access)
    monkeypatch.setattr(sandbox_api, "get_preferred_project_sandbox_lease", _preferred_lease)
    monkeypatch.setattr(sandbox_api, "_get_project_sandbox_for_request", _get_sandbox_for_request)
    monkeypatch.setattr(sandbox_api.workspace_artifacts, "read_artifact_bytes", _artifact_read)

    response = await sandbox_api._read_file_internal(
        sandbox_id,
        "/workspace/report.txt",
        _FakeRequest(),
        "user-1",
        original_sandbox_id=sandbox_id,
    )

    assert attach_calls == []
    payload = getattr(response, "body", None)
    if payload is None:
        payload = getattr(response, "content", None)
    assert payload == b"artifact-body"
    assert response.headers["x-workspace-response-source"] == "artifact"


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["list", "read"])
async def test_workspace_requests_preserve_unmappable_context_404_without_artifact_fallback(
    monkeypatch,
    action,
):
    requested_sandbox_id = "sb-missing"
    monkeypatch.setattr(sandbox_api, "db", _FakeDBConnection(_FakeClient({})))

    async def _verify_access(_client, _sandbox_id, _user_id, **_kw):
        raise HTTPException(status_code=404, detail="Sandbox not found")

    async def _artifact_hint(*, sandbox_ids, client=None):
        assert sandbox_ids == [requested_sandbox_id]
        return None

    async def _unexpected_artifact_list(*_a, **_k):
        raise AssertionError("artifact list fallback should not run")

    async def _unexpected_artifact_read(*_a, **_k):
        raise AssertionError("artifact read fallback should not run")

    monkeypatch.setattr(sandbox_api, "verify_sandbox_access", _verify_access)
    monkeypatch.setattr(
        sandbox_api.workspace_artifacts,
        "resolve_project_for_sandbox_hints",
        _artifact_hint,
    )
    monkeypatch.setattr(sandbox_api.workspace_artifacts, "list_entries", _unexpected_artifact_list)
    monkeypatch.setattr(sandbox_api.workspace_artifacts, "read_artifact_bytes", _unexpected_artifact_read)
    monkeypatch.setattr(sandbox_api.workspace_artifacts, "availability_state", lambda: "enabled")

    with pytest.raises(HTTPException) as exc_info:
        await _invoke_workspace_action(
            action,
            requested_sandbox_id,
            "/workspace/missing.txt",
            _FakeRequest(),
            "user-1",
            original_sandbox_id=requested_sandbox_id,
        )

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail["error_code"] == "FILE_PROJECT_CONTEXT_NOT_FOUND"
    assert exc_info.value.detail["requested_sandbox_id"] == requested_sandbox_id
    assert exc_info.value.detail["resolved_sandbox_id"] == requested_sandbox_id
    assert exc_info.value.detail["original_sandbox_id"] == requested_sandbox_id


@pytest.mark.asyncio
async def test_list_files_includes_diagnostics_and_missing_blob_provenance(monkeypatch):
    project_id = "proj-1"
    sandbox_id = "sb-1"
    sandbox_info = {"id": sandbox_id, "type": "desktop", "state": "running"}
    client = _FakeClient({sandbox_id: {"project_id": project_id, "sandbox": json.dumps(sandbox_info)}})
    monkeypatch.setattr(sandbox_api, "db", _FakeDBConnection(client))

    async def _verify_access(_client, _sandbox_id, _user_id, **_kw):
        return {"project_id": project_id, "sandbox": json.dumps(sandbox_info)}

    async def _get_sandbox_for_request(_client, *, project_id: str, requested_sandbox_id: str, **_kw):
        class _FakeSandbox:
            class files:
                @staticmethod
                def list(_path):
                    raise RuntimeError("reattach failed")

        return _FakeSandbox(), requested_sandbox_id, None

    async def _touch(*_a, **_k):
        return None

    async def _artifact_list(*_a, **_k):
        return [
            {
                "name": "ghost.txt",
                "path": "/workspace/ghost.txt",
                "is_dir": False,
                "size": 11,
                "mod_time": "now",
                "delivery_source": "artifact",
                "downloadable": False,
                "artifact_state": "missing_blob",
            }
        ]

    monkeypatch.setattr(sandbox_api, "verify_sandbox_access", _verify_access)
    monkeypatch.setattr(sandbox_api, "_get_project_sandbox_for_request", _get_sandbox_for_request)
    monkeypatch.setattr(sandbox_api, "_touch_project_sandbox_access", _touch)
    monkeypatch.setattr(sandbox_api.workspace_artifacts, "list_entries", _artifact_list)

    result = await sandbox_api._list_files_internal(
        sandbox_id,
        "/workspace",
        _FakeRequest(),
        "user-1",
        original_sandbox_id=sandbox_id,
    )

    assert result["diagnostics"]["identity_source"] == "sandbox_access"
    assert result["diagnostics"]["sandbox_available"] is False
    assert result["diagnostics"]["response_source"] == "artifact"
    assert result["files"][0]["delivery_source"] == "artifact"
    assert result["files"][0]["downloadable"] is False
    assert result["files"][0]["artifact_state"] == "missing_blob"


@pytest.mark.asyncio
async def test_workspace_artifact_list_entries_marks_missing_blob_as_undownloadable(monkeypatch):
    class _RowsConn:
        async def fetch(self, *_args, **_kwargs):
            return [
                {
                    "path": "/workspace/ghost.txt",
                    "size_bytes": 11,
                    "updated_at": "2026-04-02T00:00:00Z",
                    "storage_backend": "local",
                    "storage_key": "ghost-key",
                }
            ]

    class _RowsAcquire:
        async def __aenter__(self):
            return _RowsConn()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class _RowsClient:
        pool = SimpleNamespace(acquire=lambda self=None: _RowsAcquire())

    class _MissingBlobBackend:
        async def exists_bytes(self, storage_key: str) -> bool:
            assert storage_key == "ghost-key"
            return False

    monkeypatch.setattr(
        sandbox_api.workspace_artifacts,
        "_get_blob_backend",
        lambda _backend_name=None: _MissingBlobBackend(),
    )

    entries = await sandbox_api.workspace_artifacts.list_entries(
        project_id="proj-1",
        path="/workspace",
        client=_RowsClient(),
    )

    assert entries[0]["path"] == "/workspace/ghost.txt"
    assert entries[0]["delivery_source"] == "artifact"
    assert entries[0]["downloadable"] is False
    assert entries[0]["artifact_state"] == "missing_blob"


@pytest.mark.asyncio
async def test_has_artifacts_for_run_requires_agent_run_workspace_scope():
    calls = []

    class _RowsConn:
        async def fetchrow(self, query, *params):
            calls.append((query, params))
            assert "agent_run_id = $1" in query
            assert "(metadata ->> 'workspace_scope') = 'agent_run'" in query
            assert params == ("run-1",)
            return {"artifact_id": "artifact-1"}

    class _RowsAcquire:
        async def __aenter__(self):
            return _RowsConn()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class _RowsClient:
        pool = SimpleNamespace(acquire=lambda self=None: _RowsAcquire())

    assert await sandbox_api.workspace_artifacts.has_artifacts_for_run(
        "run-1",
        client=_RowsClient(),
    )
    assert calls


@pytest.mark.asyncio
async def test_resolve_project_for_claude_local_hint_requires_matching_run_scope():
    calls = []

    class _RowsConn:
        async def fetchrow(self, query, *params):
            calls.append((query, params))
            assert "sandbox_id = $1" in query
            assert "agent_run_id = $2" in query
            assert "(metadata ->> 'workspace_scope') = 'agent_run'" in query
            assert params == ("claude-local:run-1", "run-1")
            return {
                "project_id": "proj-1",
                "sandbox_id": "claude-local:run-1",
                "updated_at": "2026-04-02T00:00:00Z",
                "created_at": "2026-04-02T00:00:00Z",
            }

    class _RowsAcquire:
        async def __aenter__(self):
            return _RowsConn()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class _RowsClient:
        pool = SimpleNamespace(acquire=lambda self=None: _RowsAcquire())

    result = await sandbox_api.workspace_artifacts.resolve_project_for_sandbox_hints(
        sandbox_ids=["claude-local:run-1"],
        client=_RowsClient(),
    )

    assert result["project_id"] == "proj-1"
    assert result["sandbox_id"] == "claude-local:run-1"
    assert calls


@pytest.mark.asyncio
async def test_resolve_project_for_claude_local_hint_falls_back_to_agent_run_scope():
    calls = []

    class _RowsConn:
        async def fetchrow(self, query, *params):
            calls.append((query, params))
            if len(calls) == 1:
                assert "sandbox_id = $1" in query
                assert params == ("claude-local:run-2", "run-2")
                return None
            assert "WHERE agent_run_id = $1" in query
            assert "(metadata ->> 'workspace_scope') = 'agent_run'" in query
            assert params == ("run-2",)
            return {
                "project_id": "proj-2",
                "sandbox_id": "real-sandbox-2",
                "updated_at": "2026-04-02T00:00:00Z",
                "created_at": "2026-04-02T00:00:00Z",
            }

    class _RowsAcquire:
        async def __aenter__(self):
            return _RowsConn()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class _RowsClient:
        pool = SimpleNamespace(acquire=lambda self=None: _RowsAcquire())

    result = await sandbox_api.workspace_artifacts.resolve_project_for_sandbox_hints(
        sandbox_ids=["claude-local:run-2"],
        client=_RowsClient(),
    )

    assert result["project_id"] == "proj-2"
    assert result["sandbox_id"] == "real-sandbox-2"
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_read_file_falls_back_to_artifacts_when_sandbox_read_fails(monkeypatch):
    project_id = "proj-1"
    sandbox_id = "sb-1"
    sandbox_info = {"id": sandbox_id, "type": "desktop", "state": "running"}
    client = _FakeClient({sandbox_id: {"project_id": project_id, "sandbox": json.dumps(sandbox_info)}})
    monkeypatch.setattr(sandbox_api, "db", _FakeDBConnection(client))

    async def _verify_access(_client, _sandbox_id, _user_id, **_kw):
        return {"project_id": project_id, "sandbox": json.dumps(sandbox_info)}

    async def _get_sandbox_for_request(_client, *, project_id: str, requested_sandbox_id: str, **_kw):
        class _FakeSandbox:
            class files:
                @staticmethod
                def read(_path, **_kw):
                    raise RuntimeError("reattach failed")
        return _FakeSandbox(), requested_sandbox_id, None

    async def _touch(*_a, **_k):
        return None

    async def _artifact_read(*_a, **_k):
        return (
            SimpleNamespace(content_type="text/plain"),
            b"hello",
        )

    logger = _CapturingLogger()

    monkeypatch.setattr(sandbox_api, "verify_sandbox_access", _verify_access)
    monkeypatch.setattr(sandbox_api, "_get_project_sandbox_for_request", _get_sandbox_for_request)
    monkeypatch.setattr(sandbox_api, "_touch_project_sandbox_access", _touch)
    monkeypatch.setattr(sandbox_api.workspace_artifacts, "read_artifact_bytes", _artifact_read)
    monkeypatch.setattr(sandbox_api, "logger", logger)

    response = await sandbox_api._read_file_internal(
        sandbox_id,
        "/workspace/a.txt",
        _FakeRequest(
            headers={
                "x-request-id": "req-artifact-read",
                "x-client-operation-id": "file-download-test-1",
            }
        ),
        "user-1",
        original_sandbox_id=sandbox_id,
    )
    payload = getattr(response, "body", None)
    if payload is None:
        payload = getattr(response, "content", None)
    assert payload == b"hello"
    assert response.headers["x-request-id"] == "req-artifact-read"
    assert response.headers["x-client-operation-id"] == "file-download-test-1"
    assert response.headers["x-workspace-response-source"] == "artifact"
    assert response.headers["x-workspace-fallback"] == "1"
    assert response.media_type == "text/plain"
    assert "filename=\"a.txt\"" in response.headers["content-disposition"]
    completion = [
        kwargs.get("extra", {})
        for level, args, kwargs in logger.records
        if level == "info" and args and args[0] == "File download completed"
    ]
    assert completion
    assert completion[-1]["client_operation_id"] == "file-download-test-1"
    assert completion[-1]["request_id"] == "req-artifact-read"
    assert completion[-1]["response_source"] == "artifact"
    assert completion[-1]["status_code"] == 200


@pytest.mark.asyncio
async def test_read_file_live_response_uses_rfc5987_content_disposition(monkeypatch):
    project_id = "proj-1"
    sandbox_id = "sb-1"
    sandbox_info = {"id": sandbox_id, "type": "desktop", "state": "running"}
    client = _FakeClient({sandbox_id: {"project_id": project_id, "sandbox": json.dumps(sandbox_info)}})
    monkeypatch.setattr(sandbox_api, "db", _FakeDBConnection(client))

    async def _verify_access(_client, _sandbox_id, _user_id, **_kw):
        return {"project_id": project_id, "sandbox": json.dumps(sandbox_info)}

    async def _get_sandbox_for_request(_client, *, project_id: str, requested_sandbox_id: str, **_kw):
        class _FakeSandbox:
            class files:
                @staticmethod
                def read(_path, **_kw):
                    return b"hello"
        return _FakeSandbox(), requested_sandbox_id, None

    async def _touch(*_a, **_k):
        return None

    async def _run_inline(*args, **kwargs):
        call = args[2]
        return call()

    monkeypatch.setattr(sandbox_api, "verify_sandbox_access", _verify_access)
    monkeypatch.setattr(sandbox_api, "_get_project_sandbox_for_request", _get_sandbox_for_request)
    monkeypatch.setattr(sandbox_api, "_touch_project_sandbox_access", _touch)
    monkeypatch.setattr(sandbox_api, "_run_guarded_sandbox_io", _run_inline)

    response = await sandbox_api._read_file_internal(
        sandbox_id,
        "/workspace/résumé.txt",
        _FakeRequest(headers={"x-request-id": "req-live-read"}),
        "user-1",
        original_sandbox_id=sandbox_id,
    )

    payload = getattr(response, "body", None)
    if payload is None:
        payload = getattr(response, "content", None)
    assert payload == b"hello"
    assert response.headers["x-request-id"] == "req-live-read"
    assert response.headers["x-workspace-response-source"] == "sandbox"
    assert response.headers["x-workspace-fallback"] == "0"
    assert response.media_type == "text/plain"
    assert "filename=\"rsum.txt\"" in response.headers["content-disposition"]
    assert "filename*=UTF-8''r%C3%A9sum%C3%A9.txt" in response.headers["content-disposition"]


@pytest.mark.asyncio
async def test_read_directory_returns_structured_error_without_artifact_file_fallback(monkeypatch):
    project_id = "proj-1"
    sandbox_id = "sb-1"
    directory_path = "/workspace/data"
    sandbox_info = {"id": sandbox_id, "type": "desktop", "state": "running"}
    client = _FakeClient({sandbox_id: {"project_id": project_id, "sandbox": json.dumps(sandbox_info)}})
    monkeypatch.setattr(sandbox_api, "db", _FakeDBConnection(client))

    async def _verify_access(_client, _sandbox_id, _user_id, **_kw):
        return {"project_id": project_id, "sandbox": json.dumps(sandbox_info)}

    async def _get_sandbox_for_request(_client, *, project_id: str, requested_sandbox_id: str, **_kw):
        class _FakeSandbox:
            class files:
                @staticmethod
                def read(_path, **_kw):
                    raise IsADirectoryError(directory_path)

        return _FakeSandbox(), requested_sandbox_id, None

    async def _touch(*_a, **_k):
        return None

    async def _run_inline(*args, **kwargs):
        call = args[2]
        return call()

    async def _unexpected_artifact_read(*_a, **_k):
        raise AssertionError("artifact read fallback should not run")

    monkeypatch.setattr(sandbox_api, "verify_sandbox_access", _verify_access)
    monkeypatch.setattr(sandbox_api, "_get_project_sandbox_for_request", _get_sandbox_for_request)
    monkeypatch.setattr(sandbox_api, "_touch_project_sandbox_access", _touch)
    monkeypatch.setattr(sandbox_api, "_run_guarded_sandbox_io", _run_inline)
    monkeypatch.setattr(sandbox_api.workspace_artifacts, "read_artifact_bytes", _unexpected_artifact_read)

    with pytest.raises(HTTPException) as exc_info:
        await sandbox_api._read_file_internal(
            sandbox_id,
            directory_path,
            _FakeRequest(),
            "user-1",
            original_sandbox_id=sandbox_id,
        )

    assert exc_info.value.status_code == 409
    detail = exc_info.value.detail
    assert detail["error_code"] == "WORKSPACE_PATH_IS_DIRECTORY"
    assert detail["path"] == directory_path
    assert detail["path_kind"] == "directory"


@pytest.mark.asyncio
async def test_read_directory_returns_structured_error_when_artifacts_show_directory(monkeypatch):
    project_id = "proj-1"
    sandbox_id = "sb-1"
    directory_path = "/workspace/data"
    sandbox_info = {"id": sandbox_id, "type": "desktop", "state": "running"}
    client = _FakeClient({sandbox_id: {"project_id": project_id, "sandbox": json.dumps(sandbox_info)}})
    monkeypatch.setattr(sandbox_api, "db", _FakeDBConnection(client))

    async def _verify_access(_client, _sandbox_id, _user_id, **_kw):
        return {"project_id": project_id, "sandbox": json.dumps(sandbox_info)}

    async def _unavailable_sandbox(*_a, **_k):
        raise HTTPException(status_code=404, detail="Sandbox unavailable")

    async def _touch(*_a, **_k):
        return None

    async def _artifact_list(*_a, **_k):
        return [
            {
                "name": "nested.md",
                "path": "/workspace/data/nested.md",
                "is_dir": False,
                "size": 6,
                "mod_time": "now",
            }
        ]

    async def _unexpected_artifact_read(*_a, **_k):
        raise AssertionError("artifact byte fallback should not run for directories")

    monkeypatch.setattr(sandbox_api, "verify_sandbox_access", _verify_access)
    monkeypatch.setattr(sandbox_api, "_get_project_sandbox_for_request", _unavailable_sandbox)
    monkeypatch.setattr(sandbox_api, "_touch_project_sandbox_access", _touch)
    monkeypatch.setattr(sandbox_api.workspace_artifacts, "list_entries", _artifact_list)
    monkeypatch.setattr(sandbox_api.workspace_artifacts, "read_artifact_bytes", _unexpected_artifact_read)

    with pytest.raises(HTTPException) as exc_info:
        await sandbox_api._read_file_internal(
            sandbox_id,
            directory_path,
            _FakeRequest(),
            "user-1",
            original_sandbox_id=sandbox_id,
        )

    assert exc_info.value.status_code == 409
    detail = exc_info.value.detail
    assert detail["error_code"] == "WORKSPACE_PATH_IS_DIRECTORY"
    assert detail["path"] == directory_path
    assert detail["path_kind"] == "directory"


@pytest.mark.asyncio
async def test_delete_file_falls_back_to_artifacts_when_sandbox_delete_fails(monkeypatch):
    project_id = "proj-1"
    sandbox_id = "sb-1"
    sandbox_info = {"id": sandbox_id, "type": "desktop", "state": "running"}
    client = _FakeClient({sandbox_id: {"project_id": project_id, "sandbox": json.dumps(sandbox_info)}})
    monkeypatch.setattr(sandbox_api, "db", _FakeDBConnection(client))

    async def _verify_access(_client, _sandbox_id, _user_id, **_kw):
        return {"project_id": project_id, "sandbox": json.dumps(sandbox_info)}

    async def _get_sandbox_for_request(_client, *, project_id: str, requested_sandbox_id: str, **_kw):
        class _FakeSandbox:
            class commands:
                @staticmethod
                def run(_cmd):
                    raise RuntimeError("reattach failed")
        return _FakeSandbox(), requested_sandbox_id, None

    async def _touch(*_a, **_k):
        return None

    deleted = []

    async def _artifact_delete(*_a, **_k):
        deleted.append(True)
        return SimpleNamespace(path="/workspace/a.txt")

    monkeypatch.setattr(sandbox_api, "verify_sandbox_access", _verify_access)
    monkeypatch.setattr(sandbox_api, "_get_project_sandbox_for_request", _get_sandbox_for_request)
    monkeypatch.setattr(sandbox_api, "_touch_project_sandbox_access", _touch)
    monkeypatch.setattr(sandbox_api.workspace_artifacts, "delete_artifact", _artifact_delete)

    response = await sandbox_api.delete_file(
        sandbox_id,
        "/workspace/a.txt",
        _FakeRequest(),
        user_id="user-1",
    )
    assert response["deleted"] is True
    assert deleted

@pytest.mark.asyncio
async def test_thread_workspace_list_merges_files_across_runs_latest_wins(monkeypatch):
    expected_project_id = "proj-thread-workspace"
    thread_id = "thread-workspace-1"
    requested_sandbox_id = f"thread-workspace:{thread_id}"
    client = _FakeClient({})
    monkeypatch.setattr(sandbox_api, "db", _FakeDBConnection(client))

    async def _verify_thread_access(_client, requested_thread_id, user_id):
        assert requested_thread_id == thread_id
        assert user_id == "user-1"
        return {"project_id": expected_project_id, "account_id": "user-1"}

    async def _list_thread_entries(**kwargs):
        assert kwargs["project_id"] == expected_project_id
        assert kwargs["thread_id"] == thread_id
        assert kwargs["path"] == "/workspace"
        return [
            {
                "name": "first.md",
                "path": "/workspace/first.md",
                "is_dir": False,
                "size": 5,
                "mod_time": "2026-05-20T00:00:00Z",
                "delivery_source": "thread_workspace",
                "downloadable": True,
                "artifact_state": "available",
                "source_run_id": "run-1",
            },
            {
                "name": "second.md",
                "path": "/workspace/second.md",
                "is_dir": False,
                "size": 6,
                "mod_time": "2026-05-21T00:00:00Z",
                "delivery_source": "thread_workspace",
                "downloadable": True,
                "artifact_state": "available",
                "source_run_id": "run-2",
            },
        ]

    async def _get_sandbox_for_request(*_args, **_kwargs):
        raise AssertionError("thread workspace requests must not attach live sandbox")

    monkeypatch.setattr(sandbox_api, "verify_thread_access", _verify_thread_access)
    monkeypatch.setattr(sandbox_api.workspace_artifacts, "list_thread_entries", _list_thread_entries)
    monkeypatch.setattr(sandbox_api, "_get_project_sandbox_for_request", _get_sandbox_for_request)

    result = await sandbox_api._list_files_internal(
        requested_sandbox_id,
        "/workspace",
        _FakeRequest(),
        "user-1",
        original_sandbox_id=requested_sandbox_id,
    )

    assert [file["path"] for file in result["files"]] == [
        "/workspace/first.md",
        "/workspace/second.md",
    ]
    assert result["diagnostics"]["identity_source"] == "thread_workspace_artifacts"
    assert result["diagnostics"]["artifact_scope"] == "thread"
    assert result["diagnostics"]["thread_id"] == thread_id


@pytest.mark.asyncio
async def test_thread_workspace_read_uses_latest_thread_artifact(monkeypatch):
    expected_project_id = "proj-thread-read"
    thread_id = "thread-read-1"
    requested_sandbox_id = f"thread-workspace:{thread_id}"
    client = _FakeClient({})
    monkeypatch.setattr(sandbox_api, "db", _FakeDBConnection(client))

    async def _verify_thread_access(_client, requested_thread_id, user_id):
        assert requested_thread_id == thread_id
        assert user_id == "user-1"
        return {"project_id": expected_project_id, "account_id": "user-1"}

    async def _read_thread_artifact_bytes(**kwargs):
        assert kwargs["project_id"] == expected_project_id
        assert kwargs["thread_id"] == thread_id
        assert kwargs["path"] == "/workspace/first.md"
        record = SimpleNamespace(content_type="text/markdown")
        return record, b"latest first"

    async def _get_sandbox_for_request(*_args, **_kwargs):
        raise AssertionError("thread workspace reads must not attach live sandbox")

    monkeypatch.setattr(sandbox_api, "verify_thread_access", _verify_thread_access)
    monkeypatch.setattr(sandbox_api.workspace_artifacts, "read_thread_artifact_bytes", _read_thread_artifact_bytes)
    monkeypatch.setattr(sandbox_api, "_get_project_sandbox_for_request", _get_sandbox_for_request)

    response = await sandbox_api._read_file_internal(
        requested_sandbox_id,
        "/workspace/first.md",
        _FakeRequest(),
        "user-1",
        original_sandbox_id=requested_sandbox_id,
    )

    payload = getattr(response, "body", None)
    if payload is None:
        payload = getattr(response, "content", None)
    assert payload == b"latest first"
    assert response.headers["x-workspace-response-source"] == "thread_workspace"
    assert response.headers["x-workspace-artifact-scope"] == "thread"
    assert response.headers["x-workspace-thread-id"] == thread_id

@pytest.mark.asyncio
async def test_thread_workspace_archive_downloads_files_from_thread_artifacts(monkeypatch):
    expected_project_id = "proj-thread-archive"
    thread_id = "thread-archive-1"
    requested_sandbox_id = f"thread-workspace:{thread_id}"
    client = _FakeClient({})
    monkeypatch.setattr(sandbox_api, "db", _FakeDBConnection(client))

    async def _verify_thread_access(_client, requested_thread_id, user_id):
        assert requested_thread_id == thread_id
        assert user_id == "user-1"
        return {"project_id": expected_project_id, "account_id": "user-1"}

    async def _collect_thread_file_paths(**kwargs):
        assert kwargs["project_id"] == expected_project_id
        assert kwargs["thread_id"] == thread_id
        assert kwargs["root_path"] == "/workspace"
        return ["/workspace/first.md", "/workspace/second.md"]

    async def _read_thread_artifact_bytes(**kwargs):
        path = kwargs["path"]
        return SimpleNamespace(content_type="text/markdown"), {
            "/workspace/first.md": b"first",
            "/workspace/second.md": b"second",
        }[path]

    async def _preferred_lease(*_args, **_kwargs):
        raise AssertionError("thread workspace archive must not resolve sandbox leases")

    monkeypatch.setattr(sandbox_api, "verify_thread_access", _verify_thread_access)
    monkeypatch.setattr(sandbox_api.workspace_artifacts, "collect_thread_file_paths", _collect_thread_file_paths)
    monkeypatch.setattr(sandbox_api.workspace_artifacts, "read_thread_artifact_bytes", _read_thread_artifact_bytes)
    monkeypatch.setattr(sandbox_api, "get_preferred_project_sandbox_lease", _preferred_lease)

    class _BackgroundTasks:
        def __init__(self):
            self.tasks = []

        def add_task(self, func, *args, **kwargs):
            self.tasks.append((func, args, kwargs))

    response = await sandbox_api.download_files_archive(
        requested_sandbox_id,
        _BackgroundTasks(),
        sandbox_api.ArchiveDownloadRequest(root_path="/workspace"),
        _FakeRequest(),
        user_id="user-1",
    )

    assert response.headers["x-archive-response-source"] == "artifact"
    assert response.headers["x-archive-identity-source"] == "thread_workspace_artifacts"
    with zipfile.ZipFile(response.path) as archive:
        assert archive.read("first.md") == b"first"
        assert archive.read("second.md") == b"second"
        report = json.loads(archive.read("_download_report.json"))
    assert report["identity_source"] == "thread_workspace_artifacts"
    assert report["summary"] == {"total": 2, "succeeded": 2, "failed": 0}
    sandbox_api._cleanup_temp_file(response.path)
