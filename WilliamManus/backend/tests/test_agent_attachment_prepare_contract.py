import importlib
import json
import sys
import types
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

fastapi_responses = sys.modules.get("fastapi.responses")
if fastapi_responses is not None and not hasattr(fastapi_responses, "StreamingResponse"):
    class _StreamingResponse:
        def __init__(self, content=None, *args, **kwargs):
            self.body_iterator = content
            self.status_code = kwargs.get("status_code", 200)
            self.headers = kwargs.get("headers", {})

    fastapi_responses.StreamingResponse = _StreamingResponse

def _import_agent_api_with_temporary_stubs():
    original_run_agent_background = sys.modules.get("run_agent_background")
    original_shadow_clone_routes = sys.modules.get("agent.shadow_clone_routes")
    installed_run_agent_background_stub = False
    installed_shadow_clone_routes_stub = False

    if original_run_agent_background is None:
        run_agent_background_stub = types.ModuleType("run_agent_background")
        run_agent_background_stub.run_agent_background = SimpleNamespace(
            send=lambda *args, **kwargs: SimpleNamespace(message_id="msg-1")
        )
        run_agent_background_stub.regular_async_pool_dispatch = SimpleNamespace(
            send=lambda *args, **kwargs: SimpleNamespace(message_id="regular-dispatch-1")
        )
        run_agent_background_stub.delete_sandbox_background = SimpleNamespace(
            send=lambda *args, **kwargs: None
        )
        run_agent_background_stub._cleanup_redis_response_list = (
            lambda *args, **kwargs: None
        )
        run_agent_background_stub.update_agent_run_status = (
            lambda *args, **kwargs: None
        )
        sys.modules["run_agent_background"] = run_agent_background_stub
        installed_run_agent_background_stub = True

    if original_shadow_clone_routes is None:
        shadow_clone_routes_stub = types.ModuleType("agent.shadow_clone_routes")
        shadow_clone_routes_stub.shadow_clone_router = SimpleNamespace()
        sys.modules["agent.shadow_clone_routes"] = shadow_clone_routes_stub
        installed_shadow_clone_routes_stub = True

    try:
        return importlib.import_module("agent.api")
    finally:
        if installed_run_agent_background_stub:
            sys.modules.pop("run_agent_background", None)
        if original_run_agent_background is not None:
            sys.modules["run_agent_background"] = original_run_agent_background

        if installed_shadow_clone_routes_stub:
            sys.modules.pop("agent.shadow_clone_routes", None)
        if original_shadow_clone_routes is not None:
            sys.modules["agent.shadow_clone_routes"] = original_shadow_clone_routes


agent_api = _import_agent_api_with_temporary_stubs()


def test_import_scaffold_does_not_leave_run_agent_background_stub_installed():
    imported_module = sys.modules.get("run_agent_background")
    assert imported_module is None or hasattr(imported_module, "initialize")


class _DummyDB:
    def __init__(self, client):
        self._client = client

    @property
    def client(self):
        async def _client():
            return self._client

        return _client()


class _FakeUploadFile:
    def __init__(self, filename: str, content: bytes, content_type: str | None = None):
        self.filename = filename
        self.content_type = content_type
        self._content = content
        self.closed = False

    async def read(self):
        return self._content

    async def close(self):
        self.closed = True


class _FakeTableQuery:
    def __init__(self, table_name: str, client):
        self._table_name = table_name
        self._client = client
        self._filters = {}

    def select(self, *_args, **_kwargs):
        return self

    def eq(self, field, value):
        self._filters[field] = value
        return self

    async def execute(self):
        if self._table_name == "agents":
            return SimpleNamespace(
                data=[
                    {
                        "agent_id": self._filters.get("agent_id", "agent-1"),
                        "name": "Prepared Contract Agent",
                    }
                ]
            )
        raise AssertionError(f"Unexpected execute() on table {self._table_name}")

    async def insert(self, payload):
        if self._table_name == "threads":
            self._client.thread_inserts.append(payload)
            return SimpleNamespace(data=[{"thread_id": payload["thread_id"]}])
        if self._table_name == "agent_runs":
            self._client.agent_run_inserts.append(payload)
            return SimpleNamespace(data=[{"agent_run_id": payload.get("agent_run_id", "run-1")}])
        raise AssertionError(f"Unexpected insert() on table {self._table_name}")


class _FakeInitiateClient:
    def __init__(self):
        self.thread_inserts = []
        self.agent_run_inserts = []

    def schema(self, _name: str):
        return self

    def table(self, name: str):
        return _FakeTableQuery(name, self)


def _prepared_attachment_payload(path: str = "/workspace/report.pdf") -> str:
    return json.dumps(
        [
            {
                "attachment_id": "artifact-1",
                "name": "report.pdf",
                "original_name": "report.pdf",
                "path": path,
                "size": 12,
                "content_type": "application/pdf",
                "filename": "report.pdf",
                "sha256": "abc123",
            }
        ]
    )


def _install_initiate_capacity_harness(monkeypatch, *, send_impl=None, hard_acquire_result=None):
    acquire_calls = []
    release_calls = []
    update_status_calls = []
    send_calls = []

    async def _load_owned_project(client, *, project_id: str, user_id: str):
        assert project_id == "proj-1"
        assert user_id == "user-1"
        return {"project_id": project_id, "account_id": user_id}

    async def _resolve_shadow_clone_model_selection(**_kwargs):
        return {
            "requested_shadow_clone_main_model": None,
            "requested_shadow_clone_subagent_model": None,
            "effective_shadow_clone_main_model": None,
            "effective_shadow_clone_subagent_model": None,
        }

    async def _capacity_snapshot(**_kwargs):
        return {
            "enabled": True,
            "budget": 16,
            "in_use": 0,
            "remaining": 16,
            "requested_cost": 3,
            "can_admit": True,
        }

    async def _hard_acquire(*, agent_run_id: str, mode, owner_token: str, allow_takeover: bool = False):
        acquire_calls.append(
            {
                "agent_run_id": agent_run_id,
                "mode": mode,
                "owner_token": owner_token,
                "allow_takeover": allow_takeover,
            }
        )
        if hard_acquire_result is not None:
            return dict(hard_acquire_result)
        return {
            "enabled": True,
            "budget": 16,
            "in_use": 3,
            "remaining": 13,
            "requested_cost": 3,
            "can_admit": True,
            "capacity_kind": "shadow_clone",
            "acquired": True,
            "refreshed": False,
            "taken_over": False,
            "owner_conflict": False,
            "lease_ttl_seconds": 90,
        }

    async def _release_capacity(*, agent_run_id: str, owner_token: str):
        release_calls.append(
            {
                "agent_run_id": agent_run_id,
                "owner_token": owner_token,
            }
        )
        return {
            "enabled": True,
            "budget": 16,
            "in_use": 0,
            "remaining": 16,
            "released": True,
            "released_cost": 3,
            "owner_conflict": False,
        }

    async def _update_status(client, agent_run_id: str, status: str, error=None):
        update_status_calls.append(
            {
                "client": client,
                "agent_run_id": agent_run_id,
                "status": status,
                "error": error,
            }
        )
        return True

    def _send(**kwargs):
        send_calls.append(kwargs)
        if send_impl is not None:
            return send_impl(**kwargs)
        return SimpleNamespace(message_id="msg-1")

    async def _noop_async(*_args, **_kwargs):
        return None

    monkeypatch.setattr(agent_api, "instance_id", "test-instance")
    monkeypatch.setattr(agent_api, "resolve_model_config", lambda _name: SimpleNamespace(model_name="gpt-test"))
    monkeypatch.setattr(agent_api, "_load_owned_project", _load_owned_project)
    monkeypatch.setattr(agent_api, "_resolve_shadow_clone_model_selection", _resolve_shadow_clone_model_selection)
    monkeypatch.setattr(agent_api, "_create_adk_session_if_not_exists", _noop_async)
    monkeypatch.setattr(agent_api, "_log_adk_user_message_event", _noop_async)
    monkeypatch.setattr(agent_api, "extract_agent_config", lambda agent_data, _version_data: agent_data)
    monkeypatch.setattr(agent_api, "is_server_concurrency_budget_enabled", lambda: True)
    monkeypatch.setattr(agent_api, "get_run_capacity_cost", lambda _mode: 3)
    monkeypatch.setattr(agent_api, "get_safe_run_capacity_snapshot", _capacity_snapshot)
    monkeypatch.setattr(agent_api, "try_acquire_run_capacity_lease", _hard_acquire)
    monkeypatch.setattr(agent_api, "release_run_capacity_lease", _release_capacity)
    monkeypatch.setattr(agent_api, "update_agent_run_status", _update_status)
    monkeypatch.setattr(
        agent_api.structlog.contextvars,
        "get_contextvars",
        lambda: {"request_id": "req-123", "client_operation_id": "op-456"},
    )
    monkeypatch.setattr(agent_api.run_agent_background, "send", _send)

    return {
        "acquire_calls": acquire_calls,
        "release_calls": release_calls,
        "update_status_calls": update_status_calls,
        "send_calls": send_calls,
    }


def test_canonicalize_workspace_path_renames_duplicates_sequentially():
    existing_paths = {"/workspace/report.pdf"}

    first_name, first_path = agent_api._canonicalize_workspace_path("report.pdf", existing_paths)
    second_name, second_path = agent_api._canonicalize_workspace_path("report.pdf", existing_paths)

    assert first_name == "report (2).pdf"
    assert first_path == "/workspace/report (2).pdf"
    assert second_name == "report (3).pdf"
    assert second_path == "/workspace/report (3).pdf"


@pytest.mark.parametrize(
    ("raw_value", "expected_detail"),
    [
        ("{", "Invalid prepared_attachments_json"),
        ('{"path": "/workspace/a.txt"}', "prepared_attachments_json must be a JSON array"),
        ('[{"path": "/workspace/a.txt"}]', "Invalid prepared attachment payload"),
    ],
)
def test_parse_prepared_attachments_json_rejects_invalid_payloads(raw_value, expected_detail):
    with pytest.raises(HTTPException) as exc_info:
        agent_api._parse_prepared_attachments_json(raw_value)

    assert exc_info.value.status_code == 400
    assert expected_detail in str(exc_info.value.detail)


@pytest.mark.asyncio
async def test_prepare_agent_attachments_reuses_project_and_canonicalizes_duplicates(monkeypatch):
    persisted = []

    async def _collect_paths(**_kwargs):
        return ["/workspace/report.pdf"]

    async def _persist_artifact(**kwargs):
        persisted.append(kwargs)
        content = kwargs["content"]
        return SimpleNamespace(
            artifact_id=f"artifact-{len(persisted)}",
            path=kwargs["path"],
            size_bytes=len(content),
            content_type=kwargs["content_type"],
            sha256=f"sha-{len(persisted)}",
        )

    async def _load_owned_project(client, *, project_id: str, user_id: str):
        assert client is fake_client
        assert project_id == "proj-1"
        assert user_id == "user-1"
        return {"project_id": project_id, "account_id": user_id}

    fake_client = object()
    monkeypatch.setattr(agent_api, "db", _DummyDB(fake_client))
    monkeypatch.setattr(agent_api.workspace_artifacts, "enabled", lambda: True)
    monkeypatch.setattr(agent_api.workspace_artifacts, "collect_file_paths", _collect_paths)
    monkeypatch.setattr(agent_api.workspace_artifacts, "persist_artifact", _persist_artifact)
    monkeypatch.setattr(agent_api, "_load_owned_project", _load_owned_project)

    files = [
        _FakeUploadFile("report.pdf", b"first", "application/pdf"),
        _FakeUploadFile("report.pdf", b"second", "application/pdf"),
    ]

    response = await agent_api.prepare_agent_attachments(
        project_id="proj-1",
        files=files,
        user_id="user-1",
    )

    assert response.project_id == "proj-1"
    assert [attachment.path for attachment in response.attachments] == [
        "/workspace/report (2).pdf",
        "/workspace/report (3).pdf",
    ]
    assert [attachment.name for attachment in response.attachments] == [
        "report (2).pdf",
        "report (3).pdf",
    ]
    assert [entry["metadata"]["filename"] for entry in persisted] == [
        "report (2).pdf",
        "report (3).pdf",
    ]
    assert all(file.closed for file in files)


@pytest.mark.asyncio
async def test_initiate_agent_with_missing_prepared_attachment_preserves_400(monkeypatch):
    fake_client = _FakeInitiateClient()

    async def _load_owned_project(client, *, project_id: str, user_id: str):
        assert client is fake_client
        assert project_id == "proj-1"
        assert user_id == "user-1"
        return {"project_id": project_id, "account_id": user_id}

    async def _missing_artifact(**_kwargs):
        return None

    def _unexpected_placeholder(*_args, **_kwargs):
        raise AssertionError("initiate should reuse provided project_id")

    def _drop_task(coro):
        coro.close()
        return SimpleNamespace()

    async def _resolve_shadow_clone_model_selection(**_kwargs):
        return {
            "requested_shadow_clone_main_model": None,
            "requested_shadow_clone_subagent_model": None,
            "effective_shadow_clone_main_model": None,
            "effective_shadow_clone_subagent_model": None,
        }

    monkeypatch.setattr(agent_api, "db", _DummyDB(fake_client))
    monkeypatch.setattr(agent_api, "instance_id", "test-instance")
    monkeypatch.setattr(agent_api, "resolve_model_config", lambda _name: SimpleNamespace(model_name="gpt-test"))
    monkeypatch.setattr(agent_api, "_load_owned_project", _load_owned_project)
    monkeypatch.setattr(agent_api, "_create_placeholder_project", _unexpected_placeholder)
    monkeypatch.setattr(agent_api, "_resolve_shadow_clone_model_selection", _resolve_shadow_clone_model_selection)
    monkeypatch.setattr(agent_api, "extract_agent_config", lambda agent_data, _version_data: agent_data)
    monkeypatch.setattr(agent_api.workspace_artifacts, "get_artifact_record", _missing_artifact)
    monkeypatch.setattr(agent_api.asyncio, "create_task", _drop_task)

    with pytest.raises(HTTPException) as exc_info:
        await agent_api.initiate_agent_with_files(
            prompt="Use the prepared file",
            model_name=None,
            enable_thinking=False,
            reasoning_effort="low",
            stream=True,
            enable_context_manager=False,
            shadow_clone_mode=None,
            agent_id="agent-1",
            project_id="proj-1",
            prepared_attachments_json=_prepared_attachment_payload(),
            files=[],
            is_agent_builder=False,
            target_agent_id=None,
            user_id="user-1",
        )

    assert exc_info.value.status_code == 400
    assert exc_info.value.detail == "Prepared attachment not found: /workspace/report.pdf"
    assert len(fake_client.thread_inserts) == 1
    assert fake_client.agent_run_inserts == []


@pytest.mark.asyncio
async def test_initiate_agent_with_missing_prepared_attachment_releases_hard_reservation(
    monkeypatch,
):
    fake_client = _FakeInitiateClient()
    harness = _install_initiate_capacity_harness(monkeypatch)

    async def _missing_artifact(*, project_id: str, path: str, client):
        assert project_id == "proj-1"
        assert path == "/workspace/report.pdf"
        assert client is fake_client
        return None

    def _drop_task(coro):
        coro.close()
        return SimpleNamespace()

    monkeypatch.setattr(agent_api, "db", _DummyDB(fake_client))
    monkeypatch.setattr(agent_api.workspace_artifacts, "get_artifact_record", _missing_artifact)
    monkeypatch.setattr(agent_api.asyncio, "create_task", _drop_task)

    with pytest.raises(HTTPException) as exc_info:
        await agent_api.initiate_agent_with_files(
            prompt="Use the prepared file",
            model_name=None,
            enable_thinking=False,
            reasoning_effort="low",
            stream=True,
            enable_context_manager=False,
            shadow_clone_mode="on",
            agent_id="agent-1",
            project_id="proj-1",
            prepared_attachments_json=_prepared_attachment_payload(),
            files=[],
            is_agent_builder=False,
            target_agent_id=None,
            user_id="user-1",
        )

    assert exc_info.value.status_code == 400
    assert len(harness["acquire_calls"]) == 1
    assert harness["release_calls"] == [
        {
            "agent_run_id": harness["acquire_calls"][0]["agent_run_id"],
            "owner_token": harness["acquire_calls"][0]["owner_token"],
        }
    ]
    assert harness["update_status_calls"] == []
    assert len(fake_client.thread_inserts) == 1
    assert fake_client.agent_run_inserts == []


@pytest.mark.asyncio
async def test_initiate_agent_with_files_hard_reserves_capacity_before_insert_and_dispatch(monkeypatch):
    fake_client = _FakeInitiateClient()
    harness = _install_initiate_capacity_harness(monkeypatch)

    monkeypatch.setattr(agent_api, "db", _DummyDB(fake_client))

    response = await agent_api.initiate_agent_with_files(
        prompt="Use shadow clone please",
        model_name=None,
        enable_thinking=False,
        reasoning_effort="low",
        stream=True,
        enable_context_manager=False,
        shadow_clone_mode="on",
        agent_id="agent-1",
        project_id="proj-1",
        prepared_attachments_json=None,
        files=[],
        is_agent_builder=False,
        target_agent_id=None,
        user_id="user-1",
    )

    assert response["agent_run_id"]
    assert response["thread_id"]
    assert len(harness["acquire_calls"]) == 1
    assert harness["acquire_calls"][0]["allow_takeover"] is False
    assert harness["release_calls"] == []
    assert len(fake_client.agent_run_inserts) == 1
    inserted_run = fake_client.agent_run_inserts[0]
    assert inserted_run["agent_run_id"] == response["agent_run_id"]
    assert len(harness["send_calls"]) == 1
    assert harness["send_calls"][0]["agent_run_id"] == response["agent_run_id"]
    assert harness["update_status_calls"] == []


@pytest.mark.asyncio
async def test_initiate_agent_with_files_releases_hard_reservation_when_project_access_check_raises_http_exception(
    monkeypatch,
):
    fake_client = _FakeInitiateClient()
    harness = _install_initiate_capacity_harness(monkeypatch)

    async def _deny_owned_project(client, *, project_id: str, user_id: str):
        assert client is fake_client
        assert project_id == "proj-1"
        assert user_id == "user-1"
        raise HTTPException(status_code=403, detail="Project not found or access denied")

    monkeypatch.setattr(agent_api, "db", _DummyDB(fake_client))
    monkeypatch.setattr(agent_api, "_load_owned_project", _deny_owned_project)

    with pytest.raises(HTTPException) as exc_info:
        await agent_api.initiate_agent_with_files(
            prompt="Use shadow clone please",
            model_name=None,
            enable_thinking=False,
            reasoning_effort="low",
            stream=True,
            enable_context_manager=False,
            shadow_clone_mode="on",
            agent_id="agent-1",
            project_id="proj-1",
            prepared_attachments_json=None,
            files=[],
            is_agent_builder=False,
            target_agent_id=None,
            user_id="user-1",
        )

    assert exc_info.value.status_code == 403
    assert exc_info.value.detail == "Project not found or access denied"
    assert len(harness["acquire_calls"]) == 1
    assert harness["release_calls"] == [
        {
            "agent_run_id": harness["acquire_calls"][0]["agent_run_id"],
            "owner_token": harness["acquire_calls"][0]["owner_token"],
        }
    ]
    assert harness["update_status_calls"] == []
    assert fake_client.thread_inserts == []
    assert fake_client.agent_run_inserts == []


@pytest.mark.asyncio
async def test_initiate_agent_with_files_hard_reserves_capacity_before_creating_placeholder_project(
    monkeypatch,
):
    fake_client = _FakeInitiateClient()
    _install_initiate_capacity_harness(monkeypatch)
    call_order = []

    original_acquire = agent_api.try_acquire_run_capacity_lease

    async def _ordered_acquire(**kwargs):
        call_order.append("reserve")
        return await original_acquire(**kwargs)

    async def _create_placeholder_project(_client, *, user_id: str, name: str):
        assert user_id == "user-1"
        assert name
        call_order.append("project")
        return "proj-created"

    def _drop_task(coro):
        coro.close()
        return SimpleNamespace()

    monkeypatch.setattr(agent_api, "db", _DummyDB(fake_client))
    monkeypatch.setattr(agent_api, "try_acquire_run_capacity_lease", _ordered_acquire)
    monkeypatch.setattr(agent_api, "_create_placeholder_project", _create_placeholder_project)
    monkeypatch.setattr(agent_api.asyncio, "create_task", _drop_task)

    response = await agent_api.initiate_agent_with_files(
        prompt="Create a new project",
        model_name=None,
        enable_thinking=False,
        reasoning_effort="low",
        stream=True,
        enable_context_manager=False,
        shadow_clone_mode="on",
        agent_id="agent-1",
        project_id=None,
        prepared_attachments_json=None,
        files=[],
        is_agent_builder=False,
        target_agent_id=None,
        user_id="user-1",
    )

    assert response["agent_run_id"]
    assert response["thread_id"]
    assert call_order[:2] == ["reserve", "project"]
    assert fake_client.thread_inserts[0]["project_id"] == "proj-created"


@pytest.mark.asyncio
async def test_initiate_agent_with_files_returns_429_before_creating_placeholder_project_when_hard_reservation_denies(
    monkeypatch,
):
    fake_client = _FakeInitiateClient()
    harness = _install_initiate_capacity_harness(
        monkeypatch,
        hard_acquire_result={
            "enabled": True,
            "budget": 16,
            "in_use": 15,
            "remaining": 1,
            "requested_cost": 3,
            "can_admit": False,
            "capacity_kind": "shadow_clone",
            "acquired": False,
            "refreshed": False,
            "taken_over": False,
            "owner_conflict": False,
            "lease_ttl_seconds": 90,
        },
    )

    async def _unexpected_placeholder_project(*_args, **_kwargs):
        raise AssertionError("placeholder project creation must not happen after hard reservation denies")

    monkeypatch.setattr(agent_api, "db", _DummyDB(fake_client))
    monkeypatch.setattr(agent_api, "_create_placeholder_project", _unexpected_placeholder_project)

    with pytest.raises(HTTPException) as exc_info:
        await agent_api.initiate_agent_with_files(
            prompt="Reject before project creation",
            model_name=None,
            enable_thinking=False,
            reasoning_effort="low",
            stream=True,
            enable_context_manager=False,
            shadow_clone_mode="on",
            agent_id="agent-1",
            project_id=None,
            prepared_attachments_json=None,
            files=[],
            is_agent_builder=False,
            target_agent_id=None,
            user_id="user-1",
        )

    assert exc_info.value.status_code == 429
    assert exc_info.value.detail == {
        "code": "shadow_clone_limit",
        "message": "Server concurrency budget is exhausted. Please retry shortly.",
        "capacity_kind": "shadow_clone",
        "budget": 16,
        "in_use": 15,
        "remaining": 1,
        "requested_cost": 3,
    }
    assert len(harness["acquire_calls"]) == 1
    assert fake_client.thread_inserts == []
    assert fake_client.agent_run_inserts == []
    assert harness["release_calls"] == []


@pytest.mark.asyncio
async def test_initiate_agent_with_files_releases_hard_reservation_and_marks_failed_when_dispatch_breaks(
    monkeypatch,
):
    fake_client = _FakeInitiateClient()

    def _raising_send(**_kwargs):
        raise RuntimeError("broker unavailable")

    harness = _install_initiate_capacity_harness(monkeypatch, send_impl=_raising_send)

    monkeypatch.setattr(agent_api, "db", _DummyDB(fake_client))

    with pytest.raises(HTTPException) as exc_info:
        await agent_api.initiate_agent_with_files(
            prompt="Use shadow clone please",
            model_name=None,
            enable_thinking=False,
            reasoning_effort="low",
            stream=True,
            enable_context_manager=False,
            shadow_clone_mode="on",
            agent_id="agent-1",
            project_id="proj-1",
            prepared_attachments_json=None,
            files=[],
            is_agent_builder=False,
            target_agent_id=None,
            user_id="user-1",
        )

    assert exc_info.value.status_code == 500
    assert exc_info.value.detail == "Failed to initiate agent session: broker unavailable"
    assert len(fake_client.agent_run_inserts) == 1
    inserted_run = fake_client.agent_run_inserts[0]
    assert len(harness["acquire_calls"]) == 1
    assert harness["acquire_calls"][0]["agent_run_id"] == inserted_run["agent_run_id"]
    assert harness["release_calls"] == [
        {
            "agent_run_id": inserted_run["agent_run_id"],
            "owner_token": harness["acquire_calls"][0]["owner_token"],
        }
    ]
    assert harness["update_status_calls"] == [
        {
            "client": fake_client,
            "agent_run_id": inserted_run["agent_run_id"],
            "status": "failed",
            "error": "Failed to dispatch agent run: broker unavailable",
        }
    ]


@pytest.mark.asyncio
async def test_initiate_agent_with_files_returns_429_when_hard_reservation_denies_after_snapshot_passes(
    monkeypatch,
):
    fake_client = _FakeInitiateClient()
    harness = _install_initiate_capacity_harness(
        monkeypatch,
        hard_acquire_result={
            "enabled": True,
            "budget": 16,
            "in_use": 15,
            "remaining": 1,
            "requested_cost": 3,
            "can_admit": False,
            "capacity_kind": "shadow_clone",
            "acquired": False,
            "refreshed": False,
            "taken_over": False,
            "owner_conflict": False,
            "lease_ttl_seconds": 90,
        },
    )

    monkeypatch.setattr(agent_api, "db", _DummyDB(fake_client))

    with pytest.raises(HTTPException) as exc_info:
        await agent_api.initiate_agent_with_files(
            prompt="Use shadow clone please",
            model_name=None,
            enable_thinking=False,
            reasoning_effort="low",
            stream=True,
            enable_context_manager=False,
            shadow_clone_mode="on",
            agent_id="agent-1",
            project_id="proj-1",
            prepared_attachments_json=None,
            files=[],
            is_agent_builder=False,
            target_agent_id=None,
            user_id="user-1",
        )

    assert exc_info.value.status_code == 429
    assert exc_info.value.detail == {
        "code": "shadow_clone_limit",
        "message": "Server concurrency budget is exhausted. Please retry shortly.",
        "capacity_kind": "shadow_clone",
        "budget": 16,
        "in_use": 15,
        "remaining": 1,
        "requested_cost": 3,
    }
    assert len(harness["acquire_calls"]) == 1
    assert fake_client.agent_run_inserts == []
    assert harness["release_calls"] == []


@pytest.mark.asyncio
async def test_initiate_agent_with_files_rejects_when_server_capacity_is_exhausted(monkeypatch):
    fake_client = _FakeInitiateClient()

    async def _load_owned_project(client, *, project_id: str, user_id: str):
        assert client is fake_client
        assert project_id == "proj-1"
        assert user_id == "user-1"
        return {"project_id": project_id, "account_id": user_id}

    async def _resolve_shadow_clone_model_selection(**_kwargs):
        return {
            "requested_shadow_clone_main_model": None,
            "requested_shadow_clone_subagent_model": None,
            "effective_shadow_clone_main_model": None,
            "effective_shadow_clone_subagent_model": None,
        }

    monkeypatch.setattr(agent_api, "db", _DummyDB(fake_client))
    monkeypatch.setattr(agent_api, "instance_id", "test-instance")
    monkeypatch.setattr(agent_api, "resolve_model_config", lambda _name: SimpleNamespace(model_name="gpt-test"))
    monkeypatch.setattr(agent_api, "_load_owned_project", _load_owned_project)
    monkeypatch.setattr(agent_api, "_resolve_shadow_clone_model_selection", _resolve_shadow_clone_model_selection)
    monkeypatch.setattr(agent_api, "extract_agent_config", lambda agent_data, _version_data: agent_data)
    monkeypatch.setattr(agent_api, "is_server_concurrency_budget_enabled", lambda: True)
    monkeypatch.setattr(agent_api, "get_run_capacity_cost", lambda _mode: 3)

    async def _capacity_snapshot(**_kwargs):
        return {
            "enabled": True,
            "budget": 16,
            "in_use": 15,
            "remaining": 1,
            "requested_cost": 3,
            "can_admit": False,
        }

    monkeypatch.setattr(agent_api, "get_safe_run_capacity_snapshot", _capacity_snapshot)

    with pytest.raises(HTTPException) as exc_info:
        await agent_api.initiate_agent_with_files(
            prompt="Use shadow clone please",
            model_name=None,
            enable_thinking=False,
            reasoning_effort="low",
            stream=True,
            enable_context_manager=False,
            shadow_clone_mode="on",
            agent_id="agent-1",
            project_id="proj-1",
            prepared_attachments_json=None,
            files=[],
            is_agent_builder=False,
            target_agent_id=None,
            user_id="user-1",
        )

    assert exc_info.value.status_code == 429
    assert exc_info.value.detail == {
        "code": "shadow_clone_limit",
        "message": "Server concurrency budget is exhausted. Please retry shortly.",
        "capacity_kind": "shadow_clone",
        "budget": 16,
        "in_use": 15,
        "remaining": 1,
        "requested_cost": 3,
    }
    assert fake_client.thread_inserts == []
    assert fake_client.agent_run_inserts == []


@pytest.mark.asyncio
async def test_initiate_agent_with_files_regular_mode_uses_regular_capacity_limit_detail(monkeypatch):
    fake_client = _FakeInitiateClient()
    captured_snapshot_kwargs = {}

    async def _load_owned_project(client, *, project_id: str, user_id: str):
        assert client is fake_client
        assert project_id == "proj-1"
        assert user_id == "user-1"
        return {"project_id": project_id, "account_id": user_id}

    async def _resolve_shadow_clone_model_selection(**_kwargs):
        return {
            "requested_shadow_clone_main_model": None,
            "requested_shadow_clone_subagent_model": None,
            "effective_shadow_clone_main_model": None,
            "effective_shadow_clone_subagent_model": None,
        }

    monkeypatch.setattr(agent_api, "db", _DummyDB(fake_client))
    monkeypatch.setattr(agent_api, "instance_id", "test-instance")
    monkeypatch.setattr(agent_api, "resolve_model_config", lambda _name: SimpleNamespace(model_name="gpt-test"))
    monkeypatch.setattr(agent_api, "_load_owned_project", _load_owned_project)
    monkeypatch.setattr(agent_api, "_resolve_shadow_clone_model_selection", _resolve_shadow_clone_model_selection)
    monkeypatch.setattr(agent_api, "extract_agent_config", lambda agent_data, _version_data: agent_data)
    monkeypatch.setattr(agent_api, "is_server_concurrency_budget_enabled", lambda: True)
    monkeypatch.setattr(agent_api, "get_run_capacity_cost", lambda _mode: 1)

    async def _capacity_snapshot(**kwargs):
        captured_snapshot_kwargs.update(kwargs)
        return {
            "enabled": True,
            "budget": 16,
            "in_use": 16,
            "remaining": 0,
            "requested_cost": 1,
            "can_admit": False,
        }

    monkeypatch.setattr(agent_api, "get_safe_run_capacity_snapshot", _capacity_snapshot)

    with pytest.raises(HTTPException) as exc_info:
        await agent_api.initiate_agent_with_files(
            prompt="Use regular mode",
            model_name=None,
            enable_thinking=False,
            reasoning_effort="low",
            stream=True,
            enable_context_manager=False,
            shadow_clone_mode="off",
            agent_id="agent-1",
            project_id="proj-1",
            prepared_attachments_json=None,
            files=[],
            is_agent_builder=False,
            target_agent_id=None,
            user_id="user-1",
        )

    assert exc_info.value.status_code == 429
    assert captured_snapshot_kwargs["mode"] == "off"
    assert captured_snapshot_kwargs["requested_cost"] == 1
    assert exc_info.value.detail == {
        "code": "server_concurrency_limit",
        "message": "Server concurrency budget is exhausted. Please retry shortly.",
        "capacity_kind": "regular",
        "budget": 16,
        "in_use": 16,
        "remaining": 0,
        "requested_cost": 1,
    }
    assert fake_client.thread_inserts == []
    assert fake_client.agent_run_inserts == []


@pytest.mark.asyncio
async def test_initiate_agent_with_files_prices_capacity_from_effective_shadow_clone_mode(
    monkeypatch,
):
    fake_client = _FakeInitiateClient()
    captured_capacity_mode = {}
    captured_snapshot_kwargs = {}

    async def _load_owned_project(client, *, project_id: str, user_id: str):
        assert client is fake_client
        assert project_id == "proj-1"
        assert user_id == "user-1"
        return {"project_id": project_id, "account_id": user_id}

    async def _resolve_shadow_clone_model_selection(**_kwargs):
        return {
            "requested_shadow_clone_main_model": None,
            "requested_shadow_clone_subagent_model": None,
            "effective_shadow_clone_main_model": None,
            "effective_shadow_clone_subagent_model": None,
        }

    def _capture_capacity_cost(mode):
        captured_capacity_mode["value"] = getattr(mode, "value", mode)
        return 3

    async def _capacity_snapshot(**kwargs):
        captured_snapshot_kwargs.update(kwargs)
        return {
            "enabled": True,
            "budget": 16,
            "in_use": 15,
            "remaining": 1,
            "requested_cost": 3,
            "can_admit": False,
        }

    monkeypatch.setattr(agent_api, "db", _DummyDB(fake_client))
    monkeypatch.setattr(agent_api, "instance_id", "test-instance")
    monkeypatch.setattr(agent_api, "resolve_model_config", lambda _name: SimpleNamespace(model_name="gpt-test"))
    monkeypatch.setattr(agent_api, "_load_owned_project", _load_owned_project)
    monkeypatch.setattr(agent_api, "_resolve_shadow_clone_model_selection", _resolve_shadow_clone_model_selection)
    monkeypatch.setattr(agent_api, "extract_agent_config", lambda agent_data, _version_data: agent_data)
    monkeypatch.setattr(agent_api, "is_server_concurrency_budget_enabled", lambda: True)
    monkeypatch.setattr(agent_api, "get_run_capacity_cost", _capture_capacity_cost)
    monkeypatch.setattr(agent_api, "get_safe_run_capacity_snapshot", _capacity_snapshot)

    with pytest.raises(HTTPException) as exc_info:
        await agent_api.initiate_agent_with_files(
            prompt="Use default shadow clone mode",
            model_name=None,
            enable_thinking=False,
            reasoning_effort="low",
            stream=True,
            enable_context_manager=False,
            shadow_clone_mode=None,
            agent_id="agent-1",
            project_id="proj-1",
            prepared_attachments_json=None,
            files=[],
            is_agent_builder=False,
            target_agent_id=None,
            user_id="user-1",
        )

    assert captured_capacity_mode["value"] == "auto"
    assert captured_snapshot_kwargs["mode"] == "auto"
    assert captured_snapshot_kwargs["requested_cost"] == 3
    assert exc_info.value.status_code == 429
    assert exc_info.value.detail["code"] == "shadow_clone_limit"
    assert fake_client.thread_inserts == []
    assert fake_client.agent_run_inserts == []
