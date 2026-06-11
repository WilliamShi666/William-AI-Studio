from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any, Dict, List

import pytest
from fastapi import HTTPException

import agent.api as agent_api
import agent.shadow_clone_routes as shadow_clone_routes
import agent.utils as agent_utils


@dataclass
class _FakeResult:
    data: List[Dict[str, Any]]


class _FakeTableQuery:
    def __init__(self, rows: List[Dict[str, Any]], *, table_name: str = ""):
        self._rows = rows
        self._filters: Dict[str, Any] = {}
        self._table_name = table_name

    def select(self, fields="*", **_kwargs):
        if self._table_name == "agent_runs" and "project_id" in str(fields):
            raise AssertionError("agent_runs table does not expose a project_id column")
        return self

    def eq(self, key: str, value: Any):
        self._filters[key] = value
        return self

    def order(self, *_args, **_kwargs):
        return self

    async def execute(self):
        result = []
        for row in self._rows:
            if all(row.get(key) == value for key, value in self._filters.items()):
                result.append(row)
        return _FakeResult(data=result)


class _FakeClient:
    def __init__(
        self, *, agent_runs: List[Dict[str, Any]], threads: List[Dict[str, Any]]
    ):
        self._tables = {
            "agent_runs": agent_runs,
            "threads": threads,
        }

    def table(self, table_name: str):
        return _FakeTableQuery(self._tables.get(table_name, []), table_name=table_name)


class _FakeDBConnection:
    def __init__(self, client: _FakeClient):
        self._client = client

    @property
    async def client(self):
        return self._client


def _install_fake_db(monkeypatch, *, agent_runs, threads):
    client = _FakeClient(agent_runs=agent_runs, threads=threads)
    monkeypatch.setattr(
        shadow_clone_routes,
        "DBConnection",
        lambda: _FakeDBConnection(client),
    )
    return client


class _SelectAwareTableQuery:
    def __init__(self, rows: List[Dict[str, Any]]):
        self._rows = rows
        self._filters: Dict[str, Any] = {}
        self._selected_fields: str | None = None

    def select(self, fields="*", **_kwargs):
        self._selected_fields = fields
        return self

    def eq(self, key: str, value: Any):
        self._filters[key] = value
        return self

    def order(self, *_args, **_kwargs):
        return self

    async def execute(self):
        matched_rows = [
            dict(row)
            for row in self._rows
            if all(row.get(key) == value for key, value in self._filters.items())
        ]
        if not self._selected_fields or self._selected_fields == "*":
            return _FakeResult(data=matched_rows)

        selected_keys = [
            field.strip() for field in self._selected_fields.split(",") if field.strip()
        ]
        projected_rows = [
            {key: row.get(key) for key in selected_keys} for row in matched_rows
        ]
        return _FakeResult(data=projected_rows)


class _SelectAwareClient:
    def __init__(self, tables: Dict[str, List[Dict[str, Any]]]):
        self._tables = tables

    def table(self, table_name: str):
        return _SelectAwareTableQuery(self._tables.get(table_name, []))


def _expected_file_delivery_source(
    *,
    agent_run_id: str,
    browse_sandbox_id: str | None = None,
    archive_sandbox_id: str | None = None,
    identity_source: str = "unavailable",
    binding_state: str | None = None,
    environment_status: str | None = None,
    environment_ready: bool | None = None,
    execution_epoch: int | None = None,
    manifest_sandbox_id: str | None = None,
    terminal_status: str | None = None,
):
    return {
        "agent_run_id": agent_run_id,
        "browse_sandbox_id": browse_sandbox_id,
        "archive_sandbox_id": archive_sandbox_id,
        "browse_root_path": "/workspace",
        "archive_root_path": "/workspace",
        "identity_source": identity_source,
        "binding_state": binding_state,
        "environment_status": environment_status,
        "environment_ready": environment_ready,
        "execution_epoch": execution_epoch,
        "manifest_sandbox_id": manifest_sandbox_id,
        "terminal_status": terminal_status,
    }


def test_shadow_clone_model_policy_modes_include_v2() -> None:
    assert agent_api._is_shadow_clone_model_policy_mode("on") is True
    assert agent_api._is_shadow_clone_model_policy_mode("auto") is True
    assert agent_api._is_shadow_clone_model_policy_mode("v2") is True
    assert agent_api._is_shadow_clone_model_policy_mode("off") is False


def test_shadow_clone_runtime_metadata_matches_actual_backend_override_gate(
    monkeypatch,
) -> None:
    monkeypatch.setenv("AGENT_BACKEND", "claude_sdk")
    monkeypatch.delenv("ALLOW_CLAUDE_SDK_BACKEND", raising=False)
    assert agent_api._resolve_shadow_clone_runtime_value("on") == "v2"

    monkeypatch.setenv("ALLOW_CLAUDE_SDK_BACKEND", "1")
    assert agent_api._resolve_shadow_clone_runtime_value("on") == "claude_sdk"

    monkeypatch.setenv("AGENT_BACKEND", "agentscope")
    assert agent_api._resolve_shadow_clone_runtime_value("auto") == "v2"
    assert agent_api._resolve_shadow_clone_runtime_value("off") == "off"


def test_shadow_clone_runtime_metadata_supports_claude_agent_sdk_alias_for_multi_agent_modes(
    monkeypatch,
) -> None:
    monkeypatch.setenv("AGENT_BACKEND", "claude_agent_sdk")
    monkeypatch.setenv("ALLOW_CLAUDE_SDK_BACKEND", "1")

    assert agent_api._resolve_shadow_clone_runtime_value("on") == "claude_sdk"
    assert agent_api._resolve_shadow_clone_runtime_value("auto") == "claude_sdk"


def test_shadow_clone_runtime_metadata_matches_explicit_agent_config_backend(
    monkeypatch,
) -> None:
    monkeypatch.setenv("AGENT_BACKEND", "agentscope")
    monkeypatch.setenv("ALLOW_CLAUDE_SDK_BACKEND", "1")

    assert (
        agent_api._resolve_shadow_clone_runtime_value(
            "on",
            agent_config={"agent_backend": "claude_sdk"},
        )
        == "claude_sdk"
    )
    assert (
        agent_api._resolve_shadow_clone_runtime_value(
            "auto",
            agent_config={"backend": "claude_agent_sdk"},
        )
        == "claude_sdk"
    )


def test_shadow_clone_v2_stream_routing_includes_on_when_runtime_is_v2() -> None:
    assert (
        agent_api._agent_run_uses_shadow_clone_v2_event_stream(
            {
                "metadata": {
                    "requested_shadow_clone_mode": "on",
                    "shadow_clone_mode": "on",
                    "shadow_clone_runtime": "v2",
                }
            }
        )
        is True
    )


def test_shadow_clone_v2_stream_routing_excludes_claude_runtime() -> None:
    assert (
        agent_api._agent_run_uses_shadow_clone_v2_event_stream(
            {
                "metadata": {
                    "requested_shadow_clone_mode": "on",
                    "shadow_clone_mode": "on",
                    "shadow_clone_runtime": "claude_sdk",
                }
            }
        )
        is False
    )


@pytest.mark.asyncio
async def test_shadow_clone_model_selection_accepts_new_per_run_overrides(monkeypatch):
    async def _fake_user(_client, _user_id: str):
        return {"id": _user_id, "role": "admin", "status": "active"}

    async def _fake_global_config(client=None):
        return {"main_model_name": None, "subagent_model_name": None}

    monkeypatch.setattr(agent_api, "_get_user_role_record", _fake_user)
    monkeypatch.setattr(
        agent_api, "read_shadow_clone_model_config", _fake_global_config
    )

    result = await agent_api._resolve_shadow_clone_model_selection(
        client=object(),
        user_id="admin-1",
        requested_main_model="  moonshotai/kimi-k2.6  ",
        requested_subagent_model="  deepseek-v4-flash-high  ",
    )

    assert result["requested_shadow_clone_main_model"] == "kimi-k2.6"
    assert result["requested_shadow_clone_subagent_model"] == "deepseek-v4-flash-high"
    assert result["effective_shadow_clone_main_model"] == "kimi-k2.6"
    assert result["effective_shadow_clone_subagent_model"] == "deepseek-v4-flash-high"


@pytest.mark.asyncio
async def test_shadow_clone_model_selection_rejects_unsupported_per_run_override(
    monkeypatch,
):
    async def _fake_user(_client, _user_id: str):
        return {"id": _user_id, "role": "admin", "status": "active"}

    monkeypatch.setattr(agent_api, "_get_user_role_record", _fake_user)

    with pytest.raises(HTTPException) as exc_info:
        await agent_api._resolve_shadow_clone_model_selection(
            client=object(),
            user_id="admin-1",
            requested_main_model="ppio/moonshotai/kimi-k2.6",
            requested_subagent_model=None,
        )

    assert exc_info.value.status_code == 400
    assert (
        exc_info.value.detail
        == "shadow_clone_main_model must be an official Kimi/DeepSeek model or an openrouter/... model"
    )


@pytest.fixture(autouse=True)
def _stub_shadow_clone_lease(monkeypatch):
    async def _fake_get_lease(_run_id: str):
        return None

    monkeypatch.setattr(
        shadow_clone_routes,
        "get_run_sandbox_lease",
        _fake_get_lease,
    )


@pytest.mark.asyncio
async def test_confirm_shadow_clone_success(monkeypatch):
    _install_fake_db(
        monkeypatch,
        agent_runs=[{"agent_run_id": "run-1", "thread_id": "thread-1"}],
        threads=[{"thread_id": "thread-1", "account_id": "user-1"}],
    )

    async def _state(_run_id: str):
        return {"status": "confirming", "environment": {"ready": True}}

    published = []

    async def _publish(channel: str, message: str):
        published.append((channel, message))
        return 1

    monkeypatch.setattr(shadow_clone_routes, "get_state", _state)
    monkeypatch.setattr(
        shadow_clone_routes,
        "get_confirmation_result",
        lambda _run_id: _stateful_none(),
    )
    monkeypatch.setattr(
        shadow_clone_routes,
        "record_confirmation_result",
        lambda _run_id, _result: _recorded_confirm(),
    )
    monkeypatch.setattr(shadow_clone_routes.redis_service, "publish", _publish)

    result = await shadow_clone_routes.confirm_shadow_clone(
        agent_run_id="run-1",
        user_id="user-1",
    )

    assert result == {"status": "confirmed"}
    assert published == [("agent_run:run-1:shadow_clone_confirm", "CONFIRMED")]


async def _stateful_none():
    return None


async def _recorded_confirm():
    return (
        shadow_clone_routes.ConfirmationDecisionWriteStatus.RECORDED,
        shadow_clone_routes.ConfirmationResult.CONFIRMED,
    )


async def _recorded_deny():
    return (
        shadow_clone_routes.ConfirmationDecisionWriteStatus.RECORDED,
        shadow_clone_routes.ConfirmationResult.DENIED,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error_message", "expected_shadow_status", "expected_reason"),
    [
        (None, "cancelled", "external_stop"),
        ("shadow clone failed", "failed", "external_stop_failed"),
    ],
)
async def test_stop_agent_run_syncs_shadow_clone_terminal_truth_before_stop_publish(
    monkeypatch,
    error_message,
    expected_shadow_status,
    expected_reason,
):
    operations = []
    publish_calls = []
    cleanup_calls = []
    force_calls = []
    lease_calls = []

    class _FakeAgentDB:
        @property
        async def client(self):
            return object()

    async def _fake_update_status(_client, _run_id, _status, error=None):
        operations.append("db_status")
        return True

    async def _fake_force_terminal(run_id, *, status, reason, error_message=None):
        operations.append("force_terminal_state")
        force_calls.append(
            {
                "run_id": run_id,
                "status": status,
                "reason": reason,
                "error_message": error_message,
            }
        )
        return {"execution_epoch": 7}

    async def _fake_update_lease(run_id, **kwargs):
        operations.append("update_run_sandbox_lease")
        lease_calls.append((run_id, kwargs))
        return {"run_id": run_id, **kwargs}

    async def _fake_publish(channel, message):
        operations.append(f"publish:{channel}")
        publish_calls.append((channel, message))
        return 1

    async def _fake_keys(pattern):
        operations.append(f"keys:{pattern}")
        return (
            ["active_run:worker-1:run-stop"]
            if pattern == "active_run:*:run-stop"
            else []
        )

    async def _fake_cleanup(run_id):
        operations.append("cleanup_redis_response_list")
        cleanup_calls.append(run_id)
        return None

    monkeypatch.setattr(agent_api, "db", _FakeAgentDB())
    monkeypatch.setattr(agent_api, "update_agent_run_status", _fake_update_status)
    monkeypatch.setattr(agent_api, "force_terminal_state", _fake_force_terminal)
    monkeypatch.setattr(agent_api, "update_run_sandbox_lease", _fake_update_lease)
    monkeypatch.setattr(agent_api.redis, "publish", _fake_publish)
    monkeypatch.setattr(agent_api.redis, "scan_keys", _fake_keys, raising=False)
    monkeypatch.setattr(agent_api, "_cleanup_redis_response_list", _fake_cleanup)

    await agent_api.stop_agent_run("run-stop", error_message=error_message)

    assert force_calls == [
        {
            "run_id": "run-stop",
            "status": expected_shadow_status,
            "reason": expected_reason,
            "error_message": error_message,
        }
    ]
    assert lease_calls == [
        (
            "run-stop",
            {
                "binding_state": "released",
                "environment_ready": False,
                "environment_status": expected_shadow_status,
                "last_error": error_message,
                "execution_epoch": 7,
                "lease_epoch": 7,
            },
        )
    ]
    first_publish_index = next(
        idx for idx, op in enumerate(operations) if op.startswith("publish:")
    )
    assert operations.index("force_terminal_state") < first_publish_index
    assert operations.index("update_run_sandbox_lease") < first_publish_index


@pytest.mark.asyncio
async def test_stop_agent_run_still_publishes_stop_when_db_status_write_fails(
    monkeypatch,
):
    operations = []
    publish_calls = []
    cleanup_calls = []

    class _FakeAgentDB:
        @property
        async def client(self):
            return object()

    async def _fake_update_status(_client, _run_id, _status, error=None):
        operations.append("db_status")
        return False

    async def _fake_force_terminal(*args, **kwargs):
        operations.append("force_terminal_state")
        return {"execution_epoch": 3}

    async def _fake_update_lease(*args, **kwargs):
        operations.append("update_run_sandbox_lease")
        return {"ok": True}

    async def _fake_publish(channel, message):
        operations.append(f"publish:{channel}")
        publish_calls.append((channel, message))
        return 1

    async def _fake_keys(pattern):
        operations.append(f"keys:{pattern}")
        return (
            ["active_run:worker-1:run-stop"]
            if pattern == "active_run:*:run-stop"
            else []
        )

    async def _fake_cleanup(run_id):
        operations.append("cleanup_redis_response_list")
        cleanup_calls.append(run_id)
        return None

    monkeypatch.setattr(agent_api, "db", _FakeAgentDB())
    monkeypatch.setattr(agent_api, "update_agent_run_status", _fake_update_status)
    monkeypatch.setattr(agent_api, "force_terminal_state", _fake_force_terminal)
    monkeypatch.setattr(agent_api, "update_run_sandbox_lease", _fake_update_lease)
    monkeypatch.setattr(agent_api.redis, "publish", _fake_publish)
    monkeypatch.setattr(agent_api.redis, "scan_keys", _fake_keys, raising=False)
    monkeypatch.setattr(agent_api, "_cleanup_redis_response_list", _fake_cleanup)

    await agent_api.stop_agent_run("run-stop")

    assert publish_calls == [
        ("agent_run:run-stop:control", "STOP"),
        ("agent_run:run-stop:control:worker-1", "STOP"),
    ]
    assert cleanup_calls == ["run-stop"]


@pytest.mark.asyncio
async def test_stop_agent_run_still_publishes_stop_when_db_status_write_raises(
    monkeypatch,
):
    publish_calls = []

    class _FakeAgentDB:
        @property
        async def client(self):
            return object()

    async def _fake_update_status(_client, _run_id, _status, error=None):
        raise RuntimeError("database write unavailable")

    async def _fake_force_terminal(*args, **kwargs):
        return {"execution_epoch": 3}

    async def _fake_update_lease(*args, **kwargs):
        return {"ok": True}

    async def _fake_publish(channel, message):
        publish_calls.append((channel, message))
        return 1

    async def _fake_keys(pattern):
        return (
            ["active_run:worker-1:run-stop"]
            if pattern == "active_run:*:run-stop"
            else []
        )

    async def _fake_cleanup(_run_id):
        return None

    monkeypatch.setattr(agent_api, "db", _FakeAgentDB())
    monkeypatch.setattr(agent_api, "update_agent_run_status", _fake_update_status)
    monkeypatch.setattr(agent_api, "force_terminal_state", _fake_force_terminal)
    monkeypatch.setattr(agent_api, "update_run_sandbox_lease", _fake_update_lease)
    monkeypatch.setattr(agent_api.redis, "publish", _fake_publish)
    monkeypatch.setattr(agent_api.redis, "scan_keys", _fake_keys, raising=False)
    monkeypatch.setattr(agent_api, "_cleanup_redis_response_list", _fake_cleanup)

    await agent_api.stop_agent_run("run-stop")

    assert publish_calls == [
        ("agent_run:run-stop:control", "STOP"),
        ("agent_run:run-stop:control:worker-1", "STOP"),
    ]


@pytest.mark.asyncio
async def test_stop_agent_run_still_publishes_stop_when_db_client_lookup_raises(
    monkeypatch,
):
    publish_calls = []
    update_status_calls = []

    class _FakeAgentDB:
        @property
        async def client(self):
            raise RuntimeError("database client unavailable")

    async def _fake_update_status(*args, **kwargs):
        update_status_calls.append((args, kwargs))
        return True

    async def _fake_force_terminal(*args, **kwargs):
        return {"execution_epoch": 3}

    async def _fake_update_lease(*args, **kwargs):
        return {"ok": True}

    async def _fake_publish(channel, message):
        publish_calls.append((channel, message))
        return 1

    async def _fake_keys(pattern):
        return (
            ["active_run:worker-1:run-stop"]
            if pattern == "active_run:*:run-stop"
            else []
        )

    async def _fake_cleanup(_run_id):
        return None

    monkeypatch.setattr(agent_api, "db", _FakeAgentDB())
    monkeypatch.setattr(agent_api, "update_agent_run_status", _fake_update_status)
    monkeypatch.setattr(agent_api, "force_terminal_state", _fake_force_terminal)
    monkeypatch.setattr(agent_api, "update_run_sandbox_lease", _fake_update_lease)
    monkeypatch.setattr(agent_api.redis, "publish", _fake_publish)
    monkeypatch.setattr(agent_api.redis, "scan_keys", _fake_keys, raising=False)
    monkeypatch.setattr(agent_api, "_cleanup_redis_response_list", _fake_cleanup)

    await agent_api.stop_agent_run("run-stop")

    assert update_status_calls == []
    assert publish_calls == [
        ("agent_run:run-stop:control", "STOP"),
        ("agent_run:run-stop:control:worker-1", "STOP"),
    ]


@pytest.mark.asyncio
async def test_stop_agent_run_uses_scan_based_active_instance_lookup(monkeypatch):
    publish_calls = []
    scan_calls = []
    keys_calls = []

    class _FakeAgentDB:
        @property
        async def client(self):
            return object()

    async def _fake_update_status(_client, _run_id, _status, error=None):
        return True

    async def _fake_force_terminal(*args, **kwargs):
        return {"execution_epoch": 3}

    async def _fake_update_lease(*args, **kwargs):
        return {"ok": True}

    async def _unexpected_keys(pattern):
        keys_calls.append(pattern)
        return []

    async def _fake_scan_keys(pattern):
        scan_calls.append(pattern)
        return (
            ["active_run:worker-1:run-stop"]
            if pattern == "active_run:*:run-stop"
            else []
        )

    async def _fake_publish(channel, message):
        publish_calls.append((channel, message))
        return 1

    async def _fake_cleanup(_run_id):
        return None

    monkeypatch.setattr(agent_api, "db", _FakeAgentDB())
    monkeypatch.setattr(agent_api, "update_agent_run_status", _fake_update_status)
    monkeypatch.setattr(agent_api, "force_terminal_state", _fake_force_terminal)
    monkeypatch.setattr(agent_api, "update_run_sandbox_lease", _fake_update_lease)
    monkeypatch.setattr(agent_api.redis, "keys", _unexpected_keys)
    monkeypatch.setattr(agent_api.redis, "scan_keys", _fake_scan_keys, raising=False)
    monkeypatch.setattr(agent_api.redis, "publish", _fake_publish)
    monkeypatch.setattr(agent_api, "_cleanup_redis_response_list", _fake_cleanup)

    await agent_api.stop_agent_run("run-stop")

    assert keys_calls == []
    assert scan_calls == ["active_run:*:run-stop"]
    assert publish_calls == [
        ("agent_run:run-stop:control", "STOP"),
        ("agent_run:run-stop:control:worker-1", "STOP"),
    ]


@pytest.mark.asyncio
async def test_stop_agent_run_does_not_fetch_response_history_before_publish(
    monkeypatch,
):
    publish_calls = []
    lrange_calls = []

    class _FakeAgentDB:
        @property
        async def client(self):
            return object()

    async def _fake_lrange(*args, **kwargs):
        lrange_calls.append((args, kwargs))
        return []

    async def _fake_update_status(_client, _run_id, _status, error=None):
        return True

    async def _fake_force_terminal(*args, **kwargs):
        return {"execution_epoch": 3}

    async def _fake_update_lease(*args, **kwargs):
        return {"ok": True}

    async def _fake_publish(channel, message):
        publish_calls.append((channel, message))
        return 1

    async def _fake_keys(pattern):
        return (
            ["active_run:worker-1:run-stop"]
            if pattern == "active_run:*:run-stop"
            else []
        )

    async def _fake_cleanup(_run_id):
        return None

    monkeypatch.setattr(agent_api, "db", _FakeAgentDB())
    monkeypatch.setattr(agent_api.redis, "lrange", _fake_lrange)
    monkeypatch.setattr(agent_api, "update_agent_run_status", _fake_update_status)
    monkeypatch.setattr(agent_api, "force_terminal_state", _fake_force_terminal)
    monkeypatch.setattr(agent_api, "update_run_sandbox_lease", _fake_update_lease)
    monkeypatch.setattr(agent_api.redis, "publish", _fake_publish)
    monkeypatch.setattr(agent_api.redis, "scan_keys", _fake_keys, raising=False)
    monkeypatch.setattr(agent_api, "_cleanup_redis_response_list", _fake_cleanup)

    await agent_api.stop_agent_run("run-stop")

    assert lrange_calls == []
    assert publish_calls == [
        ("agent_run:run-stop:control", "STOP"),
        ("agent_run:run-stop:control:worker-1", "STOP"),
    ]


@pytest.mark.asyncio
async def test_stop_agent_run_releases_api_side_capacity_reservation_before_stop_publish(
    monkeypatch,
):
    operations = []
    publish_calls = []
    release_calls = []

    client = _FakeClient(
        agent_runs=[
            {
                "agent_run_id": "run-stop",
                "metadata": {
                    "request_id": "req-stop-123",
                    "client_operation_id": "op-stop-456",
                },
            }
        ],
        threads=[],
    )

    class _FakeAgentDB:
        @property
        async def client(self):
            return client

    async def _fake_update_status(_client, _run_id, _status, error=None):
        operations.append("db_status")
        return True

    async def _fake_force_terminal(*args, **kwargs):
        operations.append("force_terminal_state")
        return {"execution_epoch": 3}

    async def _fake_update_lease(*args, **kwargs):
        operations.append("update_run_sandbox_lease")
        return {"ok": True}

    async def _fake_release_capacity(*, agent_run_id: str, owner_token: str):
        operations.append("release_run_capacity_lease")
        release_calls.append((agent_run_id, owner_token))
        return {"released": True, "owner_conflict": False}

    async def _fake_publish(channel, message):
        operations.append(f"publish:{channel}")
        publish_calls.append((channel, message))
        return 1

    async def _fake_keys(pattern):
        operations.append(f"keys:{pattern}")
        return (
            ["active_run:worker-1:run-stop"]
            if pattern == "active_run:*:run-stop"
            else []
        )

    async def _fake_cleanup(_run_id):
        operations.append("cleanup_redis_response_list")
        return None

    monkeypatch.setattr(agent_api, "db", _FakeAgentDB())
    monkeypatch.setattr(agent_api, "update_agent_run_status", _fake_update_status)
    monkeypatch.setattr(agent_api, "force_terminal_state", _fake_force_terminal)
    monkeypatch.setattr(agent_api, "update_run_sandbox_lease", _fake_update_lease)
    monkeypatch.setattr(agent_api, "release_run_capacity_lease", _fake_release_capacity)
    monkeypatch.setattr(agent_api.redis, "publish", _fake_publish)
    monkeypatch.setattr(agent_api.redis, "scan_keys", _fake_keys, raising=False)
    monkeypatch.setattr(agent_api, "_cleanup_redis_response_list", _fake_cleanup)

    await agent_api.stop_agent_run("run-stop")

    assert release_calls == [("run-stop", "api-reservation:req-stop-123")]
    first_publish_index = next(
        idx for idx, op in enumerate(operations) if op.startswith("publish:")
    )
    assert operations.index("release_run_capacity_lease") < first_publish_index
    assert publish_calls == [
        ("agent_run:run-stop:control", "STOP"),
        ("agent_run:run-stop:control:worker-1", "STOP"),
    ]


@pytest.mark.asyncio
async def test_stop_agent_run_continues_when_api_side_capacity_reservation_release_fails(
    monkeypatch,
):
    publish_calls = []

    client = _FakeClient(
        agent_runs=[
            {
                "agent_run_id": "run-stop",
                "metadata": {
                    "request_id": "req-stop-123",
                },
            }
        ],
        threads=[],
    )

    class _FakeAgentDB:
        @property
        async def client(self):
            return client

    async def _fake_update_status(_client, _run_id, _status, error=None):
        return True

    async def _fake_force_terminal(*args, **kwargs):
        return {"execution_epoch": 3}

    async def _fake_update_lease(*args, **kwargs):
        return {"ok": True}

    async def _raising_release_capacity(*, agent_run_id: str, owner_token: str):
        raise RuntimeError(f"release failed for {agent_run_id} owner={owner_token}")

    async def _fake_publish(channel, message):
        publish_calls.append((channel, message))
        return 1

    async def _fake_keys(pattern):
        return (
            ["active_run:worker-1:run-stop"]
            if pattern == "active_run:*:run-stop"
            else []
        )

    async def _fake_cleanup(_run_id):
        return None

    monkeypatch.setattr(agent_api, "db", _FakeAgentDB())
    monkeypatch.setattr(agent_api, "update_agent_run_status", _fake_update_status)
    monkeypatch.setattr(agent_api, "force_terminal_state", _fake_force_terminal)
    monkeypatch.setattr(agent_api, "update_run_sandbox_lease", _fake_update_lease)
    monkeypatch.setattr(
        agent_api, "release_run_capacity_lease", _raising_release_capacity
    )
    monkeypatch.setattr(agent_api.redis, "publish", _fake_publish)
    monkeypatch.setattr(agent_api.redis, "scan_keys", _fake_keys, raising=False)
    monkeypatch.setattr(agent_api, "_cleanup_redis_response_list", _fake_cleanup)

    await agent_api.stop_agent_run("run-stop")

    assert publish_calls == [
        ("agent_run:run-stop:control", "STOP"),
        ("agent_run:run-stop:control:worker-1", "STOP"),
    ]


@pytest.mark.asyncio
async def test_stop_agent_run_avoids_response_backlog_and_raw_state_reads_on_hot_path(
    monkeypatch,
):
    publish_calls = []
    background_read_calls = []

    class _FakeAgentDB:
        @property
        async def client(self):
            return object()

    async def _unexpected_lrange(*_args, **_kwargs):
        background_read_calls.append("lrange")
        return []

    async def _fake_update_status(_client, _run_id, _status, error=None):
        return True

    async def _fake_force_terminal(*args, **kwargs):
        return {"execution_epoch": 3}

    async def _fake_update_lease(*args, **kwargs):
        return {"ok": True}

    async def _unexpected_get_state(*_args, **_kwargs):
        background_read_calls.append("get_state")
        return {"status": "cancelled", "updated_at": "2026-04-03T00:00:00Z"}

    async def _fake_publish(channel, message):
        publish_calls.append((channel, message))
        return 1

    async def _fake_keys(pattern):
        return (
            ["active_run:worker-1:run-stop"]
            if pattern == "active_run:*:run-stop"
            else []
        )

    async def _fake_cleanup(_run_id):
        return None

    monkeypatch.setattr(agent_api, "db", _FakeAgentDB())
    monkeypatch.setattr(agent_api.redis, "lrange", _unexpected_lrange)
    monkeypatch.setattr(agent_api, "update_agent_run_status", _fake_update_status)
    monkeypatch.setattr(agent_api, "force_terminal_state", _fake_force_terminal)
    monkeypatch.setattr(agent_api, "update_run_sandbox_lease", _fake_update_lease)
    monkeypatch.setattr(agent_api, "get_state", _unexpected_get_state)
    monkeypatch.setattr(agent_api.redis, "publish", _fake_publish)
    monkeypatch.setattr(agent_api.redis, "scan_keys", _fake_keys, raising=False)
    monkeypatch.setattr(agent_api, "_cleanup_redis_response_list", _fake_cleanup)

    await agent_api.stop_agent_run("run-stop")

    assert background_read_calls == []
    assert publish_calls == [
        ("agent_run:run-stop:control", "STOP"),
        ("agent_run:run-stop:control:worker-1", "STOP"),
    ]


@pytest.mark.asyncio
async def test_legacy_agent_utils_stop_agent_run_delegates_to_api_stop(monkeypatch):
    delegated_calls = []

    async def _fake_api_stop(run_id: str, error_message: str | None = None):
        delegated_calls.append((run_id, error_message))

    async def _unexpected_redis_read(*_args, **_kwargs):
        raise AssertionError(
            "legacy utils stop wrapper should not read Redis response history"
        )

    async def _unexpected_keys(*_args, **_kwargs):
        raise AssertionError(
            "legacy utils stop wrapper should not enumerate active_run keys directly"
        )

    monkeypatch.setattr(agent_api, "stop_agent_run", _fake_api_stop)
    monkeypatch.setattr(agent_utils.redis, "lrange", _unexpected_redis_read)
    monkeypatch.setattr(agent_utils.redis, "keys", _unexpected_keys, raising=False)

    await agent_utils.stop_agent_run(None, "run-legacy", error_message="stop requested")

    assert delegated_calls == [("run-legacy", "stop requested")]


@pytest.mark.asyncio
async def test_stop_agent_run_still_publishes_stop_when_db_client_unavailable(
    monkeypatch,
):
    publish_calls = []
    update_status_calls = []

    class _FailingAgentDB:
        @property
        async def client(self):
            raise RuntimeError("database unavailable")

    async def _fake_update_status(*_args, **_kwargs):
        update_status_calls.append("update_status")
        return True

    async def _fake_force_terminal(*args, **kwargs):
        return {"execution_epoch": 3}

    async def _fake_update_lease(*args, **kwargs):
        return {"ok": True}

    async def _fake_publish(channel, message):
        publish_calls.append((channel, message))
        return 1

    async def _fake_scan_keys(pattern):
        return (
            ["active_run:worker-1:run-stop"]
            if pattern == "active_run:*:run-stop"
            else []
        )

    async def _fake_cleanup(_run_id):
        return None

    monkeypatch.setattr(agent_api, "db", _FailingAgentDB())
    monkeypatch.setattr(agent_api, "update_agent_run_status", _fake_update_status)
    monkeypatch.setattr(agent_api, "force_terminal_state", _fake_force_terminal)
    monkeypatch.setattr(agent_api, "update_run_sandbox_lease", _fake_update_lease)
    monkeypatch.setattr(agent_api.redis, "publish", _fake_publish)
    monkeypatch.setattr(agent_api.redis, "scan_keys", _fake_scan_keys, raising=False)
    monkeypatch.setattr(agent_api, "_cleanup_redis_response_list", _fake_cleanup)

    await agent_api.stop_agent_run("run-stop")

    assert update_status_calls == []
    assert publish_calls == [
        ("agent_run:run-stop:control", "STOP"),
        ("agent_run:run-stop:control:worker-1", "STOP"),
    ]


@pytest.mark.asyncio
async def test_cleanup_uses_scan_keys_for_instance_active_runs(monkeypatch):
    stop_calls = []
    keys_calls = []
    scan_calls = []
    close_calls = []

    async def _fake_stop(agent_run_id: str, error_message: str | None = None):
        stop_calls.append((agent_run_id, error_message))

    async def _unexpected_keys(pattern: str):
        keys_calls.append(pattern)
        return []

    async def _fake_scan_keys(pattern: str):
        scan_calls.append(pattern)
        return [
            "active_run:instance-a:run-1",
            "active_run:instance-a:run-2",
        ]

    async def _fake_close():
        close_calls.append("close")

    monkeypatch.setattr(agent_api, "instance_id", "instance-a")
    monkeypatch.setattr(agent_api, "stop_agent_run", _fake_stop)
    monkeypatch.setattr(agent_api.redis, "keys", _unexpected_keys)
    monkeypatch.setattr(agent_api.redis, "scan_keys", _fake_scan_keys, raising=False)
    monkeypatch.setattr(agent_api.redis, "close", _fake_close)

    await agent_api.cleanup()

    assert keys_calls == []
    assert scan_calls == ["active_run:instance-a:*"]
    assert stop_calls == [
        ("run-1", "Instance instance-a shutting down"),
        ("run-2", "Instance instance-a shutting down"),
    ]
    assert close_calls == ["close"]


@pytest.mark.asyncio
async def test_enforce_single_active_qwen_run_uses_scan_keys_while_waiting(monkeypatch):
    stop_calls = []
    keys_calls = []
    scan_calls = []
    sleep_calls = []

    class _SequenceAgentRunsQuery:
        def __init__(self):
            self._execute_count = 0

        def select(self, *_args, **_kwargs):
            return self

        def eq(self, *_args, **_kwargs):
            return self

        def order(self, *_args, **_kwargs):
            return self

        async def execute(self):
            self._execute_count += 1
            if self._execute_count == 1:
                return _FakeResult(
                    data=[
                        {
                            "agent_run_id": "run-1",
                            "status": "running",
                        }
                    ]
                )
            return _FakeResult(data=[])

    class _SequenceClient:
        def __init__(self):
            self._agent_runs_query = _SequenceAgentRunsQuery()

        def table(self, table_name: str):
            assert table_name == "agent_runs"
            return self._agent_runs_query

    async def _fake_stop(agent_run_id: str, error_message: str | None = None):
        stop_calls.append((agent_run_id, error_message))

    async def _unexpected_keys(pattern: str):
        keys_calls.append(pattern)
        return []

    async def _fake_scan_keys(pattern: str):
        scan_calls.append(pattern)
        return []

    async def _fake_sleep(seconds: float):
        sleep_calls.append(seconds)

    monkeypatch.setattr(agent_api, "stop_agent_run", _fake_stop)
    monkeypatch.setattr(agent_api.redis, "keys", _unexpected_keys)
    monkeypatch.setattr(agent_api.redis, "scan_keys", _fake_scan_keys, raising=False)
    monkeypatch.setattr(agent_api.asyncio, "sleep", _fake_sleep)

    await agent_api._enforce_single_active_qwen_run(_SequenceClient(), "thread-1")

    assert stop_calls == [("run-1", None)]
    assert keys_calls == []
    assert scan_calls == ["active_run:*:run-1"]
    assert sleep_calls == []


@pytest.mark.asyncio
async def test_get_agent_run_includes_shadow_clone_file_delivery_source(monkeypatch):
    class _FakeAgentDB:
        @property
        async def client(self):
            return object()

    async def _run_with_access_check(_client, _run_id: str, _user_id: str):
        return {
            "id": "run-1",
            "thread_id": "thread-1",
            "status": "completed",
            "started_at": "2026-04-02T00:00:00Z",
            "completed_at": "2026-04-02T00:10:00Z",
            "error": None,
        }

    async def _lease(_run_id: str):
        return {
            "run_id": "run-1",
            "project_id": "proj-1",
            "thread_id": "thread-1",
            "sandbox_id": "sbx-run-1",
            "binding_state": "released",
            "environment_status": "completed",
            "environment_ready": True,
            "execution_epoch": 3,
            "environment_manifest": {"sandbox": {"id": "sbx-run-1"}},
        }

    monkeypatch.setattr(agent_api, "db", _FakeAgentDB())
    monkeypatch.setattr(
        agent_api, "get_agent_run_with_access_check", _run_with_access_check
    )
    monkeypatch.setattr(agent_api, "get_run_sandbox_lease", _lease, raising=False)

    payload = await agent_api.get_agent_run("run-1", "user-1")

    assert payload["file_delivery_source"] == _expected_file_delivery_source(
        agent_run_id="run-1",
        browse_sandbox_id="sbx-run-1",
        archive_sandbox_id="sbx-run-1",
        identity_source="shadow_clone_lease",
        binding_state="released",
        environment_status="completed",
        environment_ready=True,
        execution_epoch=3,
        manifest_sandbox_id="sbx-run-1",
        terminal_status="completed",
    )


@pytest.mark.asyncio
async def test_get_agent_run_degrades_when_lease_lookup_fails(monkeypatch):
    class _FakeAgentDB:
        @property
        async def client(self):
            return object()

    async def _run_with_access_check(_client, _run_id: str, _user_id: str):
        return {
            "id": "run-lease-failure",
            "thread_id": "thread-lease-failure",
            "status": "running",
            "started_at": "2026-04-02T00:00:00Z",
            "completed_at": None,
            "error": None,
        }

    async def _lease_failure(_run_id: str):
        raise RuntimeError("redis unavailable")

    warnings = []

    def _warning(message: str, *args):
        warnings.append(message % args if args else message)

    monkeypatch.setattr(agent_api, "db", _FakeAgentDB())
    monkeypatch.setattr(
        agent_api, "get_agent_run_with_access_check", _run_with_access_check
    )
    monkeypatch.setattr(
        agent_api, "get_run_sandbox_lease", _lease_failure, raising=False
    )
    monkeypatch.setattr(agent_api.logger, "warning", _warning)

    payload = await agent_api.get_agent_run("run-lease-failure", "user-1")

    assert payload["file_delivery_source"] == _expected_file_delivery_source(
        agent_run_id="run-lease-failure",
        terminal_status=None,
    )
    assert warnings == [
        "Failed to load sandbox lease for agent run file delivery source: run-lease-failure"
    ]


@pytest.mark.asyncio
async def test_get_agent_run_file_delivery_source_falls_back_to_state_when_lease_is_partial(
    monkeypatch,
):
    class _FakeAgentDB:
        @property
        async def client(self):
            return object()

    async def _run_with_access_check(_client, _run_id: str, _user_id: str):
        return {
            "id": "run-partial-lease",
            "thread_id": "thread-partial-lease",
            "status": "stopped",
            "started_at": "2026-04-02T00:00:00Z",
            "completed_at": "2026-04-02T00:10:00Z",
            "error": None,
        }

    async def _lease(_run_id: str):
        return {
            "run_id": "run-partial-lease",
            "project_id": "proj-partial-lease",
            "thread_id": "thread-partial-lease",
            "binding_state": "released",
            "environment_status": "cancelled",
            "environment_ready": False,
            "execution_epoch": 5,
        }

    async def _state(_run_id: str):
        return {
            "status": "cancelled",
            "execution_epoch": 5,
            "sandbox": {
                "id": "sbx-from-state",
                "binding_state": "released",
                "environment_status": "cancelled",
                "environment_ready": False,
            },
            "environment": {
                "status": "cancelled",
                "ready": False,
                "manifest": {"sandbox": {"id": "sbx-from-state"}},
            },
        }

    monkeypatch.setattr(agent_api, "db", _FakeAgentDB())
    monkeypatch.setattr(
        agent_api, "get_agent_run_with_access_check", _run_with_access_check
    )
    monkeypatch.setattr(agent_api, "get_run_sandbox_lease", _lease, raising=False)
    monkeypatch.setattr(agent_api, "get_state", _state)

    payload = await agent_api.get_agent_run("run-partial-lease", "user-1")

    assert payload["file_delivery_source"] == _expected_file_delivery_source(
        agent_run_id="run-partial-lease",
        browse_sandbox_id="sbx-from-state",
        archive_sandbox_id="sbx-from-state",
        identity_source="shadow_clone_state",
        binding_state="released",
        environment_status="cancelled",
        environment_ready=False,
        execution_epoch=5,
        manifest_sandbox_id="sbx-from-state",
        terminal_status="cancelled",
    )


@pytest.mark.asyncio
async def test_get_agent_run_projects_terminal_status_from_response_tail(monkeypatch):
    class _FakeAgentDB:
        @property
        async def client(self):
            return object()

    async def _run_with_access_check(_client, _run_id: str, _user_id: str):
        return {
            "id": "run-single-projected",
            "thread_id": "thread-single-projected",
            "status": "running",
            "started_at": "2026-04-02T00:00:00Z",
            "completed_at": None,
            "updated_at": "2026-04-02T00:09:00Z",
            "error": None,
        }

    async def _lease(_run_id: str):
        return {
            "run_id": "run-single-projected",
            "project_id": "proj-single",
            "thread_id": "thread-single-projected",
            "sandbox_id": "sbx-run-single-projected",
            "binding_state": "released",
            "environment_status": "ready",
            "environment_ready": False,
            "execution_epoch": 4,
            "environment_manifest": {"sandbox": {"id": "sbx-run-single-projected"}},
        }

    async def _stream_terminal(_run_id: str, **_kwargs):
        return "completed", None

    monkeypatch.setattr(agent_api, "db", _FakeAgentDB())
    monkeypatch.setattr(
        agent_api, "get_agent_run_with_access_check", _run_with_access_check
    )
    monkeypatch.setattr(agent_api, "get_run_sandbox_lease", _lease, raising=False)
    monkeypatch.setattr(
        agent_api,
        "read_terminal_status_from_response_tail",
        _stream_terminal,
    )

    payload = await agent_api.get_agent_run("run-single-projected", "user-1")

    assert payload["status"] == "completed"
    assert payload["completedAt"] == "2026-04-02T00:09:00Z"
    assert payload["file_delivery_source"] == _expected_file_delivery_source(
        agent_run_id="run-single-projected",
        browse_sandbox_id="sbx-run-single-projected",
        archive_sandbox_id="sbx-run-single-projected",
        identity_source="shadow_clone_lease",
        binding_state="released",
        environment_status="ready",
        environment_ready=False,
        execution_epoch=4,
        manifest_sandbox_id="sbx-run-single-projected",
        terminal_status="completed",
    )


@pytest.mark.asyncio
async def test_get_agent_runs_includes_file_delivery_source(monkeypatch):
    class _FakeAgentDB:
        @property
        async def client(self):
            return _FakeClient(
                agent_runs=[
                    {
                        "id": "db-id-1",
                        "agent_run_id": "run-1",
                        "thread_id": "thread-1",
                        "status": "running",
                        "started_at": "2026-04-02T00:00:00Z",
                        "completed_at": None,
                        "error": None,
                        "created_at": "2026-04-02T00:00:00Z",
                        "updated_at": "2026-04-02T00:00:05Z",
                    }
                ],
                threads=[],
            )

    async def _verify_thread_access(_client, _thread_id: str, _user_id: str):
        return None

    async def _lease(run_id: str):
        return {
            "run_id": run_id,
            "project_id": "proj-1",
            "thread_id": "thread-1",
            "sandbox_id": f"sbx-{run_id}",
            "binding_state": "locked",
            "environment_status": "running",
            "environment_ready": True,
            "execution_epoch": 2,
            "environment_manifest": {"sandbox": {"id": f"sbx-{run_id}"}},
        }

    monkeypatch.setattr(agent_api, "db", _FakeAgentDB())
    monkeypatch.setattr(agent_api, "verify_thread_access", _verify_thread_access)
    monkeypatch.setattr(agent_api, "get_run_sandbox_lease", _lease, raising=False)

    payload = await agent_api.get_agent_runs("thread-1", "user-1")

    assert payload["agent_runs"][0]["id"] == "run-1"
    assert payload["agent_runs"][0][
        "file_delivery_source"
    ] == _expected_file_delivery_source(
        agent_run_id="run-1",
        browse_sandbox_id="sbx-run-1",
        archive_sandbox_id="sbx-run-1",
        identity_source="shadow_clone_lease",
        binding_state="locked",
        environment_status="running",
        environment_ready=True,
        execution_epoch=2,
        manifest_sandbox_id="sbx-run-1",
        terminal_status=None,
    )


@pytest.mark.asyncio
async def test_get_agent_runs_projects_terminal_status_from_response_tail(
    monkeypatch,
):
    class _FakeAgentDB:
        @property
        async def client(self):
            return _FakeClient(
                agent_runs=[
                    {
                        "id": "db-id-projected",
                        "agent_run_id": "run-projected-complete",
                        "thread_id": "thread-projected",
                        "status": "running",
                        "started_at": "2026-04-02T00:00:00Z",
                        "completed_at": None,
                        "error": None,
                        "created_at": "2026-04-02T00:00:00Z",
                        "updated_at": "2026-04-02T00:05:00Z",
                    }
                ],
                threads=[],
            )

    async def _verify_thread_access(_client, _thread_id: str, _user_id: str):
        return None

    async def _lease(run_id: str):
        return {
            "run_id": run_id,
            "project_id": "proj-projected",
            "thread_id": "thread-projected",
            "sandbox_id": f"sbx-{run_id}",
            "binding_state": "released",
            "environment_status": "ready",
            "environment_ready": False,
            "execution_epoch": 9,
            "environment_manifest": {"sandbox": {"id": f"sbx-{run_id}"}},
        }

    async def _projected_terminal(_run_id: str, **_kwargs):
        return "completed", None

    monkeypatch.setattr(agent_api, "db", _FakeAgentDB())
    monkeypatch.setattr(agent_api, "verify_thread_access", _verify_thread_access)
    monkeypatch.setattr(agent_api, "get_run_sandbox_lease", _lease, raising=False)
    monkeypatch.setattr(
        agent_api,
        "read_terminal_status_from_response_tail",
        _projected_terminal,
    )

    payload = await agent_api.get_agent_runs("thread-projected", "user-1")

    assert payload["agent_runs"][0]["status"] == "completed"
    assert payload["agent_runs"][0][
        "file_delivery_source"
    ] == _expected_file_delivery_source(
        agent_run_id="run-projected-complete",
        browse_sandbox_id="sbx-run-projected-complete",
        archive_sandbox_id="sbx-run-projected-complete",
        identity_source="shadow_clone_lease",
        binding_state="released",
        environment_status="ready",
        environment_ready=False,
        execution_epoch=9,
        manifest_sandbox_id="sbx-run-projected-complete",
        terminal_status="completed",
    )


@pytest.mark.asyncio
async def test_get_agent_runs_phase2_projection_uses_metadata_selected_from_query(
    monkeypatch,
):
    projection_calls: list[int | None] = []

    class _FakeAgentDB:
        @property
        async def client(self):
            return _SelectAwareClient(
                {
                    "agent_runs": [
                        {
                            "id": "db-id-phase2",
                            "agent_run_id": "run-phase2-list",
                            "thread_id": "thread-phase2",
                            "status": "running",
                            "started_at": "2026-04-02T00:00:00Z",
                            "completed_at": None,
                            "error": None,
                            "created_at": "2026-04-02T00:00:00Z",
                            "updated_at": "2026-04-02T00:05:00Z",
                            "metadata": {
                                "regular_execution_mode": "phase2_supervisor",
                                "shadow_clone_mode": "off",
                            },
                        }
                    ],
                    "threads": [
                        {
                            "thread_id": "thread-phase2",
                            "account_id": "user-1",
                        }
                    ],
                    "regular_run_attempts": [
                        {
                            "attempt_id": "attempt-1",
                            "agent_run_id": "run-phase2-list",
                            "attempt_number": 1,
                            "execution_epoch": 1,
                            "status": "abandoned",
                        },
                        {
                            "attempt_id": "attempt-2",
                            "agent_run_id": "run-phase2-list",
                            "attempt_number": 2,
                            "execution_epoch": 2,
                            "status": "running",
                        },
                    ],
                }
            )

    async def _verify_thread_access(_client, _thread_id: str, _user_id: str):
        return None

    async def _lease(_run_id: str):
        return None

    async def _projected_terminal(_run_id: str, **kwargs):
        projection_calls.append(kwargs.get("current_execution_epoch"))
        return "completed", None

    monkeypatch.setattr(agent_api, "db", _FakeAgentDB())
    monkeypatch.setattr(agent_api, "verify_thread_access", _verify_thread_access)
    monkeypatch.setattr(agent_api, "get_run_sandbox_lease", _lease, raising=False)
    monkeypatch.setattr(
        agent_api,
        "read_terminal_status_from_response_tail",
        _projected_terminal,
    )

    payload = await agent_api.get_agent_runs("thread-phase2", "user-1")

    assert projection_calls == [2]
    assert payload["agent_runs"][0]["status"] == "completed"


@pytest.mark.asyncio
async def test_get_agent_runs_preserves_file_delivery_source_for_stopped_run(
    monkeypatch,
):
    class _FakeAgentDB:
        @property
        async def client(self):
            return _FakeClient(
                agent_runs=[
                    {
                        "id": "db-id-stop",
                        "agent_run_id": "run-stop-files",
                        "thread_id": "thread-stop-files",
                        "status": "stopped",
                        "started_at": "2026-04-02T00:00:00Z",
                        "completed_at": "2026-04-02T00:05:00Z",
                        "error": None,
                        "created_at": "2026-04-02T00:00:00Z",
                        "updated_at": "2026-04-02T00:05:00Z",
                    }
                ],
                threads=[],
            )

    async def _verify_thread_access(_client, _thread_id: str, _user_id: str):
        return None

    async def _lease(run_id: str):
        return {
            "run_id": run_id,
            "project_id": "proj-stop-files",
            "thread_id": "thread-stop-files",
            "sandbox_id": f"sbx-{run_id}",
            "binding_state": "released",
            "environment_status": "cancelled",
            "environment_ready": False,
            "execution_epoch": 6,
            "environment_manifest": {"sandbox": {"id": f"sbx-{run_id}"}},
        }

    monkeypatch.setattr(agent_api, "db", _FakeAgentDB())
    monkeypatch.setattr(agent_api, "verify_thread_access", _verify_thread_access)
    monkeypatch.setattr(agent_api, "get_run_sandbox_lease", _lease, raising=False)

    payload = await agent_api.get_agent_runs("thread-stop-files", "user-1")

    assert payload["agent_runs"][0]["id"] == "run-stop-files"
    assert payload["agent_runs"][0]["status"] == "stopped"
    assert payload["agent_runs"][0][
        "file_delivery_source"
    ] == _expected_file_delivery_source(
        agent_run_id="run-stop-files",
        browse_sandbox_id="sbx-run-stop-files",
        archive_sandbox_id="sbx-run-stop-files",
        identity_source="shadow_clone_lease",
        binding_state="released",
        environment_status="cancelled",
        environment_ready=False,
        execution_epoch=6,
        manifest_sandbox_id="sbx-run-stop-files",
        terminal_status="cancelled",
    )


@pytest.mark.asyncio
async def test_get_agent_runs_preserves_file_delivery_source_for_stopped_run_without_lease(
    monkeypatch,
):
    class _FakeAgentDB:
        @property
        async def client(self):
            return _FakeClient(
                agent_runs=[
                    {
                        "id": "db-id-stop-state",
                        "agent_run_id": "run-stop-state-files",
                        "thread_id": "thread-stop-state-files",
                        "status": "stopped",
                        "started_at": "2026-04-02T00:00:00Z",
                        "completed_at": "2026-04-02T00:05:00Z",
                        "error": None,
                        "created_at": "2026-04-02T00:00:00Z",
                        "updated_at": "2026-04-02T00:05:00Z",
                    }
                ],
                threads=[],
            )

    async def _verify_thread_access(_client, _thread_id: str, _user_id: str):
        return None

    async def _lease(_run_id: str):
        return None

    async def _state(_run_id: str):
        return {
            "status": "cancelled",
            "execution_epoch": 8,
            "environment": {
                "status": "cancelled",
                "ready": False,
                "manifest": {"sandbox": {"id": "sbx-run-stop-state-files"}},
            },
            "sandbox": {
                "id": "sbx-run-stop-state-files",
                "binding_state": "released",
                "environment_status": "cancelled",
                "environment_ready": False,
            },
        }

    monkeypatch.setattr(agent_api, "db", _FakeAgentDB())
    monkeypatch.setattr(agent_api, "verify_thread_access", _verify_thread_access)
    monkeypatch.setattr(agent_api, "get_run_sandbox_lease", _lease, raising=False)
    monkeypatch.setattr(agent_api, "get_state", _state)

    payload = await agent_api.get_agent_runs("thread-stop-state-files", "user-1")

    assert payload["agent_runs"][0]["id"] == "run-stop-state-files"
    assert payload["agent_runs"][0]["status"] == "stopped"
    assert payload["agent_runs"][0][
        "file_delivery_source"
    ] == _expected_file_delivery_source(
        agent_run_id="run-stop-state-files",
        browse_sandbox_id="sbx-run-stop-state-files",
        archive_sandbox_id="sbx-run-stop-state-files",
        identity_source="shadow_clone_state",
        binding_state="released",
        environment_status="cancelled",
        environment_ready=False,
        execution_epoch=8,
        manifest_sandbox_id="sbx-run-stop-state-files",
        terminal_status="cancelled",
    )


@pytest.mark.asyncio
async def test_get_agent_runs_file_delivery_source_falls_back_to_state_when_lease_is_partial(
    monkeypatch,
):
    class _FakeAgentDB:
        @property
        async def client(self):
            return _FakeClient(
                agent_runs=[
                    {
                        "id": "db-id-partial-lease",
                        "agent_run_id": "run-partial-lease-list",
                        "thread_id": "thread-partial-lease-list",
                        "status": "stopped",
                        "started_at": "2026-04-02T00:00:00Z",
                        "completed_at": "2026-04-02T00:05:00Z",
                        "error": None,
                        "created_at": "2026-04-02T00:00:00Z",
                        "updated_at": "2026-04-02T00:05:00Z",
                    }
                ],
                threads=[],
            )

    async def _verify_thread_access(_client, _thread_id: str, _user_id: str):
        return None

    async def _lease(_run_id: str):
        return {
            "binding_state": "released",
            "environment_status": "cancelled",
            "environment_ready": False,
            "execution_epoch": 7,
        }

    async def _state(_run_id: str):
        return {
            "status": "cancelled",
            "execution_epoch": 7,
            "sandbox": {
                "id": "sbx-list-state",
                "binding_state": "released",
                "environment_status": "cancelled",
                "environment_ready": False,
            },
            "environment": {
                "status": "cancelled",
                "ready": False,
                "manifest": {"sandbox": {"id": "sbx-list-state"}},
            },
        }

    monkeypatch.setattr(agent_api, "db", _FakeAgentDB())
    monkeypatch.setattr(agent_api, "verify_thread_access", _verify_thread_access)
    monkeypatch.setattr(agent_api, "get_run_sandbox_lease", _lease, raising=False)
    monkeypatch.setattr(agent_api, "get_state", _state)

    payload = await agent_api.get_agent_runs("thread-partial-lease-list", "user-1")

    assert payload["agent_runs"][0][
        "file_delivery_source"
    ] == _expected_file_delivery_source(
        agent_run_id="run-partial-lease-list",
        browse_sandbox_id="sbx-list-state",
        archive_sandbox_id="sbx-list-state",
        identity_source="shadow_clone_state",
        binding_state="released",
        environment_status="cancelled",
        environment_ready=False,
        execution_epoch=7,
        manifest_sandbox_id="sbx-list-state",
        terminal_status="cancelled",
    )


@pytest.mark.asyncio
async def test_confirm_shadow_clone_requires_confirming_state(monkeypatch):
    _install_fake_db(
        monkeypatch,
        agent_runs=[{"agent_run_id": "run-2", "thread_id": "thread-2"}],
        threads=[{"thread_id": "thread-2", "account_id": "user-1"}],
    )

    async def _state(_run_id: str):
        return {"status": "running"}

    monkeypatch.setattr(shadow_clone_routes, "get_state", _state)

    with pytest.raises(HTTPException) as exc_info:
        await shadow_clone_routes.confirm_shadow_clone(
            agent_run_id="run-2",
            user_id="user-1",
        )

    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_confirm_shadow_clone_requires_ready_environment(monkeypatch):
    _install_fake_db(
        monkeypatch,
        agent_runs=[{"agent_run_id": "run-2b", "thread_id": "thread-2b"}],
        threads=[{"thread_id": "thread-2b", "account_id": "user-1"}],
    )

    async def _state(_run_id: str):
        return {
            "status": "confirming",
            "environment": {"ready": False, "status": "preparing"},
        }

    monkeypatch.setattr(shadow_clone_routes, "get_state", _state)

    with pytest.raises(HTTPException) as exc_info:
        await shadow_clone_routes.confirm_shadow_clone(
            agent_run_id="run-2b",
            user_id="user-1",
        )

    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_confirm_shadow_clone_rejects_non_attachable_lease(monkeypatch):
    _install_fake_db(
        monkeypatch,
        agent_runs=[{"agent_run_id": "run-lease", "thread_id": "thread-lease"}],
        threads=[{"thread_id": "thread-lease", "account_id": "user-1"}],
    )

    async def _state(_run_id: str):
        return {"status": "confirming", "environment": {"ready": True}}

    async def _lease(_run_id: str):
        return {
            "binding_state": "recovery_required",
            "environment_ready": False,
        }

    monkeypatch.setattr(shadow_clone_routes, "get_state", _state)
    monkeypatch.setattr(shadow_clone_routes, "get_run_sandbox_lease", _lease)

    with pytest.raises(HTTPException) as exc_info:
        await shadow_clone_routes.confirm_shadow_clone(
            agent_run_id="run-lease",
            user_id="user-1",
        )

    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_confirm_shadow_clone_forbidden_when_not_owner(monkeypatch):
    _install_fake_db(
        monkeypatch,
        agent_runs=[{"agent_run_id": "run-3", "thread_id": "thread-3"}],
        threads=[{"thread_id": "thread-3", "account_id": "owner-1"}],
    )

    async def _deny_access(_client, _thread_id: str, _user_id: str):
        raise HTTPException(status_code=403, detail="forbidden")

    monkeypatch.setattr(shadow_clone_routes, "verify_thread_access", _deny_access)

    with pytest.raises(HTTPException) as exc_info:
        await shadow_clone_routes.confirm_shadow_clone(
            agent_run_id="run-3",
            user_id="user-2",
        )

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_deny_shadow_clone_success(monkeypatch):
    _install_fake_db(
        monkeypatch,
        agent_runs=[{"agent_run_id": "run-4", "thread_id": "thread-4"}],
        threads=[{"thread_id": "thread-4", "account_id": "user-4"}],
    )

    async def _state(_run_id: str):
        return {"status": "confirming", "environment": {"ready": False}}

    published = []

    async def _publish(channel: str, message: str):
        published.append((channel, message))
        return 1

    monkeypatch.setattr(shadow_clone_routes, "get_state", _state)
    monkeypatch.setattr(
        shadow_clone_routes,
        "get_confirmation_result",
        lambda _run_id: _stateful_none(),
    )
    monkeypatch.setattr(
        shadow_clone_routes,
        "record_confirmation_result",
        lambda _run_id, _result: _recorded_deny(),
    )
    monkeypatch.setattr(shadow_clone_routes.redis_service, "publish", _publish)

    result = await shadow_clone_routes.deny_shadow_clone(
        agent_run_id="run-4",
        body=shadow_clone_routes.DenyRequest(reason="nope"),
        user_id="user-4",
    )

    assert result["status"] == "denied"
    assert result["reason"] == "nope"
    assert published == [("agent_run:run-4:shadow_clone_confirm", "DENIED")]


@pytest.mark.asyncio
async def test_confirm_shadow_clone_is_idempotent_for_existing_confirm(monkeypatch):
    _install_fake_db(
        monkeypatch,
        agent_runs=[{"agent_run_id": "run-confirmed", "thread_id": "thread-confirmed"}],
        threads=[{"thread_id": "thread-confirmed", "account_id": "user-1"}],
    )

    publish_calls = []

    async def _publish(channel: str, message: str):
        publish_calls.append((channel, message))
        return 1

    monkeypatch.setattr(
        shadow_clone_routes,
        "get_confirmation_result",
        lambda _run_id: asyncio.sleep(
            0,
            result=shadow_clone_routes.ConfirmationResult.CONFIRMED,
        ),
    )
    monkeypatch.setattr(shadow_clone_routes.redis_service, "publish", _publish)

    result = await shadow_clone_routes.confirm_shadow_clone(
        agent_run_id="run-confirmed",
        user_id="user-1",
    )

    assert result == {"status": "confirmed"}
    assert publish_calls == [
        ("agent_run:run-confirmed:shadow_clone_confirm", "CONFIRMED")
    ]


@pytest.mark.asyncio
async def test_confirm_shadow_clone_rejects_existing_denial(monkeypatch):
    _install_fake_db(
        monkeypatch,
        agent_runs=[{"agent_run_id": "run-denied", "thread_id": "thread-denied"}],
        threads=[{"thread_id": "thread-denied", "account_id": "user-1"}],
    )

    monkeypatch.setattr(
        shadow_clone_routes,
        "get_confirmation_result",
        lambda _run_id: asyncio.sleep(
            0,
            result=shadow_clone_routes.ConfirmationResult.DENIED,
        ),
    )

    with pytest.raises(HTTPException) as exc_info:
        await shadow_clone_routes.confirm_shadow_clone(
            agent_run_id="run-denied",
            user_id="user-1",
        )

    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_confirm_shadow_clone_rejects_conflicting_race(monkeypatch):
    _install_fake_db(
        monkeypatch,
        agent_runs=[{"agent_run_id": "run-race", "thread_id": "thread-race"}],
        threads=[{"thread_id": "thread-race", "account_id": "user-1"}],
    )

    async def _state(_run_id: str):
        return {"status": "confirming", "environment": {"ready": True}}

    async def _record_conflict(_run_id: str, _result):
        return (
            shadow_clone_routes.ConfirmationDecisionWriteStatus.CONFLICT,
            shadow_clone_routes.ConfirmationResult.DENIED,
        )

    monkeypatch.setattr(shadow_clone_routes, "get_state", _state)
    monkeypatch.setattr(
        shadow_clone_routes,
        "get_confirmation_result",
        lambda _run_id: _stateful_none(),
    )
    monkeypatch.setattr(
        shadow_clone_routes,
        "record_confirmation_result",
        _record_conflict,
    )

    with pytest.raises(HTTPException) as exc_info:
        await shadow_clone_routes.confirm_shadow_clone(
            agent_run_id="run-race",
            user_id="user-1",
        )

    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_deny_shadow_clone_is_idempotent_for_existing_denial(monkeypatch):
    _install_fake_db(
        monkeypatch,
        agent_runs=[
            {"agent_run_id": "run-deny-existing", "thread_id": "thread-deny-existing"}
        ],
        threads=[{"thread_id": "thread-deny-existing", "account_id": "user-4"}],
    )

    publish_calls = []

    async def _publish(channel: str, message: str):
        publish_calls.append((channel, message))
        return 1

    monkeypatch.setattr(
        shadow_clone_routes,
        "get_confirmation_result",
        lambda _run_id: asyncio.sleep(
            0,
            result=shadow_clone_routes.ConfirmationResult.DENIED,
        ),
    )
    monkeypatch.setattr(shadow_clone_routes.redis_service, "publish", _publish)

    result = await shadow_clone_routes.deny_shadow_clone(
        agent_run_id="run-deny-existing",
        body=shadow_clone_routes.DenyRequest(reason="still no"),
        user_id="user-4",
    )

    assert result == {"status": "denied", "reason": "still no"}
    assert publish_calls == [
        ("agent_run:run-deny-existing:shadow_clone_confirm", "DENIED")
    ]


@pytest.mark.asyncio
async def test_get_shadow_clone_status_returns_state(monkeypatch):
    _install_fake_db(
        monkeypatch,
        agent_runs=[{"agent_run_id": "run-5", "thread_id": "thread-5"}],
        threads=[{"thread_id": "thread-5", "account_id": "user-5"}],
    )

    expected_state = {
        "status": "running",
        "total": 2,
        "completed": 1,
        "failed": 0,
        "running": 1,
        "live_activity": {
            "scope": "main_agent",
            "phase": "aggregate",
            "reason": "aggregate_started",
            "subtask_id": None,
            "epoch": 0,
            "updated_at": "2026-03-26T00:00:00+00:00",
            "private_field": "ignored",
        },
    }

    async def _state(_run_id: str):
        return expected_state

    monkeypatch.setattr(shadow_clone_routes, "get_state", _state)

    result = await shadow_clone_routes.get_shadow_clone_status(
        agent_run_id="run-5",
        user_id="user-5",
    )

    assert result == {
        "status": "running",
        "total": 2,
        "completed": 1,
        "failed": 0,
        "running": 1,
        "activity_owner": "main_agent",
        "ui_phase": "aggregating",
        "phase_reason": "aggregate_started",
        "live_activity": {
            "scope": "main_agent",
            "phase": "aggregate",
            "reason": "aggregate_started",
            "subtask_id": None,
            "epoch": 0,
            "updated_at": "2026-03-26T00:00:00+00:00",
        },
        "file_delivery_source": _expected_file_delivery_source(agent_run_id="run-5"),
    }


@pytest.mark.asyncio
async def test_get_shadow_clone_status_reuses_cached_public_state_until_updated(
    monkeypatch,
):
    _install_fake_db(
        monkeypatch,
        agent_runs=[{"agent_run_id": "run-cache", "thread_id": "thread-cache"}],
        threads=[{"thread_id": "thread-cache", "account_id": "user-cache"}],
    )
    shadow_clone_routes._PUBLIC_STATE_CACHE.clear()

    state_payload = {
        "status": "running",
        "updated_at": "2026-03-30T00:00:00+00:00",
        "subagents": {
            "task-1": {
                "status": "running",
                "role": "researcher",
                "owner_token": "secret",
            }
        },
    }
    sanitize_calls = {"count": 0}

    async def _state(_run_id: str):
        return state_payload

    def _sanitize_uncached(payload):
        sanitize_calls["count"] += 1
        return {"status": payload["status"], "updated_at": payload["updated_at"]}

    monkeypatch.setattr(shadow_clone_routes, "get_state", _state)
    monkeypatch.setattr(
        shadow_clone_routes,
        "_sanitize_public_shadow_clone_state_uncached",
        _sanitize_uncached,
    )

    first = await shadow_clone_routes.get_shadow_clone_status(
        agent_run_id="run-cache",
        user_id="user-cache",
    )
    second = await shadow_clone_routes.get_shadow_clone_status(
        agent_run_id="run-cache",
        user_id="user-cache",
    )

    assert first == {
        "status": "running",
        "updated_at": "2026-03-30T00:00:00+00:00",
        "activity_owner": "none",
        "file_delivery_source": _expected_file_delivery_source(
            agent_run_id="run-cache"
        ),
    }
    assert second == first
    assert sanitize_calls["count"] == 1

    state_payload["updated_at"] = "2026-03-30T00:00:01+00:00"
    third = await shadow_clone_routes.get_shadow_clone_status(
        agent_run_id="run-cache",
        user_id="user-cache",
    )

    assert third == {
        "status": "running",
        "updated_at": "2026-03-30T00:00:01+00:00",
        "activity_owner": "none",
        "file_delivery_source": _expected_file_delivery_source(
            agent_run_id="run-cache"
        ),
    }
    assert sanitize_calls["count"] == 2


@pytest.mark.asyncio
async def test_get_shadow_clone_status_redacts_internal_subagent_owner_token(
    monkeypatch,
):
    _install_fake_db(
        monkeypatch,
        agent_runs=[{"agent_run_id": "run-5-owner", "thread_id": "thread-5-owner"}],
        threads=[{"thread_id": "thread-5-owner", "account_id": "user-5"}],
    )

    async def _state(_run_id: str):
        return {
            "status": "running",
            "subagents": {
                "task-1": {
                    "status": "running",
                    "role": "researcher",
                    "owner_token": "secret-claim-token",
                }
            },
        }

    monkeypatch.setattr(shadow_clone_routes, "get_state", _state)

    result = await shadow_clone_routes.get_shadow_clone_status(
        agent_run_id="run-5-owner",
        user_id="user-5",
    )

    assert result["subagents"]["task-1"]["status"] == "running"
    assert result["subagents"]["task-1"]["role"] == "researcher"
    assert "owner_token" not in result["subagents"]["task-1"]


@pytest.mark.asyncio
async def test_get_shadow_clone_status_redacts_internal_control_plane_fields(
    monkeypatch,
):
    _install_fake_db(
        monkeypatch,
        agent_runs=[
            {"agent_run_id": "run-5-internal", "thread_id": "thread-5-internal"}
        ],
        threads=[{"thread_id": "thread-5-internal", "account_id": "user-5"}],
    )

    async def _state(_run_id: str):
        return {
            "status": "running",
            "execution_epoch": 3,
            "last_completed_layer": 1,
            "recovery": {
                "pending": True,
                "kind": "clone",
                "source": "shadow_clone_coordinator",
                "requested_at": "2026-03-20T08:00:00Z",
            },
            "environment": {
                "status": "recovering",
                "ready": False,
                "last_error": "reattaching sandbox",
                "prepared_at": "2026-03-20T07:58:00Z",
                "manifest": {"bootstrap": {"status": "ready"}},
            },
            "subagents": {
                "task-1": {
                    "status": "running",
                    "role": "researcher",
                    "owner_token": "secret",
                }
            },
        }

    monkeypatch.setattr(shadow_clone_routes, "get_state", _state)

    result = await shadow_clone_routes.get_shadow_clone_status(
        agent_run_id="run-5-internal",
        user_id="user-5",
    )

    assert result["status"] == "running"
    assert result["environment"]["status"] == "recovering"
    assert "execution_epoch" not in result
    assert "last_completed_layer" not in result
    assert result["recovery"]["pending"] is True
    assert result["recovery"]["kind"] == "clone"
    assert result["recovery"]["requested_at"] == "2026-03-20T08:00:00Z"
    assert "source" not in result["recovery"]
    assert "owner_token" not in result["subagents"]["task-1"]


@pytest.mark.asyncio
async def test_get_shadow_clone_status_prefers_lease_environment_truth(monkeypatch):
    _install_fake_db(
        monkeypatch,
        agent_runs=[{"agent_run_id": "run-5b", "thread_id": "thread-5b"}],
        threads=[{"thread_id": "thread-5b", "account_id": "user-5"}],
    )

    async def _state(_run_id: str):
        return {
            "status": "running",
            "environment": {
                "status": "ready",
                "ready": True,
                "prepared_at": "state-ts",
                "manifest": {
                    "bootstrap": {"status": "state"},
                },
            },
            "proposal": {
                "subtasks": [
                    {
                        "id": "task-1",
                        "role": "researcher",
                        "task_description": "Inspect the current environment.",
                    }
                ],
                "dependencies": [],
            },
        }

    async def _lease(_run_id: str):
        return {
            "sandbox_id": "sbx-1",
            "sandbox_type": "desktop",
            "binding_state": "recovering",
            "environment_status": "recovering",
            "environment_ready": False,
            "environment_prepared_at": "lease-ts",
            "environment_manifest": {
                "sandbox": {"id": "sbx-1"},
                "bootstrap": {"status": "ready"},
            },
        }

    monkeypatch.setattr(shadow_clone_routes, "get_state", _state)
    monkeypatch.setattr(shadow_clone_routes, "get_run_sandbox_lease", _lease)

    result = await shadow_clone_routes.get_shadow_clone_status(
        agent_run_id="run-5b",
        user_id="user-5",
    )

    assert result["sandbox"]["id"] == "sbx-1"
    assert result["sandbox"]["environment_status"] == "recovering"
    assert result["environment"]["status"] == "recovering"
    assert result["environment"]["ready"] is False
    assert result["environment"]["prepared_at"] == "lease-ts"
    assert result["environment"]["manifest"]["sandbox"]["id"] == "sbx-1"
    assert result["environment"]["manifest"]["bootstrap"]["status"] == "ready"
    assert (
        result["proposal"]["subtasks"][0]["task_description"]
        == "Inspect the current environment."
    )


@pytest.mark.asyncio
async def test_get_shadow_clone_status_treats_lost_lease_as_recovering_replacement_when_parent_is_nonterminal(
    monkeypatch,
):
    _install_fake_db(
        monkeypatch,
        agent_runs=[
            {
                "agent_run_id": "run-5b-failed",
                "thread_id": "thread-5b-failed",
                "status": "running",
                "metadata": '{"shadow_clone_mode": "auto"}',
            }
        ],
        threads=[{"thread_id": "thread-5b-failed", "account_id": "user-5"}],
    )

    async def _state(_run_id: str):
        return {
            "status": "running",
            "environment": {
                "status": "ready",
                "ready": True,
                "last_error": None,
            },
        }

    async def _lease(_run_id: str):
        return {
            "sandbox_id": "sbx-lost",
            "sandbox_type": "desktop",
            "binding_state": "lost",
            "environment_status": "ready",
            "environment_ready": True,
            "last_error": "strict sandbox disappeared",
        }

    monkeypatch.setattr(shadow_clone_routes, "get_state", _state)
    monkeypatch.setattr(shadow_clone_routes, "get_run_sandbox_lease", _lease)

    result = await shadow_clone_routes.get_shadow_clone_status(
        agent_run_id="run-5b-failed",
        user_id="user-5",
    )

    assert result["status"] == "running"
    assert result["sandbox"]["binding_state"] == "lost"
    assert result["sandbox"]["last_error"] == "strict sandbox disappeared"
    assert result["sandbox"]["environment_status"] == "recovering"
    assert result["environment"]["ready"] is False
    assert result["environment"]["status"] == "recovering"
    assert result["environment"]["last_error"] == "strict sandbox disappeared"
    assert result["recovery"]["pending"] is True
    assert result["recovery"]["kind"] == "replacement"
    assert result["recovery"]["message"] == "strict sandbox disappeared"
    assert result["file_delivery_source"] == _expected_file_delivery_source(
        agent_run_id="run-5b-failed",
        browse_sandbox_id="sbx-lost",
        archive_sandbox_id="sbx-lost",
        identity_source="shadow_clone_lease",
        binding_state="lost",
        environment_status="ready",
        environment_ready=True,
        execution_epoch=None,
        manifest_sandbox_id=None,
        terminal_status=None,
    )


@pytest.mark.asyncio
async def test_get_shadow_clone_status_file_delivery_source_uses_effective_terminal_status(
    monkeypatch,
):
    _install_fake_db(
        monkeypatch,
        agent_runs=[
            {
                "agent_run_id": "run-5b-failed-terminal",
                "thread_id": "thread-5b-failed-terminal",
                "status": "failed",
                "metadata": '{"shadow_clone_mode": "auto"}',
            }
        ],
        threads=[{"thread_id": "thread-5b-failed-terminal", "account_id": "user-5"}],
    )

    async def _state(_run_id: str):
        return {
            "status": "running",
            "environment": {
                "status": "ready",
                "ready": True,
            },
        }

    async def _lease(_run_id: str):
        return {
            "sandbox_id": "sbx-lost-terminal",
            "sandbox_type": "desktop",
            "binding_state": "lost",
            "environment_status": "ready",
            "environment_ready": True,
            "last_error": "strict sandbox disappeared",
            "execution_epoch": 9,
            "environment_manifest": {"sandbox": {"id": "sbx-lost-terminal"}},
        }

    monkeypatch.setattr(shadow_clone_routes, "get_state", _state)
    monkeypatch.setattr(shadow_clone_routes, "get_run_sandbox_lease", _lease)

    result = await shadow_clone_routes.get_shadow_clone_status(
        agent_run_id="run-5b-failed-terminal",
        user_id="user-5",
    )

    assert result["status"] == "failed"
    assert result["file_delivery_source"] == _expected_file_delivery_source(
        agent_run_id="run-5b-failed-terminal",
        browse_sandbox_id="sbx-lost-terminal",
        archive_sandbox_id="sbx-lost-terminal",
        identity_source="shadow_clone_lease",
        binding_state="lost",
        environment_status="ready",
        environment_ready=True,
        execution_epoch=9,
        manifest_sandbox_id="sbx-lost-terminal",
        terminal_status="failed",
    )


@pytest.mark.asyncio
async def test_get_shadow_clone_status_file_delivery_source_prefers_current_lease_sandbox_id_over_manifest(
    monkeypatch,
):
    _install_fake_db(
        monkeypatch,
        agent_runs=[
            {
                "agent_run_id": "run-5b-rebound",
                "thread_id": "thread-5b-rebound",
                "status": "running",
                "metadata": '{"shadow_clone_mode": "auto"}',
            }
        ],
        threads=[{"thread_id": "thread-5b-rebound", "account_id": "user-5"}],
    )

    async def _state(_run_id: str):
        return {
            "status": "running",
            "sandbox": {
                "id": "sbx-stale-manifest",
                "type": "desktop",
                "binding_state": "recovering",
            },
            "environment": {
                "status": "ready",
                "ready": True,
                "manifest": {
                    "sandbox": {
                        "id": "sbx-stale-manifest",
                        "type": "desktop",
                    }
                },
            },
        }

    async def _lease(_run_id: str):
        return {
            "sandbox_id": "sbx-current-bound",
            "sandbox_type": "desktop",
            "binding_state": "locked",
            "environment_status": "ready",
            "environment_ready": True,
            "execution_epoch": 11,
            "environment_manifest": {
                "sandbox": {
                    "id": "sbx-stale-manifest",
                    "type": "desktop",
                }
            },
        }

    monkeypatch.setattr(shadow_clone_routes, "get_state", _state)
    monkeypatch.setattr(shadow_clone_routes, "get_run_sandbox_lease", _lease)

    result = await shadow_clone_routes.get_shadow_clone_status(
        agent_run_id="run-5b-rebound",
        user_id="user-5",
    )

    assert result["sandbox"]["id"] == "sbx-current-bound"
    assert result["environment"]["manifest"]["sandbox"]["id"] == "sbx-stale-manifest"
    assert result["file_delivery_source"] == _expected_file_delivery_source(
        agent_run_id="run-5b-rebound",
        browse_sandbox_id="sbx-current-bound",
        archive_sandbox_id="sbx-current-bound",
        identity_source="shadow_clone_lease",
        binding_state="locked",
        environment_status="ready",
        environment_ready=True,
        execution_epoch=11,
        manifest_sandbox_id="sbx-stale-manifest",
        terminal_status=None,
    )


@pytest.mark.asyncio
async def test_get_shadow_clone_status_exposes_recovery_required_when_lease_truth_is_ahead(
    monkeypatch,
):
    _install_fake_db(
        monkeypatch,
        agent_runs=[
            {"agent_run_id": "run-5b-recovery", "thread_id": "thread-5b-recovery"}
        ],
        threads=[{"thread_id": "thread-5b-recovery", "account_id": "user-5"}],
    )

    async def _state(_run_id: str):
        return {
            "status": "running",
            "environment": {
                "status": "ready",
                "ready": True,
            },
        }

    async def _lease(_run_id: str):
        return {
            "sandbox_id": "sbx-recovery",
            "sandbox_type": "desktop",
            "binding_state": "recovery_required",
            "environment_status": "recovering",
            "environment_ready": True,
            "last_error": "checkpoint recovery required",
        }

    monkeypatch.setattr(shadow_clone_routes, "get_state", _state)
    monkeypatch.setattr(shadow_clone_routes, "get_run_sandbox_lease", _lease)

    result = await shadow_clone_routes.get_shadow_clone_status(
        agent_run_id="run-5b-recovery",
        user_id="user-5",
    )

    assert result["status"] == "running"
    assert result["environment"]["status"] == "recovering"
    assert result["environment"]["ready"] is False
    assert result["recovery"]["pending"] is True
    assert result["recovery"]["kind"] == "clone"
    assert result["recovery"]["message"] == "checkpoint recovery required"


@pytest.mark.asyncio
async def test_get_shadow_clone_status_converges_external_stop_truth_over_released_lease(
    monkeypatch,
):
    _install_fake_db(
        monkeypatch,
        agent_runs=[
            {
                "agent_run_id": "run-stop-converged",
                "thread_id": "thread-stop-converged",
                "status": "stopped",
                "metadata": '{"shadow_clone_mode": "auto"}',
            }
        ],
        threads=[{"thread_id": "thread-stop-converged", "account_id": "user-stop"}],
    )

    async def _state(_run_id: str):
        return {
            "status": "running",
            "running": 1,
            "environment": {
                "status": "ready",
                "ready": True,
            },
            "live_activity": {
                "scope": "shadow_clone_main",
                "phase": "execution",
                "reason": "subagents_running",
            },
            "subagents": {
                "task-1": {
                    "status": "running",
                    "role": "researcher",
                }
            },
        }

    async def _lease(_run_id: str):
        return {
            "sandbox_id": "sbx-stop",
            "sandbox_type": "desktop",
            "binding_state": "released",
            "environment_status": "cancelled",
            "environment_ready": False,
            "last_error": None,
        }

    monkeypatch.setattr(shadow_clone_routes, "get_state", _state)
    monkeypatch.setattr(shadow_clone_routes, "get_run_sandbox_lease", _lease)

    result = await shadow_clone_routes.get_shadow_clone_status(
        agent_run_id="run-stop-converged",
        user_id="user-stop",
    )

    assert result["status"] == "cancelled"
    assert result["completion_mode"] is None
    assert result["terminal_reason"] == "parent_run_stopped"
    assert result["running"] == 0
    assert result["environment"]["status"] == "cancelled"
    assert result["environment"]["ready"] is False
    assert result["sandbox"]["binding_state"] == "released"
    assert result["sandbox"]["environment_status"] == "cancelled"
    assert result["live_activity"]["phase"] == "cancelled"
    assert result["live_activity"]["reason"] == "parent_run_stopped"
    assert result["subagents"]["task-1"]["status"] == "cancelled"


@pytest.mark.asyncio
async def test_get_shadow_clone_status_parent_stop_overrides_stale_aggregating_state(
    monkeypatch,
):
    _install_fake_db(
        monkeypatch,
        agent_runs=[
            {
                "agent_run_id": "run-stop-aggregating",
                "thread_id": "thread-stop-aggregating",
                "status": "stopped",
                "metadata": '{"shadow_clone_mode": "auto"}',
            }
        ],
        threads=[
            {
                "thread_id": "thread-stop-aggregating",
                "account_id": "user-stop-aggregating",
            }
        ],
    )

    async def _state(_run_id: str):
        return {
            "status": "aggregating",
            "total": 2,
            "completed": 2,
            "failed": 0,
            "running": 0,
            "environment": {
                "status": "ready",
                "ready": True,
            },
            "live_activity": {
                "scope": "main_agent",
                "phase": "aggregate",
                "reason": "aggregate_started",
            },
            "subagents": {
                "task-1": {
                    "status": "completed",
                    "role": "researcher",
                },
                "task-2": {
                    "status": "completed",
                    "role": "analyst",
                },
            },
        }

    async def _lease(_run_id: str):
        return {
            "sandbox_id": "sbx-stop-aggregating",
            "sandbox_type": "desktop",
            "binding_state": "released",
            "environment_status": "cancelled",
            "environment_ready": False,
            "last_error": None,
        }

    monkeypatch.setattr(shadow_clone_routes, "get_state", _state)
    monkeypatch.setattr(shadow_clone_routes, "get_run_sandbox_lease", _lease)

    result = await shadow_clone_routes.get_shadow_clone_status(
        agent_run_id="run-stop-aggregating",
        user_id="user-stop-aggregating",
    )

    assert result["status"] == "cancelled"
    assert result["completion_mode"] is None
    assert result["terminal_reason"] == "parent_run_stopped"
    assert result["completed"] == 2
    assert result["failed"] == 0
    assert result["running"] == 0
    assert result["environment"]["status"] == "cancelled"
    assert result["environment"]["ready"] is False
    assert result["sandbox"]["binding_state"] == "released"
    assert result["sandbox"]["environment_status"] == "cancelled"
    assert result["live_activity"]["phase"] == "cancelled"
    assert result["live_activity"]["reason"] == "parent_run_stopped"
    assert result["subagents"]["task-1"]["status"] == "completed"
    assert result["subagents"]["task-2"]["status"] == "completed"


@pytest.mark.asyncio
async def test_get_shadow_clone_status_returns_parent_stop_fallback_when_state_is_missing(
    monkeypatch,
):
    _install_fake_db(
        monkeypatch,
        agent_runs=[
            {
                "agent_run_id": "run-stop-fallback",
                "thread_id": "thread-stop-fallback",
                "status": "stopped",
                "metadata": '{"shadow_clone_mode": "auto"}',
            }
        ],
        threads=[{"thread_id": "thread-stop-fallback", "account_id": "user-stop"}],
    )

    async def _state(_run_id: str):
        return None

    monkeypatch.setattr(shadow_clone_routes, "get_state", _state)

    result = await shadow_clone_routes.get_shadow_clone_status(
        agent_run_id="run-stop-fallback",
        user_id="user-stop",
    )

    assert result == {
        "status": "cancelled",
        "completion_mode": None,
        "terminal_reason": "parent_run_stopped",
        "activity_owner": "none",
        "ui_phase": "cancelled",
        "phase_reason": "parent_run_stopped",
        "environment": {
            "ready": False,
            "status": "cancelled",
        },
        "file_delivery_source": _expected_file_delivery_source(
            agent_run_id="run-stop-fallback",
            environment_status="cancelled",
            environment_ready=False,
            terminal_status="cancelled",
        ),
    }


@pytest.mark.asyncio
async def test_get_shadow_clone_status_projects_completed_from_response_tail_when_parent_row_is_stale(
    monkeypatch,
):
    _install_fake_db(
        monkeypatch,
        agent_runs=[
            {
                "agent_run_id": "run-stream-complete",
                "thread_id": "thread-stream-complete",
                "status": "running",
                "metadata": '{"shadow_clone_mode": "auto"}',
            }
        ],
        threads=[
            {
                "thread_id": "thread-stream-complete",
                "account_id": "user-stream-complete",
            }
        ],
    )

    async def _state(_run_id: str):
        return None

    async def _stream_terminal(_run_id: str):
        return "completed", "Agent run completed successfully"

    monkeypatch.setattr(shadow_clone_routes, "get_state", _state)
    monkeypatch.setattr(
        shadow_clone_routes,
        "read_terminal_status_from_response_tail",
        _stream_terminal,
    )

    result = await shadow_clone_routes.get_shadow_clone_status(
        agent_run_id="run-stream-complete",
        user_id="user-stream-complete",
    )

    assert result == {
        "status": "completed",
        "completion_mode": None,
        "terminal_reason": "response_stream_completed",
        "activity_owner": "none",
        "ui_phase": "completed",
        "phase_reason": "response_stream_completed",
        "environment": {
            "ready": False,
            "status": "completed",
        },
        "file_delivery_source": _expected_file_delivery_source(
            agent_run_id="run-stream-complete",
            environment_status="completed",
            environment_ready=False,
            terminal_status="completed",
        ),
    }


@pytest.mark.asyncio
async def test_shadow_clone_status_terminal_fallback_preserves_file_delivery_source(
    monkeypatch,
):
    _install_fake_db(
        monkeypatch,
        agent_runs=[
            {
                "agent_run_id": "run-stop-fallback-identity",
                "thread_id": "thread-stop-fallback-identity",
                "status": "stopped",
                "metadata": '{"shadow_clone_mode": "auto"}',
            }
        ],
        threads=[
            {
                "thread_id": "thread-stop-fallback-identity",
                "account_id": "user-stop-identity",
            }
        ],
    )

    async def _state(_run_id: str):
        return {
            "status": "aggregating",
            "environment": {
                "status": "ready",
                "ready": True,
            },
        }

    async def _lease(_run_id: str):
        return {
            "run_id": "run-stop-fallback-identity",
            "project_id": "proj-1",
            "thread_id": "thread-stop-fallback-identity",
            "sandbox_id": "sbx-run-identity",
            "binding_state": "released",
            "environment_status": "cancelled",
            "environment_ready": False,
            "execution_epoch": 4,
            "environment_manifest": {"sandbox": {"id": "sbx-run-identity"}},
        }

    monkeypatch.setattr(shadow_clone_routes, "get_state", _state)
    monkeypatch.setattr(shadow_clone_routes, "get_run_sandbox_lease", _lease)

    result = await shadow_clone_routes.get_shadow_clone_status(
        agent_run_id="run-stop-fallback-identity",
        user_id="user-stop-identity",
    )

    assert result["status"] == "cancelled"
    assert result["file_delivery_source"] == _expected_file_delivery_source(
        agent_run_id="run-stop-fallback-identity",
        browse_sandbox_id="sbx-run-identity",
        archive_sandbox_id="sbx-run-identity",
        identity_source="shadow_clone_lease",
        binding_state="released",
        environment_status="cancelled",
        environment_ready=False,
        execution_epoch=4,
        manifest_sandbox_id="sbx-run-identity",
        terminal_status="cancelled",
    )


@pytest.mark.asyncio
async def test_get_shadow_clone_status_returns_parent_failure_fallback_when_state_is_missing(
    monkeypatch,
):
    _install_fake_db(
        monkeypatch,
        agent_runs=[
            {
                "agent_run_id": "run-failed-fallback",
                "thread_id": "thread-failed-fallback",
                "status": "failed",
                "error": "shadow clone sync failed",
                "metadata": '{"shadow_clone_mode": "auto"}',
            }
        ],
        threads=[{"thread_id": "thread-failed-fallback", "account_id": "user-failed"}],
    )

    async def _state(_run_id: str):
        return None

    monkeypatch.setattr(shadow_clone_routes, "get_state", _state)

    result = await shadow_clone_routes.get_shadow_clone_status(
        agent_run_id="run-failed-fallback",
        user_id="user-failed",
    )

    assert result == {
        "status": "failed",
        "completion_mode": None,
        "terminal_reason": "parent_run_failed",
        "activity_owner": "none",
        "ui_phase": "failed",
        "phase_reason": "parent_run_failed",
        "environment": {
            "ready": False,
            "status": "failed",
            "last_error": "shadow clone sync failed",
        },
        "file_delivery_source": _expected_file_delivery_source(
            agent_run_id="run-failed-fallback",
            environment_status="failed",
            environment_ready=False,
            terminal_status="failed",
        ),
    }


@pytest.mark.asyncio
async def test_get_shadow_clone_status_does_not_override_existing_terminal_shadow_state(
    monkeypatch,
):
    _install_fake_db(
        monkeypatch,
        agent_runs=[
            {
                "agent_run_id": "run-terminal-preserved",
                "thread_id": "thread-terminal-preserved",
                "status": "stopped",
                "metadata": '{"shadow_clone_mode": "auto"}',
            }
        ],
        threads=[
            {
                "thread_id": "thread-terminal-preserved",
                "account_id": "user-terminal",
            }
        ],
    )

    async def _state(_run_id: str):
        return {
            "status": "failed",
            "environment": {
                "status": "failed",
                "ready": False,
                "last_error": "provider error",
            },
            "live_activity": {
                "scope": "shadow_clone_main",
                "phase": "failed",
                "reason": "provider_error",
            },
        }

    monkeypatch.setattr(shadow_clone_routes, "get_state", _state)

    result = await shadow_clone_routes.get_shadow_clone_status(
        agent_run_id="run-terminal-preserved",
        user_id="user-terminal",
    )

    assert result["status"] == "failed"
    assert result["environment"]["status"] == "failed"
    assert result["environment"]["last_error"] == "provider error"
    assert result["live_activity"]["phase"] == "failed"


@pytest.mark.asyncio
async def test_get_shadow_clone_status_parent_failure_overrides_stale_pending_planning(
    monkeypatch,
):
    _install_fake_db(
        monkeypatch,
        agent_runs=[
            {
                "agent_run_id": "run-parent-failed-stale",
                "thread_id": "thread-parent-failed-stale",
                "status": "failed",
                "error": "main agent setup failed",
                "metadata": '{"shadow_clone_mode": "on"}',
            }
        ],
        threads=[
            {
                "thread_id": "thread-parent-failed-stale",
                "account_id": "user-parent-failed-stale",
            }
        ],
    )

    async def _state(_run_id: str):
        return {
            "status": "pending",
            "running": 3,
            "environment": {
                "status": "pending",
                "ready": True,
            },
            "live_activity": {
                "scope": "shadow_clone_main",
                "phase": "planning",
                "reason": "planning_started",
            },
            "subagents": {
                "task-1": {
                    "status": "running",
                    "role": "researcher",
                }
            },
        }

    monkeypatch.setattr(shadow_clone_routes, "get_state", _state)

    result = await shadow_clone_routes.get_shadow_clone_status(
        agent_run_id="run-parent-failed-stale",
        user_id="user-parent-failed-stale",
    )

    assert result["status"] == "failed"
    assert result["completion_mode"] is None
    assert result["terminal_reason"] == "parent_run_failed"
    assert result["running"] == 0
    assert result["environment"]["status"] == "failed"
    assert result["environment"]["ready"] is False
    assert result["environment"]["last_error"] == "main agent setup failed"
    assert result["live_activity"]["phase"] == "failed"
    assert result["live_activity"]["reason"] == "parent_run_failed"
    assert result["subagents"]["task-1"]["status"] == "failed"


@pytest.mark.asyncio
async def test_get_shadow_clone_status_exposes_recovery_and_attempt_metadata(
    monkeypatch,
):
    _install_fake_db(
        monkeypatch,
        agent_runs=[{"agent_run_id": "run-5c", "thread_id": "thread-5c"}],
        threads=[{"thread_id": "thread-5c", "account_id": "user-5"}],
    )

    async def _state(_run_id: str):
        return {
            "status": "running",
            "recovery": {
                "pending": True,
                "kind": "layer_review",
                "message": "Layer review required.",
                "requested_at": "2026-03-23T00:00:00Z",
                "scope": "layer:0",
                "failed_subtasks": [
                    {
                        "subtask_id": "task-2",
                        "failure_class": "timeout",
                        "attempt_index": 1,
                    }
                ],
                "retryable_subtasks": ["task-2"],
                "decision": None,
                "decision_at": None,
                "decision_source": "internal-only",
            },
            "subagents": {
                "task-2": {
                    "status": "failed",
                    "role": "designer",
                    "attempt_index": 1,
                    "failure_class": "timeout",
                    "last_error": "SubAgent timed out after 300 seconds.",
                    "recovery": {
                        "mode": "replacement",
                        "phase": "replacement_started",
                        "reason": "SubAgent timed out after 300 seconds.",
                        "wake_attempts": 1,
                        "replacement_attempts": 1,
                        "replacement_context_id": "ctx-123",
                        "handoff_summary": "Use the partial notes and continue from the draft.",
                        "updated_at": "2026-03-26T00:00:00Z",
                        "last_command_id": "secret-command-id",
                    },
                    "owner_token": "secret",
                }
            },
        }

    monkeypatch.setattr(shadow_clone_routes, "get_state", _state)

    result = await shadow_clone_routes.get_shadow_clone_status(
        agent_run_id="run-5c",
        user_id="user-5",
    )

    assert result["recovery"]["kind"] == "layer_review"
    assert result["recovery"]["failed_subtasks"][0]["subtask_id"] == "task-2"
    assert result["recovery"]["retryable_subtasks"] == ["task-2"]
    assert "decision_source" not in result["recovery"]
    assert result["subagents"]["task-2"]["attempt_index"] == 1
    assert result["subagents"]["task-2"]["failure_class"] == "timeout"
    assert (
        result["subagents"]["task-2"]["last_error"]
        == "SubAgent timed out after 300 seconds."
    )
    assert result["subagents"]["task-2"]["recovery"] == {
        "mode": "replacement",
        "phase": "replacement_started",
        "reason": "SubAgent timed out after 300 seconds.",
        "wake_attempts": 1,
        "replacement_attempts": 1,
        "replacement_context_id": "ctx-123",
        "handoff_summary": "Use the partial notes and continue from the draft.",
        "updated_at": "2026-03-26T00:00:00Z",
    }
    assert "owner_token" not in result["subagents"]["task-2"]


@pytest.mark.asyncio
async def test_get_shadow_clone_status_surfaces_on_mode_missing_proposal_failure(
    monkeypatch,
):
    _install_fake_db(
        monkeypatch,
        agent_runs=[
            {
                "agent_run_id": "run-on-missing-proposal",
                "thread_id": "thread-on-missing-proposal",
            }
        ],
        threads=[{"thread_id": "thread-on-missing-proposal", "account_id": "user-6"}],
    )

    async def _state(_run_id: str):
        return {
            "status": "pending",
            "environment": {
                "status": "failed",
                "ready": False,
                "last_error": (
                    "Shadow Clone mode 'on' requires a proposal via spawn_subagents, "
                    "but the planning turn completed without producing one."
                ),
                "manifest": {
                    "proposal_required": True,
                    "proposal_missing": True,
                    "shadow_clone_mode": "on",
                },
            },
            "live_activity": {
                "scope": "shadow_clone_main",
                "phase": "failed",
                "reason": "proposal_required_but_missing",
            },
        }

    monkeypatch.setattr(shadow_clone_routes, "get_state", _state)

    result = await shadow_clone_routes.get_shadow_clone_status(
        agent_run_id="run-on-missing-proposal",
        user_id="user-6",
    )

    assert result["status"] == "failed"
    assert result["environment"]["status"] == "failed"
    assert result["environment"]["manifest"]["proposal_missing"] is True
    assert result["live_activity"]["reason"] == "proposal_required_but_missing"


@pytest.mark.asyncio
async def test_get_shadow_clone_status_not_found(monkeypatch):
    _install_fake_db(
        monkeypatch,
        agent_runs=[{"agent_run_id": "run-6", "thread_id": "thread-6"}],
        threads=[{"thread_id": "thread-6", "account_id": "user-6"}],
    )

    async def _state(_run_id: str):
        return None

    monkeypatch.setattr(shadow_clone_routes, "get_state", _state)

    result = await shadow_clone_routes.get_shadow_clone_status(
        agent_run_id="run-6",
        user_id="user-6",
    )

    assert result == {"status": "not_found"}


@pytest.mark.asyncio
async def test_get_shadow_clone_status_ignores_stream_terminal_projection_without_shadow_clone_context(
    monkeypatch,
):
    _install_fake_db(
        monkeypatch,
        agent_runs=[
            {
                "agent_run_id": "run-plain-complete",
                "thread_id": "thread-plain-complete",
                "status": "running",
                "metadata": "{}",
            }
        ],
        threads=[
            {
                "thread_id": "thread-plain-complete",
                "account_id": "user-plain-complete",
            }
        ],
    )

    async def _state(_run_id: str):
        return None

    async def _stream_terminal(_run_id: str):
        return "completed", "done"

    monkeypatch.setattr(shadow_clone_routes, "get_state", _state)
    monkeypatch.setattr(
        shadow_clone_routes,
        "read_terminal_status_from_response_tail",
        _stream_terminal,
    )

    result = await shadow_clone_routes.get_shadow_clone_status(
        agent_run_id="run-plain-complete",
        user_id="user-plain-complete",
    )

    assert result == {"status": "not_found"}


@pytest.mark.asyncio
async def test_get_shadow_clone_results(monkeypatch):
    _install_fake_db(
        monkeypatch,
        agent_runs=[{"agent_run_id": "run-7", "thread_id": "thread-7"}],
        threads=[{"thread_id": "thread-7", "account_id": "user-7"}],
    )

    from agentscope_integration.shadow_clone import result_store

    async def _fake_read_summaries(_run_id: str):
        return [
            {
                "subtask_id": "task-1",
                "summary": "A",
                "full_key": "shadow_clone:run-7:full:task-1",
                "attempt_index": 1,
            },
            {
                "subtask_id": "task-2",
                "summary": "B",
                "full_key": "shadow_clone:run-7:full:task-2",
                "attempt_index": 2,
                "failure_class": "timeout",
            },
        ]

    monkeypatch.setattr(result_store, "read_summaries", _fake_read_summaries)

    result = await shadow_clone_routes.get_shadow_clone_results(
        agent_run_id="run-7",
        user_id="user-7",
    )
    assert result["count"] == 2
    assert len(result["results"]) == 2
    assert "full_key" not in result["results"][0]
    assert result["results"][1]["attempt_index"] == 2
    assert result["results"][1]["failure_class"] == "timeout"


@pytest.mark.asyncio
async def test_get_shadow_clone_results_reconciles_terminal_subtasks_from_state(
    monkeypatch,
):
    _install_fake_db(
        monkeypatch,
        agent_runs=[
            {"agent_run_id": "run-7b", "thread_id": "thread-7b", "status": "stopped"}
        ],
        threads=[{"thread_id": "thread-7b", "account_id": "user-7b"}],
    )

    from agentscope_integration.shadow_clone import result_store

    ensure_calls = []

    async def _fake_read_summaries(_run_id: str):
        return [
            {
                "subtask_id": "task-1",
                "role": "Researcher",
                "status": "running",
                "summary": "",
            }
        ]

    async def _fake_ensure(run_id: str, expected_results):
        ensure_calls.append((run_id, expected_results))
        return ["task-1"]

    async def _fake_state(_run_id: str):
        return {
            "status": "cancelled",
            "subagents": {
                "task-1": {
                    "status": "cancelled",
                    "role": "Researcher",
                    "attempt_index": 2,
                    "failure_class": "stop_requested",
                    "last_error": "Stopped by user",
                }
            },
        }

    monkeypatch.setattr(result_store, "read_summaries", _fake_read_summaries)
    monkeypatch.setattr(
        result_store, "ensure_terminal_result_summaries_visible", _fake_ensure
    )
    monkeypatch.setattr(shadow_clone_routes, "get_state", _fake_state)

    result = await shadow_clone_routes.get_shadow_clone_results(
        agent_run_id="run-7b",
        user_id="user-7b",
    )

    assert ensure_calls == [
        (
            "run-7b",
            [
                {
                    "subtask_id": "task-1",
                    "role": "Researcher",
                    "status": "cancelled",
                    "summary": "Stopped by user",
                    "attempt_index": 2,
                    "failure_class": "stop_requested",
                }
            ],
        )
    ]
    assert result == {
        "results": [
            {
                "subtask_id": "task-1",
                "role": "Researcher",
                "status": "cancelled",
                "summary": "Stopped by user",
                "attempt_index": 2,
                "failure_class": "stop_requested",
            }
        ],
        "count": 1,
    }


@pytest.mark.asyncio
async def test_get_shadow_clone_results_projects_parent_terminal_truth_from_response_tail(
    monkeypatch,
):
    _install_fake_db(
        monkeypatch,
        agent_runs=[
            {"agent_run_id": "run-7c", "thread_id": "thread-7c", "status": "running"}
        ],
        threads=[{"thread_id": "thread-7c", "account_id": "user-7c"}],
    )

    from agentscope_integration.shadow_clone import result_store

    async def _fake_read_summaries(_run_id: str):
        return [
            {
                "subtask_id": "task-1",
                "role": "Researcher",
                "status": "running",
                "summary": "",
            }
        ]

    async def _fake_ensure(_run_id: str, _expected_results):
        return []

    async def _fake_state(_run_id: str):
        return None

    async def _fake_terminal_truth(_run_id: str):
        return ("stopped", "Stopped by user")

    monkeypatch.setattr(result_store, "read_summaries", _fake_read_summaries)
    monkeypatch.setattr(
        result_store, "ensure_terminal_result_summaries_visible", _fake_ensure
    )
    monkeypatch.setattr(shadow_clone_routes, "get_state", _fake_state)
    monkeypatch.setattr(
        shadow_clone_routes,
        "read_terminal_status_from_response_tail",
        _fake_terminal_truth,
    )

    result = await shadow_clone_routes.get_shadow_clone_results(
        agent_run_id="run-7c",
        user_id="user-7c",
    )

    assert result == {
        "results": [
            {
                "subtask_id": "task-1",
                "role": "Researcher",
                "status": "cancelled",
                "summary": "Subagent stopped before producing a visible textual result.",
            }
        ],
        "count": 1,
    }


@pytest.mark.asyncio
async def test_get_shadow_clone_full_result_found_and_not_found(monkeypatch):
    _install_fake_db(
        monkeypatch,
        agent_runs=[{"agent_run_id": "run-8", "thread_id": "thread-8"}],
        threads=[{"thread_id": "thread-8", "account_id": "user-8"}],
    )

    from agentscope_integration.shadow_clone import result_store

    async def _fake_read_full_result(_run_id: str, subtask_id: str):
        if subtask_id == "task-ok":
            return "full text"
        return None

    monkeypatch.setattr(result_store, "read_full_result", _fake_read_full_result)

    ok = await shadow_clone_routes.get_shadow_clone_full_result(
        agent_run_id="run-8",
        subtask_id="task-ok",
        user_id="user-8",
    )
    assert ok == {"subtask_id": "task-ok", "result": "full text"}

    with pytest.raises(HTTPException) as exc_info:
        await shadow_clone_routes.get_shadow_clone_full_result(
            agent_run_id="run-8",
            subtask_id="task-missing",
            user_id="user-8",
        )
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_get_agent_runs_prefers_thread_workspace_file_delivery_source(
    monkeypatch,
):
    thread_id = "thread-workspace-api"
    user_id = "user-1"
    project_id = "proj-thread-api"
    client = _FakeClient(
        agent_runs=[
            {
                "id": "row-1",
                "agent_run_id": "run-1",
                "thread_id": thread_id,
                "status": "completed",
                "started_at": "2026-05-20T00:00:00Z",
                "completed_at": "2026-05-20T00:01:00Z",
                "created_at": "2026-05-20T00:00:00Z",
                "updated_at": "2026-05-20T00:01:00Z",
                "error": None,
                "metadata": {},
            }
        ],
        threads=[
            {"thread_id": thread_id, "account_id": user_id, "project_id": project_id}
        ],
    )
    monkeypatch.setattr(agent_api, "db", _FakeDBConnection(client))

    async def _project_status(row, client=None):
        return row

    monkeypatch.setattr(
        agent_api, "_project_agent_run_row_terminal_status", _project_status
    )
    artifact_probe_calls = []

    async def _has_artifacts_for_thread(*_args, **_kwargs):
        artifact_probe_calls.append(_kwargs)
        return True

    monkeypatch.setattr(
        agent_api.workspace_artifacts,
        "has_artifacts_for_thread",
        _has_artifacts_for_thread,
        raising=False,
    )

    payload = await agent_api.get_agent_runs(thread_id, user_id)

    assert len(artifact_probe_calls) == 1

    source = payload["agent_runs"][0]["file_delivery_source"]
    assert source["browse_sandbox_id"] == f"thread-workspace:{thread_id}"
    assert source["archive_sandbox_id"] == f"thread-workspace:{thread_id}"
    assert source["identity_source"] == "thread_workspace_artifacts"
    assert source["agent_run_id"] == f"thread-workspace:{thread_id}"
