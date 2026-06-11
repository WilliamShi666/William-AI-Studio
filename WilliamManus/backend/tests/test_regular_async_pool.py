from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from dataclasses import dataclass
from types import SimpleNamespace
import json

import pytest
from fastapi import HTTPException

import agent.api as agent_api
import run_agent_background as run_agent_background_module
from services import regular_async_pool
from services import regular_run_attempts


@dataclass
class _Filter:
    kind: str
    field: str
    value: object


class _FakeTableQuery:
    def __init__(self, rows: list[dict], table_name: str):
        self._rows = rows
        self._table_name = table_name
        self._action = "select"
        self._filters: list[_Filter] = []
        self._update_payload: dict | None = None
        self._order_field: str | None = None
        self._order_desc = False
        self._limit: int | None = None

    def select(self, *_args, **_kwargs):
        self._action = "select"
        return self

    def update(self, payload: dict):
        self._action = "update"
        self._update_payload = dict(payload)
        return self

    def eq(self, field: str, value: object):
        self._filters.append(_Filter("eq", field, value))
        return self

    def in_(self, field: str, values: list[object]):
        self._filters.append(_Filter("in", field, list(values)))
        return self

    def order(self, field: str, *, desc: bool = False):
        self._order_field = field
        self._order_desc = bool(desc)
        return self

    def limit(self, value: int):
        self._limit = int(value)
        return self

    async def execute(self):
        assert self._table_name == "agent_runs"

        def _matches(row: dict) -> bool:
            for current_filter in self._filters:
                if current_filter.kind == "eq":
                    if row.get(current_filter.field) != current_filter.value:
                        return False
                    continue
                if current_filter.kind == "in":
                    accepted_values = current_filter.value
                    if not isinstance(accepted_values, list):
                        return False
                    if row.get(current_filter.field) not in accepted_values:
                        return False
            return True

        matched_rows = [row for row in self._rows if _matches(row)]

        if self._action == "update":
            payload = self._update_payload or {}
            updated_rows: list[dict] = []
            for row in matched_rows:
                row.update(payload)
                updated_rows.append(dict(row))
            return SimpleNamespace(data=updated_rows)

        selected_rows = [dict(row) for row in matched_rows]
        if self._order_field is not None:
            selected_rows.sort(
                key=lambda row: row.get(self._order_field),
                reverse=self._order_desc,
            )
        if self._limit is not None:
            selected_rows = selected_rows[: self._limit]
        return SimpleNamespace(data=selected_rows)


class _FakeClient:
    def __init__(self, rows: list[dict]):
        self._rows = rows

    def table(self, table_name: str):
        return _FakeTableQuery(self._rows, table_name)


class _DummyDB:
    def __init__(self, client):
        self._client = client

    @property
    def client(self):
        async def _client():
            return self._client

        return _client()


class _FakeAgentApiTableQuery:
    def __init__(self, table_name: str, client):
        self._table_name = table_name
        self._client = client
        self._filters: dict[str, object] = {}
        self._action = "select"
        self._update_payload: dict | None = None

    def select(self, *_args, **_kwargs):
        self._action = "select"
        return self

    def eq(self, field: str, value: object):
        self._filters[field] = value
        return self

    def order(self, *_args, **_kwargs):
        return self

    def limit(self, *_args, **_kwargs):
        return self

    async def execute(self):
        if self._action == "update":
            if self._table_name == "agent_runs":
                payload = dict(self._update_payload or {})
                self._client.agent_run_updates.append(payload)
                updated_rows = []
                for row in self._client.agent_run_rows:
                    if all(row.get(key) == value for key, value in self._filters.items()):
                        row.update(payload)
                        updated_rows.append(dict(row))
                return SimpleNamespace(data=updated_rows)
            if self._table_name == "regular_run_attempts":
                payload = dict(self._update_payload or {})
                self._client.regular_attempt_updates.append(payload)
                updated_rows = []
                for row in self._client.regular_attempt_rows:
                    if all(row.get(key) == value for key, value in self._filters.items()):
                        row.update(payload)
                        updated_rows.append(dict(row))
                return SimpleNamespace(data=updated_rows)

        if self._table_name == "threads":
            return SimpleNamespace(data=[dict(self._client.thread_row)])
        if self._table_name == "events":
            return SimpleNamespace(
                data=[{"id": "event-1", "timestamp": "2026-04-03T00:00:00Z"}]
            )
        if self._table_name == "agents":
            agent_id = self._filters.get("agent_id", "agent-1")
            return SimpleNamespace(
                data=[
                    {
                        "agent_id": agent_id,
                        "name": "Regular Pool Agent",
                        "current_version_id": None,
                    }
                ]
            )
        if self._table_name == "agent_runs":
            result = []
            for row in self._client.agent_run_rows:
                if all(row.get(key) == value for key, value in self._filters.items()):
                    result.append(dict(row))
            return SimpleNamespace(data=result)
        if self._table_name == "regular_run_attempts":
            result = []
            for row in self._client.regular_attempt_rows:
                if all(row.get(key) == value for key, value in self._filters.items()):
                    result.append(dict(row))
            return SimpleNamespace(data=result)
        raise AssertionError(f"Unexpected execute() on table {self._table_name}")

    async def insert(self, payload: dict):
        if self._table_name == "threads":
            self._client.thread_inserts.append(dict(payload))
            return SimpleNamespace(data=[{"thread_id": payload["thread_id"]}])
        if self._table_name == "agent_runs":
            inserted_payload = dict(payload)
            self._client.agent_run_inserts.append(inserted_payload)
            self._client.agent_run_rows.append(
                {
                    "agent_run_id": inserted_payload.get("agent_run_id", "run-1"),
                    "status": inserted_payload.get("status"),
                    "metadata": inserted_payload.get("metadata"),
                }
            )
            return SimpleNamespace(
                data=[{"agent_run_id": inserted_payload.get("agent_run_id", "run-1")}]
            )
        if self._table_name == "regular_run_attempts":
            inserted_payload = dict(payload)
            self._client.regular_attempt_inserts.append(inserted_payload)
            self._client.regular_attempt_rows.append(dict(inserted_payload))
            return SimpleNamespace(data=[dict(inserted_payload)])
        raise AssertionError(f"Unexpected insert() on table {self._table_name}")

    def update(self, payload: dict):
        self._action = "update"
        self._update_payload = dict(payload)
        if self._table_name == "agent_runs":
            return self
        if self._table_name == "regular_run_attempts":
            return self
        return self


class _FakeAgentApiClient:
    def __init__(
        self,
        *,
        thread_row: dict | None = None,
        agent_run_rows: list[dict] | None = None,
        regular_attempt_rows: list[dict] | None = None,
    ):
        self.thread_row = thread_row or {
            "project_id": "project-1",
            "account_id": "user-1",
            "metadata": {},
        }
        self.agent_run_rows = [dict(row) for row in (agent_run_rows or [])]
        self.regular_attempt_rows = [dict(row) for row in (regular_attempt_rows or [])]
        self.thread_inserts: list[dict] = []
        self.agent_run_inserts: list[dict] = []
        self.agent_run_updates: list[dict] = []
        self.regular_attempt_inserts: list[dict] = []
        self.regular_attempt_updates: list[dict] = []

    def schema(self, _name: str):
        return self

    def table(self, table_name: str):
        return _FakeAgentApiTableQuery(table_name, self)


def _install_start_agent_queue_harness(
    monkeypatch,
    *,
    queue_full: bool = False,
):
    fake_client = _FakeAgentApiClient()
    queue_capacity_calls = []
    dispatch_calls = []
    send_calls = []

    async def _resolve_shadow_clone_model_selection(**_kwargs):
        return {
            "requested_shadow_clone_main_model": None,
            "requested_shadow_clone_subagent_model": None,
            "effective_shadow_clone_main_model": None,
            "effective_shadow_clone_subagent_model": None,
        }

    async def _ensure_queue_capacity(client, additional_slots: int = 1):
        queue_capacity_calls.append(
            {
                "client": client,
                "additional_slots": additional_slots,
            }
        )
        if queue_full:
            raise regular_async_pool.RegularQueueFullError(
                "regular queue depth exhausted"
            )

    def _unexpected_send(**kwargs):
        send_calls.append(kwargs)
        raise AssertionError(
            "queued regular run must not dispatch run_agent_background"
        )

    async def _unexpected_capacity_snapshot(**_kwargs):
        raise AssertionError(
            "queued regular run must bypass active capacity snapshot gating"
        )

    async def _unexpected_capacity_reservation(**_kwargs):
        raise AssertionError(
            "queued regular run must bypass API-side active capacity reservation"
        )

    async def _unexpected_redis_set(*_args, **_kwargs):
        raise AssertionError(
            "queued regular run must not register active_run Redis key"
        )

    monkeypatch.setattr(agent_api, "db", _DummyDB(fake_client))
    monkeypatch.setattr(agent_api, "instance_id", "test-instance")
    monkeypatch.setattr(
        agent_api,
        "_resolve_shadow_clone_model_selection",
        _resolve_shadow_clone_model_selection,
    )
    monkeypatch.setattr(
        agent_api, "extract_agent_config", lambda agent, _version: agent
    )
    monkeypatch.setattr(agent_api, "is_server_concurrency_budget_enabled", lambda: True)
    monkeypatch.setattr(agent_api, "get_run_capacity_cost", lambda _mode: 1)
    monkeypatch.setattr(
        agent_api, "get_safe_run_capacity_snapshot", _unexpected_capacity_snapshot
    )
    monkeypatch.setattr(
        agent_api, "_reserve_run_capacity_or_raise", _unexpected_capacity_reservation
    )
    monkeypatch.setattr(
        agent_api.regular_async_pool,
        "ensure_regular_queue_capacity",
        _ensure_queue_capacity,
    )
    monkeypatch.setattr(
        agent_api,
        "regular_async_pool_dispatch",
        SimpleNamespace(
            send=lambda: dispatch_calls.append("regular_async_pool_dispatch")
        ),
        raising=False,
    )
    monkeypatch.setattr(
        agent_api.structlog.contextvars,
        "get_contextvars",
        lambda: {"request_id": "req-123", "client_operation_id": "op-456"},
    )
    monkeypatch.setattr(agent_api.run_agent_background, "send", _unexpected_send)
    monkeypatch.setattr(agent_api.redis, "set", _unexpected_redis_set)

    return {
        "client": fake_client,
        "queue_capacity_calls": queue_capacity_calls,
        "dispatch_calls": dispatch_calls,
        "send_calls": send_calls,
    }


def _install_start_agent_legacy_harness(monkeypatch):
    fake_client = _FakeAgentApiClient()
    snapshot_calls = []
    reservation_calls = []
    dispatch_calls = []
    send_calls = []
    redis_set_calls = []

    async def _resolve_shadow_clone_model_selection(**_kwargs):
        return {
            "requested_shadow_clone_main_model": None,
            "requested_shadow_clone_subagent_model": None,
            "effective_shadow_clone_main_model": None,
            "effective_shadow_clone_subagent_model": None,
        }

    async def _capacity_snapshot(**kwargs):
        snapshot_calls.append(dict(kwargs))
        return {
            "enabled": True,
            "budget": 16,
            "in_use": 0,
            "remaining": 16,
            "requested_cost": 1,
            "can_admit": True,
        }

    async def _reserve_capacity(**kwargs):
        reservation_calls.append(dict(kwargs))
        return {"acquired": True}

    def _send(**kwargs):
        send_calls.append(dict(kwargs))
        return SimpleNamespace(message_id="msg-legacy")

    async def _redis_set(*args, **kwargs):
        redis_set_calls.append((args, kwargs))
        return True

    async def _unexpected_queue_capacity(*_args, **_kwargs):
        raise AssertionError(
            "legacy path must not use regular async pool queue capacity"
        )

    monkeypatch.setattr(agent_api, "db", _DummyDB(fake_client))
    monkeypatch.setattr(agent_api, "instance_id", "test-instance")
    monkeypatch.setattr(
        agent_api,
        "_resolve_shadow_clone_model_selection",
        _resolve_shadow_clone_model_selection,
    )
    monkeypatch.setattr(
        agent_api, "extract_agent_config", lambda agent, _version: agent
    )
    monkeypatch.setattr(agent_api, "is_server_concurrency_budget_enabled", lambda: True)
    monkeypatch.setattr(agent_api, "get_run_capacity_cost", lambda _mode: 1)
    monkeypatch.setattr(agent_api, "get_safe_run_capacity_snapshot", _capacity_snapshot)
    monkeypatch.setattr(agent_api, "_reserve_run_capacity_or_raise", _reserve_capacity)
    monkeypatch.setattr(
        agent_api.regular_async_pool,
        "ensure_regular_queue_capacity",
        _unexpected_queue_capacity,
    )
    monkeypatch.setattr(
        agent_api,
        "regular_async_pool_dispatch",
        SimpleNamespace(
            send=lambda: dispatch_calls.append("regular_async_pool_dispatch")
        ),
        raising=False,
    )
    monkeypatch.setattr(
        agent_api.structlog.contextvars,
        "get_contextvars",
        lambda: {"request_id": "req-123", "client_operation_id": "op-456"},
    )
    monkeypatch.setattr(agent_api.run_agent_background, "send", _send)
    monkeypatch.setattr(agent_api.redis, "set", _redis_set)

    return {
        "client": fake_client,
        "snapshot_calls": snapshot_calls,
        "reservation_calls": reservation_calls,
        "dispatch_calls": dispatch_calls,
        "send_calls": send_calls,
        "redis_set_calls": redis_set_calls,
    }


def _install_start_agent_phase2_harness(
    monkeypatch,
    *,
    queue_full: bool = False,
    dispatch_should_fail: bool = False,
):
    fake_client = _FakeAgentApiClient()
    queue_capacity_calls = []
    attempt_create_calls = []
    dispatch_calls = []
    send_calls = []
    update_status_calls = []

    async def _resolve_shadow_clone_model_selection(**_kwargs):
        return {
            "requested_shadow_clone_main_model": None,
            "requested_shadow_clone_subagent_model": None,
            "effective_shadow_clone_main_model": None,
            "effective_shadow_clone_subagent_model": None,
        }

    async def _ensure_attempt_queue_capacity(client, additional_slots: int = 1):
        queue_capacity_calls.append(
            {
                "client": client,
                "additional_slots": additional_slots,
            }
        )
        if queue_full:
            raise regular_run_attempts.RegularAttemptQueueFullError(
                "regular attempt queue depth exhausted"
            )

    async def _create_initial_attempt(client, *, agent_run_id: str):
        assert client is fake_client
        attempt_create_calls.append(agent_run_id)
        fake_client.regular_attempt_rows.append(
            {
                "attempt_id": "attempt-1",
                "agent_run_id": agent_run_id,
                "status": "queued",
            }
        )
        return dict(fake_client.regular_attempt_rows[-1])

    def _unexpected_send(**kwargs):
        send_calls.append(kwargs)
        raise AssertionError(
            "phase2 queued regular run must not dispatch run_agent_background"
        )

    async def _unexpected_capacity_snapshot(**_kwargs):
        raise AssertionError(
            "phase2 queued regular run must bypass active capacity snapshot gating"
        )

    async def _unexpected_capacity_reservation(**_kwargs):
        raise AssertionError(
            "phase2 queued regular run must bypass API-side active capacity reservation"
        )

    async def _unexpected_redis_set(*_args, **_kwargs):
        raise AssertionError(
            "phase2 queued regular run must not register active_run Redis key"
        )

    async def _fake_update_status(_client, agent_run_id: str, status: str, error=None):
        update_status_calls.append(
            {
                "agent_run_id": agent_run_id,
                "status": status,
                "error": error,
            }
        )
        for row in fake_client.agent_run_rows:
            if row.get("agent_run_id") == agent_run_id:
                row["status"] = status
        return True

    def _dispatch():
        dispatch_calls.append("regular_supervisor_dispatch")
        if dispatch_should_fail:
            raise RuntimeError("phase2 dispatch failed")

    monkeypatch.setattr(agent_api, "db", _DummyDB(fake_client))
    monkeypatch.setattr(agent_api, "instance_id", "test-instance")
    monkeypatch.setattr(
        agent_api,
        "_resolve_shadow_clone_model_selection",
        _resolve_shadow_clone_model_selection,
    )
    monkeypatch.setattr(
        agent_api, "extract_agent_config", lambda agent, _version: agent
    )
    monkeypatch.setattr(agent_api, "is_server_concurrency_budget_enabled", lambda: True)
    monkeypatch.setattr(agent_api, "get_run_capacity_cost", lambda _mode: 1)
    monkeypatch.setattr(
        agent_api, "get_safe_run_capacity_snapshot", _unexpected_capacity_snapshot
    )
    monkeypatch.setattr(
        agent_api, "_reserve_run_capacity_or_raise", _unexpected_capacity_reservation
    )
    monkeypatch.setattr(
        agent_api.regular_run_attempts,
        "ensure_attempt_queue_capacity",
        _ensure_attempt_queue_capacity,
    )
    monkeypatch.setattr(
        agent_api.regular_run_attempts,
        "create_initial_attempt",
        _create_initial_attempt,
    )
    monkeypatch.setattr(agent_api, "update_agent_run_status", _fake_update_status)
    monkeypatch.setattr(
        agent_api,
        "regular_supervisor_dispatch",
        SimpleNamespace(send=_dispatch),
        raising=False,
    )
    monkeypatch.setattr(
        agent_api.structlog.contextvars,
        "get_contextvars",
        lambda: {"request_id": "req-123", "client_operation_id": "op-456"},
    )
    monkeypatch.setattr(agent_api.run_agent_background, "send", _unexpected_send)
    monkeypatch.setattr(agent_api.redis, "set", _unexpected_redis_set)

    return {
        "client": fake_client,
        "queue_capacity_calls": queue_capacity_calls,
        "attempt_create_calls": attempt_create_calls,
        "dispatch_calls": dispatch_calls,
        "send_calls": send_calls,
        "update_status_calls": update_status_calls,
    }


def _install_initiate_queue_harness(monkeypatch):
    fake_client = _FakeAgentApiClient()
    queue_capacity_calls = []
    dispatch_calls = []
    send_calls = []

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

    async def _ensure_queue_capacity(client, additional_slots: int = 1):
        queue_capacity_calls.append(
            {
                "client": client,
                "additional_slots": additional_slots,
            }
        )

    def _unexpected_send(**kwargs):
        send_calls.append(kwargs)
        raise AssertionError(
            "queued regular run must not dispatch run_agent_background"
        )

    async def _unexpected_capacity_snapshot(**_kwargs):
        raise AssertionError(
            "queued regular run must bypass active capacity snapshot gating"
        )

    async def _unexpected_capacity_reservation(**_kwargs):
        raise AssertionError(
            "queued regular run must bypass API-side active capacity reservation"
        )

    async def _unexpected_redis_set(*_args, **_kwargs):
        raise AssertionError(
            "queued regular run must not register active_run Redis key"
        )

    async def _noop_async(*_args, **_kwargs):
        return None

    def _drop_task(coro):
        coro.close()
        return SimpleNamespace()

    monkeypatch.setattr(agent_api, "db", _DummyDB(fake_client))
    monkeypatch.setattr(agent_api, "instance_id", "test-instance")
    monkeypatch.setattr(
        agent_api,
        "resolve_model_config",
        lambda name: SimpleNamespace(model_name=name or "gpt-test"),
    )
    monkeypatch.setattr(agent_api, "_load_owned_project", _load_owned_project)
    monkeypatch.setattr(
        agent_api,
        "_resolve_shadow_clone_model_selection",
        _resolve_shadow_clone_model_selection,
    )
    monkeypatch.setattr(
        agent_api, "extract_agent_config", lambda agent, _version: agent
    )
    monkeypatch.setattr(agent_api, "_create_adk_session_if_not_exists", _noop_async)
    monkeypatch.setattr(agent_api, "_log_adk_user_message_event", _noop_async)
    monkeypatch.setattr(agent_api, "is_server_concurrency_budget_enabled", lambda: True)
    monkeypatch.setattr(agent_api, "get_run_capacity_cost", lambda _mode: 1)
    monkeypatch.setattr(
        agent_api, "get_safe_run_capacity_snapshot", _unexpected_capacity_snapshot
    )
    monkeypatch.setattr(
        agent_api, "_reserve_run_capacity_or_raise", _unexpected_capacity_reservation
    )
    monkeypatch.setattr(
        agent_api.regular_async_pool,
        "ensure_regular_queue_capacity",
        _ensure_queue_capacity,
    )
    monkeypatch.setattr(
        agent_api,
        "regular_async_pool_dispatch",
        SimpleNamespace(
            send=lambda: dispatch_calls.append("regular_async_pool_dispatch")
        ),
        raising=False,
    )
    monkeypatch.setattr(
        agent_api.structlog.contextvars,
        "get_contextvars",
        lambda: {"request_id": "req-123", "client_operation_id": "op-456"},
    )
    monkeypatch.setattr(agent_api.run_agent_background, "send", _unexpected_send)
    monkeypatch.setattr(agent_api.redis, "set", _unexpected_redis_set)
    monkeypatch.setattr(agent_api.asyncio, "create_task", _drop_task)

    return {
        "client": fake_client,
        "queue_capacity_calls": queue_capacity_calls,
        "dispatch_calls": dispatch_calls,
        "send_calls": send_calls,
    }


def _install_initiate_phase2_harness(
    monkeypatch,
    *,
    queue_full: bool = False,
    dispatch_should_fail: bool = False,
):
    fake_client = _FakeAgentApiClient()
    queue_capacity_calls = []
    attempt_create_calls = []
    dispatch_calls = []
    send_calls = []
    update_status_calls = []

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

    async def _ensure_attempt_queue_capacity(client, additional_slots: int = 1):
        queue_capacity_calls.append(
            {
                "client": client,
                "additional_slots": additional_slots,
            }
        )
        if queue_full:
            raise regular_run_attempts.RegularAttemptQueueFullError(
                "regular attempt queue depth exhausted"
            )

    async def _create_initial_attempt(client, *, agent_run_id: str):
        assert client is fake_client
        attempt_create_calls.append(agent_run_id)
        fake_client.regular_attempt_rows.append(
            {
                "attempt_id": "attempt-1",
                "agent_run_id": agent_run_id,
                "status": "queued",
            }
        )
        return dict(fake_client.regular_attempt_rows[-1])

    def _unexpected_send(**kwargs):
        send_calls.append(kwargs)
        raise AssertionError(
            "phase2 queued regular run must not dispatch run_agent_background"
        )

    async def _unexpected_capacity_snapshot(**_kwargs):
        raise AssertionError(
            "phase2 queued regular run must bypass active capacity snapshot gating"
        )

    async def _unexpected_capacity_reservation(**_kwargs):
        raise AssertionError(
            "phase2 queued regular run must bypass API-side active capacity reservation"
        )

    async def _unexpected_redis_set(*_args, **_kwargs):
        raise AssertionError(
            "phase2 queued regular run must not register active_run Redis key"
        )

    async def _noop_async(*_args, **_kwargs):
        return None

    async def _fake_update_status(_client, agent_run_id: str, status: str, error=None):
        update_status_calls.append(
            {
                "agent_run_id": agent_run_id,
                "status": status,
                "error": error,
            }
        )
        for row in fake_client.agent_run_rows:
            if row.get("agent_run_id") == agent_run_id:
                row["status"] = status
        return True

    def _dispatch():
        dispatch_calls.append("regular_supervisor_dispatch")
        if dispatch_should_fail:
            raise RuntimeError("phase2 dispatch failed")

    def _drop_task(coro):
        coro.close()
        return SimpleNamespace()

    monkeypatch.setattr(agent_api, "db", _DummyDB(fake_client))
    monkeypatch.setattr(agent_api, "instance_id", "test-instance")
    monkeypatch.setattr(
        agent_api,
        "resolve_model_config",
        lambda name: SimpleNamespace(model_name=name or "gpt-test"),
    )
    monkeypatch.setattr(agent_api, "_load_owned_project", _load_owned_project)
    monkeypatch.setattr(
        agent_api,
        "_resolve_shadow_clone_model_selection",
        _resolve_shadow_clone_model_selection,
    )
    monkeypatch.setattr(
        agent_api, "extract_agent_config", lambda agent, _version: agent
    )
    monkeypatch.setattr(agent_api, "_create_adk_session_if_not_exists", _noop_async)
    monkeypatch.setattr(agent_api, "_log_adk_user_message_event", _noop_async)
    monkeypatch.setattr(agent_api, "is_server_concurrency_budget_enabled", lambda: True)
    monkeypatch.setattr(agent_api, "get_run_capacity_cost", lambda _mode: 1)
    monkeypatch.setattr(
        agent_api, "get_safe_run_capacity_snapshot", _unexpected_capacity_snapshot
    )
    monkeypatch.setattr(
        agent_api, "_reserve_run_capacity_or_raise", _unexpected_capacity_reservation
    )
    monkeypatch.setattr(
        agent_api.regular_run_attempts,
        "ensure_attempt_queue_capacity",
        _ensure_attempt_queue_capacity,
    )
    monkeypatch.setattr(
        agent_api.regular_run_attempts,
        "create_initial_attempt",
        _create_initial_attempt,
    )
    monkeypatch.setattr(agent_api, "update_agent_run_status", _fake_update_status)
    monkeypatch.setattr(
        agent_api,
        "regular_supervisor_dispatch",
        SimpleNamespace(send=_dispatch),
        raising=False,
    )
    monkeypatch.setattr(
        agent_api.structlog.contextvars,
        "get_contextvars",
        lambda: {"request_id": "req-123", "client_operation_id": "op-456"},
    )
    monkeypatch.setattr(agent_api.run_agent_background, "send", _unexpected_send)
    monkeypatch.setattr(agent_api.redis, "set", _unexpected_redis_set)
    monkeypatch.setattr(agent_api.asyncio, "create_task", _drop_task)

    return {
        "client": fake_client,
        "queue_capacity_calls": queue_capacity_calls,
        "attempt_create_calls": attempt_create_calls,
        "dispatch_calls": dispatch_calls,
        "send_calls": send_calls,
        "update_status_calls": update_status_calls,
    }


def test_is_regular_async_pool_enabled_only_for_regular_mode(monkeypatch):
    monkeypatch.setenv("REGULAR_ASYNC_POOL_ENABLED", "true")

    assert regular_async_pool.is_regular_async_pool_enabled("off") is True
    assert regular_async_pool.is_regular_async_pool_enabled("on") is False
    assert regular_async_pool.is_regular_async_pool_enabled("auto") is False


@pytest.mark.parametrize("mode", ["on", "auto", "v2"])
def test_shadow_clone_v2_modes_prefer_regular_supervisor_chain_by_default(
    monkeypatch,
    mode,
):
    monkeypatch.delenv("REGULAR_EXECUTION_MODE", raising=False)
    monkeypatch.delenv("REGULAR_ASYNC_POOL_ENABLED", raising=False)
    monkeypatch.delenv("SHADOW_CLONE_V2_EXECUTION_CHAIN", raising=False)

    assert (
        agent_api._resolve_regular_execution_mode(
            mode,
            shadow_clone_runtime="v2",
        )
        == "phase2_supervisor"
    )
    assert agent_api._should_queue_regular_run(
        mode,
        shadow_clone_runtime="v2",
    ) is True


@pytest.mark.parametrize(
    "configured_chain",
    ["run_agent_background", "legacy", "dramatiq"],
)
def test_shadow_clone_v2_execution_chain_can_fallback_to_run_agent_background(
    monkeypatch,
    configured_chain,
):
    monkeypatch.setenv("SHADOW_CLONE_V2_EXECUTION_CHAIN", configured_chain)
    monkeypatch.setenv("REGULAR_EXECUTION_MODE", "phase2_supervisor")

    assert (
        agent_api._resolve_regular_execution_mode(
            "on",
            shadow_clone_runtime="v2",
        )
        == "legacy"
    )
    assert agent_api._should_queue_regular_run(
        "on",
        shadow_clone_runtime="v2",
    ) is False


def test_claude_sdk_multi_agent_modes_stay_on_claude_chain(monkeypatch):
    monkeypatch.delenv("SHADOW_CLONE_V2_EXECUTION_CHAIN", raising=False)
    monkeypatch.setenv("REGULAR_EXECUTION_MODE", "phase2_supervisor")

    assert (
        agent_api._resolve_regular_execution_mode(
            "auto",
            shadow_clone_runtime="claude_sdk",
        )
        == "legacy"
    )
    assert agent_api._should_queue_regular_run(
        "auto",
        shadow_clone_runtime="claude_sdk",
    ) is False


@pytest.mark.asyncio
async def test_claim_next_regular_run_returns_oldest_queued_run():
    rows = [
        {"agent_run_id": "run-2", "status": "queued", "mode": "off", "created_at": 20},
        {"agent_run_id": "run-1", "status": "queued", "mode": "off", "created_at": 10},
    ]
    client = _FakeClient(rows)

    claimed = await regular_async_pool.claim_next_regular_run(
        client, owner_token="worker-1"
    )

    assert claimed is not None
    assert claimed["agent_run_id"] == "run-1"
    assert claimed["status"] == "running"
    assert "regular_pool_owner_token" not in claimed
    claimed_row = next(row for row in rows if row["agent_run_id"] == "run-1")
    assert "regular_pool_owner_token" not in claimed_row


@pytest.mark.asyncio
async def test_claim_next_regular_run_ignores_non_regular_queued_rows():
    rows = [
        {
            "agent_run_id": "run-shadow-clone",
            "status": "queued",
            "created_at": 10,
            "metadata": {"shadow_clone_mode": "on"},
        },
        {
            "agent_run_id": "run-regular",
            "status": "queued",
            "created_at": 20,
            "metadata": json.dumps({"shadow_clone_mode": "off"}),
        },
    ]
    client = _FakeClient(rows)

    claimed = await regular_async_pool.claim_next_regular_run(
        client, owner_token="worker-1"
    )

    assert claimed is not None
    assert claimed["agent_run_id"] == "run-regular"
    non_regular_row = next(
        row for row in rows if row["agent_run_id"] == "run-shadow-clone"
    )
    assert non_regular_row["status"] == "queued"


@pytest.mark.asyncio
async def test_claim_next_regular_run_refreshes_started_at_when_promoted():
    rows = [
        {
            "agent_run_id": "run-1",
            "status": "queued",
            "created_at": 10,
            "started_at": "2026-04-01T00:00:00+00:00",
        },
    ]
    client = _FakeClient(rows)

    claimed = await regular_async_pool.claim_next_regular_run(
        client, owner_token="worker-1"
    )

    assert claimed is not None
    assert claimed["started_at"] != "2026-04-01T00:00:00+00:00"
    assert datetime.fromisoformat(claimed["started_at"]).tzinfo is not None
    updated_row = next(row for row in rows if row["agent_run_id"] == "run-1")
    assert updated_row["started_at"] == claimed["started_at"]


@pytest.mark.asyncio
async def test_claim_next_regular_run_is_atomic_against_duplicate_dispatchers():
    rows = [
        {"agent_run_id": "run-1", "status": "queued", "mode": "off", "created_at": 10},
    ]
    client = _FakeClient(rows)

    first_claim = await regular_async_pool.claim_next_regular_run(
        client, owner_token="worker-a"
    )
    second_claim = await regular_async_pool.claim_next_regular_run(
        client, owner_token="worker-b"
    )

    assert first_claim is not None
    assert first_claim["agent_run_id"] == "run-1"
    assert second_claim is None


@pytest.mark.asyncio
async def test_heartbeat_regular_run_claim_writes_redis_claim(monkeypatch):
    recorded: dict[str, object] = {}

    async def _fake_hset(key: str, mapping: dict[str, object], **_kwargs):
        recorded["hset"] = (key, dict(mapping))
        return 1

    async def _fake_expire(key: str, seconds: int, **_kwargs):
        recorded["expire"] = (key, seconds)
        return True

    monkeypatch.setattr(regular_async_pool.redis, "hset", _fake_hset)
    monkeypatch.setattr(regular_async_pool.redis, "expire", _fake_expire)

    result = await regular_async_pool.heartbeat_regular_run_claim(
        "run-1",
        "owner-1",
    )

    assert result["refreshed"] is True
    assert recorded["hset"][0] == "regular_async_pool:claim:run-1"
    assert recorded["hset"][1]["owner_token"] == "owner-1"
    assert "heartbeat_at" in recorded["hset"][1]
    assert recorded["expire"][0] == "regular_async_pool:claim:run-1"


@pytest.mark.asyncio
async def test_find_orphaned_regular_async_pool_run_ids_ignores_legacy_running_rows(
    monkeypatch,
):
    client = _FakeClient(
        [
            {
                "agent_run_id": "run-legacy",
                "status": "running",
                "metadata": json.dumps({"shadow_clone_mode": "off"}),
            }
        ]
    )

    async def _fake_read_claim(_agent_run_id: str) -> dict[str, object]:
        return {}

    monkeypatch.setattr(
        regular_async_pool,
        "_read_regular_run_claim",
        _fake_read_claim,
    )

    orphaned = await regular_async_pool.find_orphaned_regular_async_pool_run_ids(client)

    assert orphaned == []


@pytest.mark.asyncio
async def test_find_orphaned_regular_async_pool_run_ids_returns_marked_running_rows(
    monkeypatch,
):
    client = _FakeClient(
        [
            {
                "agent_run_id": "run-pooled",
                "status": "running",
                "metadata": json.dumps(
                    {
                        "shadow_clone_mode": "off",
                        "regular_async_pool": True,
                    }
                ),
            },
            {
                "agent_run_id": "run-healthy",
                "status": "running",
                "metadata": json.dumps(
                    {
                        "shadow_clone_mode": "off",
                        "regular_async_pool": True,
                    }
                ),
            },
        ]
    )

    async def _fake_read_claim(agent_run_id: str) -> dict[str, object]:
        if agent_run_id == "run-healthy":
            return {"owner_token": "owner-1"}
        return {}

    monkeypatch.setattr(
        regular_async_pool,
        "_read_regular_run_claim",
        _fake_read_claim,
    )

    orphaned = await regular_async_pool.find_orphaned_regular_async_pool_run_ids(client)

    assert orphaned == ["run-pooled"]


@pytest.mark.asyncio
async def test_find_orphaned_regular_async_pool_run_ids_skips_fresh_running_rows(
    monkeypatch,
):
    client = _FakeClient(
        [
            {
                "agent_run_id": "run-fresh",
                "status": "running",
                "started_at": datetime.now(timezone.utc).isoformat(),
                "metadata": json.dumps(
                    {
                        "shadow_clone_mode": "off",
                        "regular_async_pool": True,
                    }
                ),
            }
        ]
    )

    async def _fake_read_claim(_agent_run_id: str) -> dict[str, object]:
        return {}

    monkeypatch.setattr(
        regular_async_pool,
        "_read_regular_run_claim",
        _fake_read_claim,
    )

    orphaned = await regular_async_pool.find_orphaned_regular_async_pool_run_ids(client)

    assert orphaned == []


@pytest.mark.asyncio
async def test_reconcile_expired_claim_marks_running_run_failed():
    rows = [
        {
            "agent_run_id": "run-1",
            "status": "running",
            "mode": "off",
        },
    ]
    client = _FakeClient(rows)

    updated_run_ids = await regular_async_pool.reconcile_expired_regular_run_claims(
        client,
        expired_run_ids=["run-1"],
    )

    assert updated_run_ids == ["run-1"]
    assert rows[0]["status"] == "failed"
    assert rows[0]["error"] == "regular_pool_owner_lost"


@pytest.mark.asyncio
async def test_regular_queue_depth_guard_rejects_when_limit_reached(monkeypatch):
    monkeypatch.setenv("REGULAR_QUEUE_MAX_DEPTH", "2")
    rows = [
        {"agent_run_id": "run-1", "status": "queued", "mode": "off"},
        {"agent_run_id": "run-2", "status": "queued", "mode": "off"},
    ]
    client = _FakeClient(rows)

    with pytest.raises(regular_async_pool.RegularQueueFullError):
        await regular_async_pool.ensure_regular_queue_capacity(
            client, additional_slots=1
        )


@pytest.mark.asyncio
async def test_regular_queue_depth_guard_ignores_non_regular_queued_rows(monkeypatch):
    monkeypatch.setenv("REGULAR_QUEUE_MAX_DEPTH", "1")
    rows = [
        {
            "agent_run_id": "run-shadow-clone",
            "status": "queued",
            "metadata": {"shadow_clone_mode": "auto"},
        },
    ]
    client = _FakeClient(rows)

    await regular_async_pool.ensure_regular_queue_capacity(client, additional_slots=1)


@pytest.mark.asyncio
async def test_run_dispatch_loop_drains_queue_while_respecting_pool_size(monkeypatch):
    monkeypatch.setenv("REGULAR_ASYNC_POOL_SIZE_PER_PROCESS", "2")
    rows = [
        {
            "agent_run_id": "run-1",
            "thread_id": "thread-1",
            "project_id": "project-1",
            "status": "queued",
            "created_at": 1,
            "metadata": json.dumps({"shadow_clone_mode": "off"}),
        },
        {
            "agent_run_id": "run-2",
            "thread_id": "thread-2",
            "project_id": "project-2",
            "status": "queued",
            "created_at": 2,
            "metadata": json.dumps({"shadow_clone_mode": "off"}),
        },
        {
            "agent_run_id": "run-3",
            "thread_id": "thread-3",
            "project_id": "project-3",
            "status": "queued",
            "created_at": 3,
            "metadata": json.dumps({"shadow_clone_mode": "off"}),
        },
    ]
    client = _FakeClient(rows)
    execution_order: list[str] = []
    current_concurrency = 0
    max_concurrency = 0

    async def _recording_executor(run_row: dict[str, object], owner_token: str) -> str:
        nonlocal current_concurrency, max_concurrency
        execution_order.append(f"{run_row['agent_run_id']}@{owner_token}")
        current_concurrency += 1
        max_concurrency = max(max_concurrency, current_concurrency)
        await asyncio.sleep(0.01)
        current_concurrency -= 1
        return "completed"

    completed = await regular_async_pool.run_dispatch_loop(
        client=client,
        executor=_recording_executor,
        owner_token="dispatcher-1",
    )

    assert sorted(completed) == ["run-1", "run-2", "run-3"]
    assert max_concurrency == 2
    assert execution_order == [
        "run-1@dispatcher-1",
        "run-2@dispatcher-1",
        "run-3@dispatcher-1",
    ]


@pytest.mark.asyncio
async def test_run_dispatch_loop_continues_after_executor_exception(monkeypatch):
    monkeypatch.setenv("REGULAR_ASYNC_POOL_SIZE_PER_PROCESS", "1")
    rows = [
        {
            "agent_run_id": "run-1",
            "thread_id": "thread-1",
            "project_id": "project-1",
            "status": "queued",
            "created_at": 1,
            "metadata": json.dumps({"shadow_clone_mode": "off"}),
        },
        {
            "agent_run_id": "run-2",
            "thread_id": "thread-2",
            "project_id": "project-2",
            "status": "queued",
            "created_at": 2,
            "metadata": json.dumps({"shadow_clone_mode": "off"}),
        },
    ]
    client = _FakeClient(rows)
    execution_order: list[str] = []

    async def _flaky_executor(run_row: dict[str, object], owner_token: str) -> str:
        execution_order.append(f"{run_row['agent_run_id']}@{owner_token}")
        await asyncio.sleep(0)
        if run_row["agent_run_id"] == "run-1":
            raise RuntimeError("synthetic failure")
        return "completed"

    completed = await regular_async_pool.run_dispatch_loop(
        client=client,
        executor=_flaky_executor,
        owner_token="dispatcher-1",
    )

    assert completed == ["run-1", "run-2"]
    assert execution_order == [
        "run-1@dispatcher-1",
        "run-2@dispatcher-1",
    ]


@pytest.mark.asyncio
async def test_run_dispatch_loop_refreshes_heartbeats_while_waiting(monkeypatch):
    monkeypatch.setenv("REGULAR_ASYNC_POOL_SIZE_PER_PROCESS", "1")
    monkeypatch.setenv("REGULAR_ASYNC_POOL_HEARTBEAT_INTERVAL_SECONDS", "0.01")
    rows = [
        {
            "agent_run_id": "run-1",
            "thread_id": "thread-1",
            "project_id": "project-1",
            "status": "queued",
            "created_at": 1,
            "metadata": json.dumps({"shadow_clone_mode": "off"}),
        }
    ]
    client = _FakeClient(rows)
    heartbeat_calls: list[tuple[str, str]] = []

    async def _record_heartbeat(
        agent_run_id: str, owner_token: str
    ) -> dict[str, object]:
        heartbeat_calls.append((agent_run_id, owner_token))
        return {
            "agent_run_id": agent_run_id,
            "owner_token": owner_token,
            "refreshed": True,
        }

    async def _slow_executor(_run_row: dict[str, object], _owner_token: str) -> str:
        await asyncio.sleep(0.03)
        return "completed"

    monkeypatch.setattr(
        regular_async_pool,
        "heartbeat_regular_run_claim",
        _record_heartbeat,
    )

    completed = await regular_async_pool.run_dispatch_loop(
        client=client,
        executor=_slow_executor,
        owner_token="dispatcher-1",
    )

    assert completed == ["run-1"]
    assert heartbeat_calls[0] == ("run-1", "dispatcher-1")
    assert len(heartbeat_calls) >= 2


@pytest.mark.asyncio
async def test_dispatcher_singleton_lock_skips_duplicate_dispatcher(monkeypatch):
    monkeypatch.setattr(
        regular_async_pool,
        "_DISPATCHER_LOCK_OWNER",
        None,
        raising=False,
    )

    first = await regular_async_pool.try_acquire_dispatcher_lock(
        owner_token="dispatcher-1"
    )
    second = await regular_async_pool.try_acquire_dispatcher_lock(
        owner_token="dispatcher-2"
    )

    assert first["acquired"] is True
    assert second["acquired"] is False


@pytest.mark.asyncio
async def test_schedule_idle_dispatch_wakeup_enqueues_delayed_actor(monkeypatch):
    monkeypatch.setenv("REGULAR_ASYNC_POOL_IDLE_RECONCILE_INTERVAL_SECONDS", "30")
    send_calls: list[dict[str, object]] = []

    async def _fake_set(
        key: str,
        value: str,
        ex: int | None = None,
        nx: bool = False,
        **_kwargs,
    ):
        assert key == "regular_async_pool:idle_dispatch_scheduled"
        assert value == "1"
        assert ex == 45
        assert nx is True
        return True

    class _FakeActor:
        def send_with_options(self, *, kwargs=None, delay=None, **_options):
            send_calls.append({"kwargs": kwargs, "delay": delay})

    monkeypatch.setattr(regular_async_pool.redis, "set", _fake_set)

    scheduled = await regular_async_pool.schedule_idle_dispatch_wakeup(
        actor_handle=_FakeActor(),
    )

    assert scheduled is True
    assert send_calls == [{"kwargs": {"trigger": "idle"}, "delay": 30000}]


@pytest.mark.asyncio
async def test_schedule_idle_dispatch_wakeup_skips_when_already_scheduled(
    monkeypatch,
):
    async def _fake_set(
        _key: str,
        _value: str,
        ex: int | None = None,
        nx: bool = False,
        **_kwargs,
    ):
        assert ex == 45
        assert nx is True
        return False

    class _FakeActor:
        def send_with_options(self, *, kwargs=None, delay=None, **_options):
            raise AssertionError("duplicate idle wakeup must not be enqueued")

    monkeypatch.setattr(regular_async_pool.redis, "set", _fake_set)

    scheduled = await regular_async_pool.schedule_idle_dispatch_wakeup(
        actor_handle=_FakeActor(),
    )

    assert scheduled is False


@pytest.mark.asyncio
async def test_schedule_idle_dispatch_wakeup_clears_key_when_enqueue_fails(
    monkeypatch,
):
    delete_calls: list[str] = []

    async def _fake_set(
        _key: str,
        _value: str,
        ex: int | None = None,
        nx: bool = False,
        **_kwargs,
    ):
        assert ex == 45
        assert nx is True
        return True

    async def _fake_delete(key: str, **_kwargs):
        delete_calls.append(key)
        return 1

    class _FakeActor:
        def send_with_options(self, *, kwargs=None, delay=None, **_options):
            raise RuntimeError("broker unavailable")

    monkeypatch.setattr(regular_async_pool.redis, "set", _fake_set)
    monkeypatch.setattr(regular_async_pool.redis, "delete", _fake_delete)

    scheduled = await regular_async_pool.schedule_idle_dispatch_wakeup(
        actor_handle=_FakeActor(),
    )

    assert scheduled is False
    assert delete_calls == ["regular_async_pool:idle_dispatch_scheduled"]


@pytest.mark.asyncio
async def test_regular_async_pool_dispatch_reconciles_orphaned_runs_before_and_after_drain(
    monkeypatch,
):
    client = _FakeClient([])
    call_order: list[object] = []

    async def _noop_initialize():
        return None

    async def _fake_try_acquire_dispatcher_lock(owner_token: str) -> dict[str, object]:
        call_order.append(("lock", owner_token))
        return {"acquired": True, "owner_token": owner_token}

    async def _fake_find_orphaned(_client) -> list[str]:
        call_order.append("find_orphaned")
        if call_order.count("find_orphaned") == 1:
            return ["run-orphaned"]
        return []

    async def _fake_reconcile(_client, expired_run_ids: list[str]) -> list[str]:
        call_order.append(("reconcile", list(expired_run_ids)))
        return list(expired_run_ids)

    async def _fake_run_dispatch_loop(**_kwargs) -> list[str]:
        call_order.append("dispatch_loop")
        return []

    async def _fake_release_dispatcher_lock(owner_token: str) -> dict[str, object]:
        call_order.append(("release", owner_token))
        return {"released": True}

    monkeypatch.setattr(run_agent_background_module, "initialize", _noop_initialize)
    monkeypatch.setattr(run_agent_background_module, "db", _DummyDB(client))
    monkeypatch.setattr(
        run_agent_background_module.regular_async_pool,
        "try_acquire_dispatcher_lock",
        _fake_try_acquire_dispatcher_lock,
    )
    monkeypatch.setattr(
        run_agent_background_module.regular_async_pool,
        "find_orphaned_regular_async_pool_run_ids",
        _fake_find_orphaned,
    )
    monkeypatch.setattr(
        run_agent_background_module.regular_async_pool,
        "reconcile_expired_regular_run_claims",
        _fake_reconcile,
    )
    monkeypatch.setattr(
        run_agent_background_module.regular_async_pool,
        "run_dispatch_loop",
        _fake_run_dispatch_loop,
    )
    monkeypatch.setattr(
        run_agent_background_module.regular_async_pool,
        "release_dispatcher_lock",
        _fake_release_dispatcher_lock,
    )

    await run_agent_background_module.regular_async_pool_dispatch()

    assert call_order[1:4] == [
        "find_orphaned",
        ("reconcile", ["run-orphaned"]),
        "dispatch_loop",
    ]
    assert call_order[-2:] == [
        "find_orphaned",
        ("release", call_order[0][1]),
    ]


@pytest.mark.asyncio
async def test_idle_dispatch_clears_idle_schedule_key_before_reconcile_and_reschedules(
    monkeypatch,
):
    client = _FakeClient([])
    call_order: list[object] = []

    async def _noop_initialize():
        return None

    async def _fake_try_acquire_dispatcher_lock(owner_token: str) -> dict[str, object]:
        call_order.append(("lock", owner_token))
        return {"acquired": True, "owner_token": owner_token}

    async def _fake_clear_idle_dispatch_wakeup() -> bool:
        call_order.append("clear_idle")
        return True

    async def _fake_find_orphaned(_client) -> list[str]:
        call_order.append("find_orphaned")
        return []

    async def _fake_run_dispatch_loop(**_kwargs) -> list[str]:
        call_order.append("dispatch_loop")
        return []

    async def _fake_release_dispatcher_lock(owner_token: str) -> dict[str, object]:
        call_order.append(("release", owner_token))
        return {"released": True}

    async def _fake_schedule_idle_dispatch_wakeup(*, actor_handle):
        call_order.append(("schedule_idle", actor_handle))
        return True

    monkeypatch.setattr(run_agent_background_module, "initialize", _noop_initialize)
    monkeypatch.setattr(run_agent_background_module, "db", _DummyDB(client))
    monkeypatch.setattr(
        run_agent_background_module.regular_async_pool,
        "try_acquire_dispatcher_lock",
        _fake_try_acquire_dispatcher_lock,
    )
    monkeypatch.setattr(
        run_agent_background_module.regular_async_pool,
        "clear_idle_dispatch_wakeup",
        _fake_clear_idle_dispatch_wakeup,
    )
    monkeypatch.setattr(
        run_agent_background_module.regular_async_pool,
        "find_orphaned_regular_async_pool_run_ids",
        _fake_find_orphaned,
    )
    monkeypatch.setattr(
        run_agent_background_module.regular_async_pool,
        "run_dispatch_loop",
        _fake_run_dispatch_loop,
    )
    monkeypatch.setattr(
        run_agent_background_module.regular_async_pool,
        "release_dispatcher_lock",
        _fake_release_dispatcher_lock,
    )
    monkeypatch.setattr(
        run_agent_background_module.regular_async_pool,
        "schedule_idle_dispatch_wakeup",
        _fake_schedule_idle_dispatch_wakeup,
    )
    monkeypatch.setattr(
        run_agent_background_module.regular_async_pool,
        "is_regular_async_pool_enabled",
        lambda _mode: True,
    )

    await run_agent_background_module.regular_async_pool_dispatch(trigger="idle")

    assert call_order[0] == "clear_idle"
    assert call_order[2:4] == ["find_orphaned", "dispatch_loop"]
    assert call_order[-1] == (
        "schedule_idle",
        run_agent_background_module.regular_async_pool_dispatch,
    )


@pytest.mark.asyncio
async def test_event_dispatch_does_not_clear_idle_schedule_key(monkeypatch):
    client = _FakeClient([])
    call_order: list[object] = []

    async def _noop_initialize():
        return None

    async def _fake_try_acquire_dispatcher_lock(owner_token: str) -> dict[str, object]:
        call_order.append(("lock", owner_token))
        return {"acquired": True, "owner_token": owner_token}

    async def _unexpected_clear_idle_dispatch_wakeup() -> bool:
        raise AssertionError("event trigger must not clear idle schedule key")

    async def _fake_find_orphaned(_client) -> list[str]:
        call_order.append("find_orphaned")
        return []

    async def _fake_run_dispatch_loop(**_kwargs) -> list[str]:
        call_order.append("dispatch_loop")
        return []

    async def _fake_release_dispatcher_lock(owner_token: str) -> dict[str, object]:
        call_order.append(("release", owner_token))
        return {"released": True}

    async def _fake_schedule_idle_dispatch_wakeup(*, actor_handle):
        call_order.append(("schedule_idle", actor_handle))
        return True

    monkeypatch.setattr(run_agent_background_module, "initialize", _noop_initialize)
    monkeypatch.setattr(run_agent_background_module, "db", _DummyDB(client))
    monkeypatch.setattr(
        run_agent_background_module.regular_async_pool,
        "try_acquire_dispatcher_lock",
        _fake_try_acquire_dispatcher_lock,
    )
    monkeypatch.setattr(
        run_agent_background_module.regular_async_pool,
        "clear_idle_dispatch_wakeup",
        _unexpected_clear_idle_dispatch_wakeup,
    )
    monkeypatch.setattr(
        run_agent_background_module.regular_async_pool,
        "find_orphaned_regular_async_pool_run_ids",
        _fake_find_orphaned,
    )
    monkeypatch.setattr(
        run_agent_background_module.regular_async_pool,
        "run_dispatch_loop",
        _fake_run_dispatch_loop,
    )
    monkeypatch.setattr(
        run_agent_background_module.regular_async_pool,
        "release_dispatcher_lock",
        _fake_release_dispatcher_lock,
    )
    monkeypatch.setattr(
        run_agent_background_module.regular_async_pool,
        "schedule_idle_dispatch_wakeup",
        _fake_schedule_idle_dispatch_wakeup,
    )
    monkeypatch.setattr(
        run_agent_background_module.regular_async_pool,
        "is_regular_async_pool_enabled",
        lambda _mode: True,
    )

    await run_agent_background_module.regular_async_pool_dispatch(trigger="event")

    assert "find_orphaned" in call_order
    assert call_order[-1] == (
        "schedule_idle",
        run_agent_background_module.regular_async_pool_dispatch,
    )


@pytest.mark.asyncio
async def test_dispatch_does_not_schedule_idle_wakeup_when_pool_disabled(monkeypatch):
    client = _FakeClient([])

    async def _noop_initialize():
        return None

    async def _fake_try_acquire_dispatcher_lock(owner_token: str) -> dict[str, object]:
        return {"acquired": True, "owner_token": owner_token}

    async def _fake_find_orphaned(_client) -> list[str]:
        return []

    async def _fake_run_dispatch_loop(**_kwargs) -> list[str]:
        return []

    async def _fake_release_dispatcher_lock(owner_token: str) -> dict[str, object]:
        return {"released": True}

    async def _unexpected_schedule_idle_dispatch_wakeup(*, actor_handle):
        raise AssertionError("pool-disabled path must not schedule idle wakeups")

    monkeypatch.setattr(run_agent_background_module, "initialize", _noop_initialize)
    monkeypatch.setattr(run_agent_background_module, "db", _DummyDB(client))
    monkeypatch.setattr(
        run_agent_background_module.regular_async_pool,
        "try_acquire_dispatcher_lock",
        _fake_try_acquire_dispatcher_lock,
    )
    monkeypatch.setattr(
        run_agent_background_module.regular_async_pool,
        "find_orphaned_regular_async_pool_run_ids",
        _fake_find_orphaned,
    )
    monkeypatch.setattr(
        run_agent_background_module.regular_async_pool,
        "run_dispatch_loop",
        _fake_run_dispatch_loop,
    )
    monkeypatch.setattr(
        run_agent_background_module.regular_async_pool,
        "release_dispatcher_lock",
        _fake_release_dispatcher_lock,
    )
    monkeypatch.setattr(
        run_agent_background_module.regular_async_pool,
        "schedule_idle_dispatch_wakeup",
        _unexpected_schedule_idle_dispatch_wakeup,
    )
    monkeypatch.setattr(
        run_agent_background_module.regular_async_pool,
        "is_regular_async_pool_enabled",
        lambda _mode: False,
    )

    await run_agent_background_module.regular_async_pool_dispatch(trigger="event")


@pytest.mark.asyncio
async def test_agent_api_initialize_bootstraps_regular_async_pool_dispatch_when_enabled(
    monkeypatch,
):
    dispatch_calls: list[str] = []

    monkeypatch.setattr(
        agent_api.regular_async_pool,
        "is_regular_async_pool_enabled",
        lambda _mode: True,
    )
    monkeypatch.setattr(
        agent_api,
        "regular_async_pool_dispatch",
        SimpleNamespace(send=lambda: dispatch_calls.append("dispatch")),
        raising=False,
    )

    agent_api.initialize(_DummyDB(_FakeAgentApiClient()), "test-instance")

    assert dispatch_calls == ["dispatch"]


@pytest.mark.asyncio
async def test_agent_api_initialize_skips_regular_async_pool_bootstrap_when_disabled(
    monkeypatch,
):
    monkeypatch.setattr(
        agent_api.regular_async_pool,
        "is_regular_async_pool_enabled",
        lambda _mode: False,
    )
    monkeypatch.setattr(
        agent_api,
        "regular_async_pool_dispatch",
        SimpleNamespace(
            send=lambda: (_ for _ in ()).throw(
                AssertionError("disabled path must not bootstrap dispatch")
            )
        ),
        raising=False,
    )

    agent_api.initialize(_DummyDB(_FakeAgentApiClient()), "test-instance")


@pytest.mark.asyncio
async def test_start_agent_creates_queued_regular_run_when_pool_flag_enabled(
    monkeypatch,
):
    monkeypatch.delenv("REGULAR_EXECUTION_MODE", raising=False)
    monkeypatch.setenv("REGULAR_ASYNC_POOL_ENABLED", "true")
    harness = _install_start_agent_queue_harness(monkeypatch)

    result = await agent_api.start_agent(
        thread_id="thread-1",
        body=agent_api.AgentStartRequest(
            model_name="gpt-test",
            shadow_clone_mode="off",
            agent_id="agent-1",
        ),
        user_id="user-1",
    )

    assert result["status"] == "queued"
    assert harness["queue_capacity_calls"] == [
        {"client": harness["client"], "additional_slots": 1}
    ]
    assert harness["client"].agent_run_inserts[0]["status"] == "queued"
    queued_metadata = json.loads(harness["client"].agent_run_inserts[0]["metadata"])
    assert queued_metadata["regular_async_pool"] is True
    assert harness["dispatch_calls"] == ["regular_async_pool_dispatch"]
    assert harness["send_calls"] == []


@pytest.mark.asyncio
async def test_start_agent_returns_429_when_regular_queue_depth_is_exhausted(
    monkeypatch,
):
    monkeypatch.delenv("REGULAR_EXECUTION_MODE", raising=False)
    monkeypatch.setenv("REGULAR_ASYNC_POOL_ENABLED", "true")
    harness = _install_start_agent_queue_harness(monkeypatch, queue_full=True)

    with pytest.raises(HTTPException) as exc_info:
        await agent_api.start_agent(
            thread_id="thread-1",
            body=agent_api.AgentStartRequest(
                model_name="gpt-test",
                shadow_clone_mode="off",
                agent_id="agent-1",
            ),
            user_id="user-1",
        )

    assert exc_info.value.status_code == 429
    assert "regular queue depth exhausted" in str(exc_info.value.detail)
    assert harness["client"].agent_run_inserts == []


@pytest.mark.asyncio
async def test_start_agent_flag_off_preserves_legacy_running_dispatch(monkeypatch):
    monkeypatch.setenv("REGULAR_EXECUTION_MODE", "legacy")
    monkeypatch.delenv("REGULAR_ASYNC_POOL_ENABLED", raising=False)
    harness = _install_start_agent_legacy_harness(monkeypatch)

    result = await agent_api.start_agent(
        thread_id="thread-1",
        body=agent_api.AgentStartRequest(
            model_name="gpt-test",
            shadow_clone_mode="off",
            agent_id="agent-1",
        ),
        user_id="user-1",
    )

    assert result["status"] == "running"
    assert harness["snapshot_calls"] == [{"mode": "off", "requested_cost": 1}]
    assert len(harness["reservation_calls"]) == 1
    assert harness["client"].agent_run_inserts[0]["status"] == "running"
    assert len(harness["send_calls"]) == 1
    assert harness["dispatch_calls"] == []
    assert len(harness["redis_set_calls"]) == 1


@pytest.mark.asyncio
async def test_start_agent_phase2_supervisor_creates_queued_attempt(monkeypatch):
    monkeypatch.setenv("REGULAR_EXECUTION_MODE", "phase2_supervisor")
    monkeypatch.delenv("REGULAR_ASYNC_POOL_ENABLED", raising=False)
    harness = _install_start_agent_phase2_harness(monkeypatch)

    result = await agent_api.start_agent(
        thread_id="thread-1",
        body=agent_api.AgentStartRequest(
            model_name="gpt-test",
            shadow_clone_mode="off",
            agent_id="agent-1",
        ),
        user_id="user-1",
    )

    assert result["status"] == "queued"
    assert harness["queue_capacity_calls"] == [
        {"client": harness["client"], "additional_slots": 1}
    ]
    assert harness["attempt_create_calls"] == [result["agent_run_id"]]
    assert harness["client"].agent_run_inserts[0]["status"] == "queued"
    queued_metadata = json.loads(harness["client"].agent_run_inserts[0]["metadata"])
    assert queued_metadata["regular_execution_mode"] == "phase2_supervisor"
    assert queued_metadata["agent_config_snapshot"]["agent_id"] == "agent-1"
    assert "regular_async_pool" not in queued_metadata
    assert harness["dispatch_calls"] == ["regular_supervisor_dispatch"]
    assert harness["send_calls"] == []


@pytest.mark.asyncio
async def test_start_agent_shadow_clone_v2_auto_queues_phase2_supervisor_attempt(
    monkeypatch,
):
    monkeypatch.delenv("SHADOW_CLONE_V2_EXECUTION_CHAIN", raising=False)
    monkeypatch.delenv("REGULAR_EXECUTION_MODE", raising=False)
    monkeypatch.delenv("REGULAR_ASYNC_POOL_ENABLED", raising=False)
    harness = _install_start_agent_phase2_harness(monkeypatch)

    result = await agent_api.start_agent(
        thread_id="thread-1",
        body=agent_api.AgentStartRequest(
            model_name="gpt-test",
            shadow_clone_mode="auto",
            agent_id="agent-1",
        ),
        user_id="user-1",
    )

    assert result["status"] == "queued"
    assert harness["queue_capacity_calls"] == [
        {"client": harness["client"], "additional_slots": 1}
    ]
    assert harness["attempt_create_calls"] == [result["agent_run_id"]]
    queued_metadata = json.loads(harness["client"].agent_run_inserts[0]["metadata"])
    assert queued_metadata["regular_execution_mode"] == "phase2_supervisor"
    assert queued_metadata["shadow_clone_mode"] == "auto"
    assert queued_metadata["shadow_clone_runtime"] == "v2"
    assert queued_metadata["shadow_clone_v2_execution_chain"] == "regular_supervisor"
    assert harness["dispatch_calls"] == ["regular_supervisor_dispatch"]
    assert harness["send_calls"] == []


@pytest.mark.asyncio
async def test_start_agent_shadow_clone_v2_chain_fallback_uses_run_agent_background(
    monkeypatch,
):
    monkeypatch.setenv("SHADOW_CLONE_V2_EXECUTION_CHAIN", "run_agent_background")
    monkeypatch.setenv("REGULAR_EXECUTION_MODE", "phase2_supervisor")
    harness = _install_start_agent_legacy_harness(monkeypatch)

    result = await agent_api.start_agent(
        thread_id="thread-1",
        body=agent_api.AgentStartRequest(
            model_name="gpt-test",
            shadow_clone_mode="on",
            agent_id="agent-1",
        ),
        user_id="user-1",
    )

    assert result["status"] == "running"
    inserted_metadata = json.loads(harness["client"].agent_run_inserts[0]["metadata"])
    assert inserted_metadata["shadow_clone_mode"] == "on"
    assert inserted_metadata["shadow_clone_runtime"] == "v2"
    assert inserted_metadata["shadow_clone_v2_execution_chain"] == "run_agent_background"
    assert "regular_execution_mode" not in inserted_metadata
    assert len(harness["send_calls"]) == 1
    assert harness["dispatch_calls"] == []


def test_regular_supervisor_dispatch_handle_enqueues_background_actor(monkeypatch):
    send_calls = []
    monkeypatch.setenv("REGULAR_SUPERVISOR_PERSISTENT", "false")

    monkeypatch.setattr(
        agent_api,
        "regular_supervisor_background_actor",
        SimpleNamespace(
            send=lambda **kwargs: send_calls.append(dict(kwargs)),
        ),
        raising=False,
    )

    dispatch_handle = agent_api._RegularSupervisorDispatchHandle()
    dispatch_handle.send()

    assert len(send_calls) == 1
    assert str(send_calls[0]["supervisor_id"]).startswith("regular-supervisor:")


@pytest.mark.asyncio
async def test_regular_supervisor_dispatch_handle_enqueues_coalesced_wakeup_when_persistent_mode_enabled(
    monkeypatch,
):
    send_calls = []
    monkeypatch.setenv("REGULAR_SUPERVISOR_PERSISTENT", "true")
    monkeypatch.setenv("REGULAR_SUPERVISOR_SHARD_PREFIX", "wm-phase3")
    wakeup_reservations = []

    async def _fake_set(key, value, ex=None, nx=False, **_kwargs):
        wakeup_reservations.append((key, value, ex, nx))
        return True

    monkeypatch.setattr(
        agent_api,
        "regular_supervisor_background_actor",
        SimpleNamespace(
            send=lambda **kwargs: send_calls.append(dict(kwargs)),
        ),
        raising=False,
    )
    monkeypatch.setattr(agent_api.redis, "set", _fake_set, raising=False)

    dispatch_handle = agent_api._RegularSupervisorDispatchHandle()
    dispatch_handle.send()
    await asyncio.sleep(0)

    assert wakeup_reservations == [
        (
            "wm-phase3:wakeup",
            agent_api.PERSISTENT_SUPERVISOR_WAKEUP_SUPERVISOR_ID,
            30,
            True,
        )
    ]
    assert send_calls == [
        {
            "supervisor_id": agent_api.PERSISTENT_SUPERVISOR_WAKEUP_SUPERVISOR_ID,
        }
    ]


@pytest.mark.asyncio
async def test_regular_supervisor_dispatch_handle_skips_duplicate_persistent_wakeup_when_reservation_exists(
    monkeypatch,
):
    send_calls = []
    monkeypatch.setenv("REGULAR_SUPERVISOR_PERSISTENT", "true")

    async def _fake_set(*_args, **_kwargs):
        return False

    monkeypatch.setattr(
        agent_api,
        "regular_supervisor_background_actor",
        SimpleNamespace(
            send=lambda **kwargs: send_calls.append(dict(kwargs)),
        ),
        raising=False,
    )
    monkeypatch.setattr(agent_api.redis, "set", _fake_set, raising=False)

    dispatch_handle = agent_api._RegularSupervisorDispatchHandle()
    dispatch_handle.send()
    await asyncio.sleep(0)

    assert send_calls == []


@pytest.mark.asyncio
async def test_start_agent_phase2_dispatch_failure_marks_initial_attempt_failed(
    monkeypatch,
):
    monkeypatch.setenv("REGULAR_EXECUTION_MODE", "phase2_supervisor")
    monkeypatch.delenv("REGULAR_ASYNC_POOL_ENABLED", raising=False)
    harness = _install_start_agent_phase2_harness(
        monkeypatch,
        dispatch_should_fail=True,
    )

    with pytest.raises(RuntimeError, match="phase2 dispatch failed"):
        await agent_api.start_agent(
            thread_id="thread-1",
            body=agent_api.AgentStartRequest(
                model_name="gpt-test",
                shadow_clone_mode="off",
                agent_id="agent-1",
            ),
            user_id="user-1",
        )

    assert harness["dispatch_calls"] == ["regular_supervisor_dispatch"]
    assert harness["client"].regular_attempt_rows[0]["status"] == "failed"
    assert harness["client"].regular_attempt_rows[0]["terminal_status"] == "failed"
    assert harness["update_status_calls"] == [
        {
            "agent_run_id": harness["client"].agent_run_rows[0]["agent_run_id"],
            "status": "failed",
            "error": "Failed to dispatch agent run: phase2 dispatch failed",
        }
    ]


@pytest.mark.asyncio
async def test_initiate_agent_with_files_creates_queued_regular_run_when_pool_flag_enabled(
    monkeypatch,
):
    monkeypatch.delenv("REGULAR_EXECUTION_MODE", raising=False)
    monkeypatch.setenv("REGULAR_ASYNC_POOL_ENABLED", "true")
    harness = _install_initiate_queue_harness(monkeypatch)

    result = await agent_api.initiate_agent_with_files(
        prompt="Queue this regular run",
        model_name=None,
        enable_thinking=False,
        reasoning_effort="low",
        stream=True,
        enable_context_manager=False,
        shadow_clone_mode="off",
        shadow_clone_main_model=None,
        shadow_clone_subagent_model=None,
        agent_id="agent-1",
        project_id="proj-1",
        prepared_attachments_json=None,
        files=[],
        is_agent_builder=False,
        target_agent_id=None,
        user_id="user-1",
    )

    assert result["thread_id"]
    assert result["agent_run_id"]
    assert harness["queue_capacity_calls"] == [
        {"client": harness["client"], "additional_slots": 1}
    ]
    assert harness["client"].agent_run_inserts[0]["status"] == "queued"
    queued_metadata = json.loads(harness["client"].agent_run_inserts[0]["metadata"])
    assert queued_metadata["regular_async_pool"] is True
    assert harness["dispatch_calls"] == ["regular_async_pool_dispatch"]
    assert harness["send_calls"] == []


@pytest.mark.asyncio
async def test_initiate_agent_with_files_phase2_supervisor_creates_queued_attempt(
    monkeypatch,
):
    monkeypatch.setenv("REGULAR_EXECUTION_MODE", "phase2_supervisor")
    monkeypatch.delenv("REGULAR_ASYNC_POOL_ENABLED", raising=False)
    harness = _install_initiate_phase2_harness(monkeypatch)

    result = await agent_api.initiate_agent_with_files(
        prompt="Queue this phase2 regular run",
        model_name=None,
        enable_thinking=False,
        reasoning_effort="low",
        stream=True,
        enable_context_manager=False,
        shadow_clone_mode="off",
        shadow_clone_main_model=None,
        shadow_clone_subagent_model=None,
        agent_id="agent-1",
        project_id="proj-1",
        prepared_attachments_json=None,
        files=[],
        is_agent_builder=False,
        target_agent_id=None,
        user_id="user-1",
    )

    assert result["thread_id"]
    assert result["agent_run_id"]
    assert harness["queue_capacity_calls"] == [
        {"client": harness["client"], "additional_slots": 1}
    ]
    assert harness["attempt_create_calls"] == [result["agent_run_id"]]
    assert harness["client"].agent_run_inserts[0]["status"] == "queued"
    queued_metadata = json.loads(harness["client"].agent_run_inserts[0]["metadata"])
    assert queued_metadata["regular_execution_mode"] == "phase2_supervisor"
    assert "regular_async_pool" not in queued_metadata
    assert harness["dispatch_calls"] == ["regular_supervisor_dispatch"]
    assert harness["send_calls"] == []


@pytest.mark.asyncio
async def test_initiate_agent_with_files_shadow_clone_v2_on_queues_phase2_supervisor(
    monkeypatch,
):
    monkeypatch.delenv("SHADOW_CLONE_V2_EXECUTION_CHAIN", raising=False)
    monkeypatch.delenv("REGULAR_EXECUTION_MODE", raising=False)
    monkeypatch.delenv("REGULAR_ASYNC_POOL_ENABLED", raising=False)
    harness = _install_initiate_phase2_harness(monkeypatch)

    result = await agent_api.initiate_agent_with_files(
        prompt="Use multiple teammates",
        model_name="gpt-test",
        enable_thinking=False,
        reasoning_effort="low",
        stream=True,
        enable_context_manager=False,
        shadow_clone_mode="on",
        shadow_clone_main_model=None,
        shadow_clone_subagent_model=None,
        agent_id="agent-1",
        project_id="proj-1",
        prepared_attachments_json=None,
        files=[],
        is_agent_builder=False,
        target_agent_id=None,
        user_id="user-1",
    )

    assert result["thread_id"]
    assert result["agent_run_id"]
    assert harness["attempt_create_calls"] == [result["agent_run_id"]]
    queued_metadata = json.loads(harness["client"].agent_run_inserts[0]["metadata"])
    assert queued_metadata["regular_execution_mode"] == "phase2_supervisor"
    assert queued_metadata["shadow_clone_mode"] == "on"
    assert queued_metadata["shadow_clone_runtime"] == "v2"
    assert queued_metadata["shadow_clone_v2_execution_chain"] == "regular_supervisor"
    assert harness["dispatch_calls"] == ["regular_supervisor_dispatch"]
    assert harness["send_calls"] == []


@pytest.mark.asyncio
async def test_initiate_agent_with_files_normalizes_deepseek_max_reasoning_metadata(monkeypatch):
    monkeypatch.setenv("REGULAR_EXECUTION_MODE", "phase2_supervisor")
    monkeypatch.delenv("REGULAR_ASYNC_POOL_ENABLED", raising=False)
    harness = _install_initiate_phase2_harness(monkeypatch)

    await agent_api.initiate_agent_with_files(
        prompt="Run with max reasoning",
        model_name="deepseek-v4-pro-max",
        enable_thinking=False,
        reasoning_effort="low",
        stream=False,
        enable_context_manager=False,
        shadow_clone_mode="off",
        shadow_clone_main_model=None,
        shadow_clone_subagent_model=None,
        agent_id="agent-1",
        project_id="proj-1",
        prepared_attachments_json=None,
        files=[],
        is_agent_builder=False,
        target_agent_id=None,
        user_id="user-1",
    )

    queued_metadata = json.loads(harness["client"].agent_run_inserts[0]["metadata"])
    assert queued_metadata["model_name"] == "deepseek-v4-pro-max"
    assert queued_metadata["requested_model"] == "deepseek-v4-pro-max"
    assert queued_metadata["enable_thinking"] is True
    assert queued_metadata["reasoning_effort"] == "max"


@pytest.mark.asyncio
async def test_initiate_agent_with_files_phase2_dispatch_failure_marks_initial_attempt_failed(
    monkeypatch,
):
    monkeypatch.setenv("REGULAR_EXECUTION_MODE", "phase2_supervisor")
    monkeypatch.delenv("REGULAR_ASYNC_POOL_ENABLED", raising=False)
    harness = _install_initiate_phase2_harness(
        monkeypatch,
        dispatch_should_fail=True,
    )

    with pytest.raises(HTTPException) as exc_info:
        await agent_api.initiate_agent_with_files(
            prompt="Queue this phase2 regular run",
            model_name=None,
            enable_thinking=False,
            reasoning_effort="low",
            stream=True,
            enable_context_manager=False,
            shadow_clone_mode="off",
            shadow_clone_main_model=None,
            shadow_clone_subagent_model=None,
            agent_id="agent-1",
            project_id="proj-1",
            prepared_attachments_json=None,
            files=[],
            is_agent_builder=False,
            target_agent_id=None,
            user_id="user-1",
        )

    assert exc_info.value.status_code == 500
    assert "phase2 dispatch failed" in str(exc_info.value.detail)
    assert harness["dispatch_calls"] == ["regular_supervisor_dispatch"]
    assert harness["client"].regular_attempt_rows[0]["status"] == "failed"
    assert harness["client"].regular_attempt_rows[0]["terminal_status"] == "failed"
    assert harness["update_status_calls"] == [
        {
            "agent_run_id": harness["client"].agent_run_rows[0]["agent_run_id"],
            "status": "failed",
            "error": "Failed to dispatch agent run: phase2 dispatch failed",
        }
    ]


@pytest.mark.asyncio
async def test_stop_agent_run_marks_queued_regular_run_stopped_without_stop_publish(
    monkeypatch,
):
    client = _FakeAgentApiClient(
        agent_run_rows=[
            {
                "agent_run_id": "run-queued",
                "status": "queued",
                "metadata": json.dumps({"shadow_clone_mode": "off"}),
            }
        ]
    )
    update_status_calls = []
    publish_calls = []

    class _FakeAgentDB:
        @property
        async def client(self):
            return client

    async def _fake_update_status(_client, agent_run_id: str, status: str, error=None):
        update_status_calls.append(
            {
                "agent_run_id": agent_run_id,
                "status": status,
                "error": error,
            }
        )
        return True

    async def _unexpected_force_terminal(*_args, **_kwargs):
        raise AssertionError("queued regular stop must not sync active shadow state")

    async def _unexpected_update_lease(*_args, **_kwargs):
        raise AssertionError("queued regular stop must not touch sandbox lease")

    async def _fake_publish(channel: str, message: str):
        publish_calls.append((channel, message))
        return 1

    async def _unexpected_scan_keys(*_args, **_kwargs):
        raise AssertionError("queued regular stop must not scan active_run keys")

    async def _unexpected_cleanup(*_args, **_kwargs):
        raise AssertionError("queued regular stop must not clean Redis response list")

    monkeypatch.setattr(agent_api, "db", _FakeAgentDB())
    monkeypatch.setattr(agent_api, "update_agent_run_status", _fake_update_status)
    monkeypatch.setattr(agent_api, "force_terminal_state", _unexpected_force_terminal)
    monkeypatch.setattr(agent_api, "update_run_sandbox_lease", _unexpected_update_lease)
    monkeypatch.setattr(agent_api.redis, "publish", _fake_publish)
    monkeypatch.setattr(
        agent_api.redis, "scan_keys", _unexpected_scan_keys, raising=False
    )
    monkeypatch.setattr(agent_api, "_cleanup_redis_response_list", _unexpected_cleanup)

    await agent_api.stop_agent_run("run-queued")

    assert update_status_calls == [
        {
            "agent_run_id": "run-queued",
            "status": "stopped",
            "error": None,
        }
    ]
    assert publish_calls == []


@pytest.mark.asyncio
async def test_stop_agent_run_marks_queued_phase2_run_and_attempt_stopped_without_stop_publish(
    monkeypatch,
):
    client = _FakeAgentApiClient(
        agent_run_rows=[
            {
                "agent_run_id": "run-phase2-queued",
                "status": "queued",
                "metadata": json.dumps(
                    {
                        "shadow_clone_mode": "off",
                        "regular_execution_mode": "phase2_supervisor",
                    }
                ),
            }
        ],
        regular_attempt_rows=[
            {
                "attempt_id": "attempt-1",
                "agent_run_id": "run-phase2-queued",
                "status": "queued",
            }
        ],
    )
    update_status_calls = []
    publish_calls = []

    class _FakeAgentDB:
        @property
        async def client(self):
            return client

    async def _fake_update_status(_client, agent_run_id: str, status: str, error=None):
        update_status_calls.append(
            {
                "agent_run_id": agent_run_id,
                "status": status,
                "error": error,
            }
        )
        return True

    async def _unexpected_force_terminal(*_args, **_kwargs):
        raise AssertionError(
            "queued phase2 regular stop must not sync active shadow state"
        )

    async def _unexpected_update_lease(*_args, **_kwargs):
        raise AssertionError("queued phase2 regular stop must not touch sandbox lease")

    async def _fake_publish(channel: str, message: str):
        publish_calls.append((channel, message))
        return 1

    async def _unexpected_scan_keys(*_args, **_kwargs):
        raise AssertionError("queued phase2 regular stop must not scan active_run keys")

    async def _unexpected_cleanup(*_args, **_kwargs):
        raise AssertionError(
            "queued phase2 regular stop must not clean Redis response list"
        )

    monkeypatch.setattr(agent_api, "db", _FakeAgentDB())
    monkeypatch.setattr(agent_api, "update_agent_run_status", _fake_update_status)
    monkeypatch.setattr(agent_api, "force_terminal_state", _unexpected_force_terminal)
    monkeypatch.setattr(agent_api, "update_run_sandbox_lease", _unexpected_update_lease)
    monkeypatch.setattr(agent_api.redis, "publish", _fake_publish)
    monkeypatch.setattr(
        agent_api.redis, "scan_keys", _unexpected_scan_keys, raising=False
    )
    monkeypatch.setattr(agent_api, "_cleanup_redis_response_list", _unexpected_cleanup)

    await agent_api.stop_agent_run("run-phase2-queued")

    assert update_status_calls == [
        {
            "agent_run_id": "run-phase2-queued",
            "status": "stopped",
            "error": None,
        }
    ]
    assert client.regular_attempt_rows[0]["status"] == "stopped"
    assert client.regular_attempt_rows[0]["terminal_status"] == "stopped"
    assert publish_calls == []


@pytest.mark.asyncio
async def test_stop_agent_run_marks_queued_shadow_clone_v2_phase2_attempt_stopped_without_legacy_shadow_sync(
    monkeypatch,
):
    client = _FakeAgentApiClient(
        agent_run_rows=[
            {
                "agent_run_id": "run-v2-phase2-queued",
                "status": "queued",
                "metadata": json.dumps(
                    {
                        "shadow_clone_mode": "on",
                        "shadow_clone_runtime": "v2",
                        "shadow_clone_v2_execution_chain": "regular_supervisor",
                        "regular_execution_mode": "phase2_supervisor",
                    }
                ),
            }
        ],
        regular_attempt_rows=[
            {
                "attempt_id": "attempt-v2-queued",
                "agent_run_id": "run-v2-phase2-queued",
                "status": "queued",
            }
        ],
    )
    update_status_calls = []
    publish_calls = []

    class _FakeAgentDB:
        @property
        async def client(self):
            return client

    async def _fake_update_status(_client, agent_run_id: str, status: str, error=None):
        update_status_calls.append(
            {
                "agent_run_id": agent_run_id,
                "status": status,
                "error": error,
            }
        )
        return True

    async def _unexpected_force_terminal(*_args, **_kwargs):
        raise AssertionError(
            "queued V2 phase2 stop must not sync legacy shadow state"
        )

    async def _unexpected_update_lease(*_args, **_kwargs):
        raise AssertionError("queued V2 phase2 stop must not touch sandbox lease")

    async def _fake_publish(channel: str, message: str):
        publish_calls.append((channel, message))
        return 1

    async def _unexpected_scan_keys(*_args, **_kwargs):
        raise AssertionError("queued V2 phase2 stop must not scan active_run keys")

    async def _unexpected_cleanup(*_args, **_kwargs):
        raise AssertionError(
            "queued V2 phase2 stop must not clean Redis response list"
        )

    monkeypatch.setattr(agent_api, "db", _FakeAgentDB())
    monkeypatch.setattr(agent_api, "update_agent_run_status", _fake_update_status)
    monkeypatch.setattr(agent_api, "force_terminal_state", _unexpected_force_terminal)
    monkeypatch.setattr(agent_api, "update_run_sandbox_lease", _unexpected_update_lease)
    monkeypatch.setattr(agent_api.redis, "publish", _fake_publish)
    monkeypatch.setattr(
        agent_api.redis, "scan_keys", _unexpected_scan_keys, raising=False
    )
    monkeypatch.setattr(agent_api, "_cleanup_redis_response_list", _unexpected_cleanup)

    await agent_api.stop_agent_run("run-v2-phase2-queued")

    assert update_status_calls == [
        {
            "agent_run_id": "run-v2-phase2-queued",
            "status": "stopped",
            "error": None,
        }
    ]
    assert client.regular_attempt_rows[0]["status"] == "stopped"
    assert client.regular_attempt_rows[0]["terminal_status"] == "stopped"
    assert publish_calls == []


@pytest.mark.asyncio
async def test_stop_agent_run_marks_active_phase2_attempt_stopped_and_publishes_stop(
    monkeypatch,
):
    client = _FakeAgentApiClient(
        agent_run_rows=[
            {
                "agent_run_id": "run-phase2-active",
                "status": "running",
                "metadata": json.dumps(
                    {
                        "shadow_clone_mode": "off",
                        "regular_execution_mode": "phase2_supervisor",
                    }
                ),
            }
        ],
        regular_attempt_rows=[
            {
                "attempt_id": "attempt-active",
                "agent_run_id": "run-phase2-active",
                "attempt_number": 1,
                "execution_epoch": 1,
                "status": "running",
            }
        ],
    )
    update_status_calls = []
    publish_calls = []
    cleanup_calls = []

    class _FakeAgentDB:
        @property
        async def client(self):
            return client

    async def _fake_update_status(_client, agent_run_id: str, status: str, error=None):
        update_status_calls.append(
            {
                "agent_run_id": agent_run_id,
                "status": status,
                "error": error,
            }
        )
        for row in client.agent_run_rows:
            if row.get("agent_run_id") == agent_run_id:
                row["status"] = status
        return True

    async def _unexpected_force_terminal(*_args, **_kwargs):
        raise AssertionError("regular phase2 stop must not sync shadow clone state")

    async def _unexpected_update_lease(*_args, **_kwargs):
        raise AssertionError("regular phase2 stop must not touch sandbox lease")

    async def _fake_publish(channel: str, message: str):
        publish_calls.append((channel, message))
        return 1

    async def _fake_scan_keys(pattern: str):
        if pattern == "active_run:*:run-phase2-active":
            return ["active_run:worker-1:run-phase2-active"]
        return []

    async def _fake_cleanup(run_id: str, **_kwargs):
        cleanup_calls.append(run_id)
        return None

    monkeypatch.setattr(agent_api, "db", _FakeAgentDB())
    monkeypatch.setattr(agent_api, "update_agent_run_status", _fake_update_status)
    monkeypatch.setattr(agent_api, "force_terminal_state", _unexpected_force_terminal)
    monkeypatch.setattr(agent_api, "update_run_sandbox_lease", _unexpected_update_lease)
    monkeypatch.setattr(agent_api.redis, "publish", _fake_publish)
    monkeypatch.setattr(agent_api.redis, "scan_keys", _fake_scan_keys, raising=False)
    monkeypatch.setattr(agent_api, "_cleanup_redis_response_list", _fake_cleanup)

    await agent_api.stop_agent_run("run-phase2-active")

    assert update_status_calls == [
        {
            "agent_run_id": "run-phase2-active",
            "status": "stopped",
            "error": None,
        }
    ]
    assert client.regular_attempt_rows[0]["status"] == "stopped"
    assert client.regular_attempt_rows[0]["terminal_status"] == "stopped"
    assert client.regular_attempt_rows[0]["terminal_reason"] == "external_stop"
    assert publish_calls == [
        ("agent_run:run-phase2-active:control", "STOP"),
        ("agent_run:run-phase2-active:control:worker-1", "STOP"),
    ]
    assert cleanup_calls == ["run-phase2-active"]


@pytest.mark.asyncio
async def test_stop_agent_run_marks_active_shadow_clone_v2_phase2_attempt_stopped_without_legacy_shadow_sync(
    monkeypatch,
):
    client = _FakeAgentApiClient(
        agent_run_rows=[
            {
                "agent_run_id": "run-v2-phase2-active",
                "status": "running",
                "metadata": json.dumps(
                    {
                        "shadow_clone_mode": "on",
                        "shadow_clone_runtime": "v2",
                        "shadow_clone_v2_execution_chain": "regular_supervisor",
                        "regular_execution_mode": "phase2_supervisor",
                    }
                ),
            }
        ],
        regular_attempt_rows=[
            {
                "attempt_id": "attempt-v2-active",
                "agent_run_id": "run-v2-phase2-active",
                "attempt_number": 1,
                "execution_epoch": 1,
                "status": "running",
            }
        ],
    )
    update_status_calls = []
    publish_calls = []
    cleanup_calls = []

    class _FakeAgentDB:
        @property
        async def client(self):
            return client

    async def _fake_update_status(_client, agent_run_id: str, status: str, error=None):
        update_status_calls.append(
            {
                "agent_run_id": agent_run_id,
                "status": status,
                "error": error,
            }
        )
        for row in client.agent_run_rows:
            if row.get("agent_run_id") == agent_run_id:
                row["status"] = status
        return True

    async def _unexpected_force_terminal(*_args, **_kwargs):
        raise AssertionError("active V2 phase2 stop must not sync legacy shadow state")

    async def _unexpected_update_lease(*_args, **_kwargs):
        raise AssertionError("active V2 phase2 stop must not touch sandbox lease")

    async def _fake_publish(channel: str, message: str):
        publish_calls.append((channel, message))
        return 1

    async def _fake_scan_keys(pattern: str):
        if pattern == "active_run:*:run-v2-phase2-active":
            return ["active_run:worker-1:run-v2-phase2-active"]
        return []

    async def _fake_cleanup(run_id: str, **_kwargs):
        cleanup_calls.append(run_id)
        return None

    monkeypatch.setattr(agent_api, "db", _FakeAgentDB())
    monkeypatch.setattr(agent_api, "update_agent_run_status", _fake_update_status)
    monkeypatch.setattr(agent_api, "force_terminal_state", _unexpected_force_terminal)
    monkeypatch.setattr(agent_api, "update_run_sandbox_lease", _unexpected_update_lease)
    monkeypatch.setattr(agent_api.redis, "publish", _fake_publish)
    monkeypatch.setattr(agent_api.redis, "scan_keys", _fake_scan_keys, raising=False)
    monkeypatch.setattr(agent_api, "_cleanup_redis_response_list", _fake_cleanup)

    await agent_api.stop_agent_run("run-v2-phase2-active")

    assert update_status_calls == [
        {
            "agent_run_id": "run-v2-phase2-active",
            "status": "stopped",
            "error": None,
        }
    ]
    assert client.regular_attempt_rows[0]["status"] == "stopped"
    assert client.regular_attempt_rows[0]["terminal_status"] == "stopped"
    assert client.regular_attempt_rows[0]["terminal_reason"] == "external_stop"
    assert publish_calls == [
        ("agent_run:run-v2-phase2-active:control", "STOP"),
        ("agent_run:run-v2-phase2-active:control:worker-1", "STOP"),
    ]
    assert cleanup_calls == ["run-v2-phase2-active"]


@pytest.mark.asyncio
async def test_stop_agent_run_marks_active_phase2_attempt_stopped_even_if_parent_status_write_fails(
    monkeypatch,
):
    client = _FakeAgentApiClient(
        agent_run_rows=[
            {
                "agent_run_id": "run-phase2-stop-race",
                "status": "running",
                "metadata": json.dumps(
                    {
                        "shadow_clone_mode": "off",
                        "regular_execution_mode": "phase2_supervisor",
                    }
                ),
            }
        ],
        regular_attempt_rows=[
            {
                "attempt_id": "attempt-stop-race",
                "agent_run_id": "run-phase2-stop-race",
                "attempt_number": 3,
                "execution_epoch": 7,
                "status": "running",
            }
        ],
    )
    publish_calls = []

    class _FakeAgentDB:
        @property
        async def client(self):
            return client

    async def _failed_update_status(*_args, **_kwargs):
        return False

    async def _unexpected_force_terminal(*_args, **_kwargs):
        raise AssertionError("regular phase2 stop must not sync shadow clone state")

    async def _unexpected_update_lease(*_args, **_kwargs):
        raise AssertionError("regular phase2 stop must not touch sandbox lease")

    async def _fake_publish(channel: str, message: str):
        publish_calls.append((channel, message))
        return 1

    async def _fake_scan_keys(pattern: str):
        if pattern == "active_run:*:run-phase2-stop-race":
            return ["active_run:worker-1:run-phase2-stop-race"]
        return []

    async def _fake_cleanup(_run_id: str, **_kwargs):
        return None

    monkeypatch.setattr(agent_api, "db", _FakeAgentDB())
    monkeypatch.setattr(agent_api, "update_agent_run_status", _failed_update_status)
    monkeypatch.setattr(agent_api, "force_terminal_state", _unexpected_force_terminal)
    monkeypatch.setattr(agent_api, "update_run_sandbox_lease", _unexpected_update_lease)
    monkeypatch.setattr(agent_api.redis, "publish", _fake_publish)
    monkeypatch.setattr(agent_api.redis, "scan_keys", _fake_scan_keys, raising=False)
    monkeypatch.setattr(agent_api, "_cleanup_redis_response_list", _fake_cleanup)

    await agent_api.stop_agent_run("run-phase2-stop-race")

    assert client.regular_attempt_rows[0]["status"] == "stopped"
    assert client.regular_attempt_rows[0]["terminal_status"] == "stopped"
    assert client.regular_attempt_rows[0]["terminal_reason"] == "external_stop"
    assert publish_calls == [
        ("agent_run:run-phase2-stop-race:control", "STOP"),
        ("agent_run:run-phase2-stop-race:control:worker-1", "STOP"),
    ]


@pytest.mark.asyncio
async def test_stop_agent_run_marks_existing_phase2_queued_retry_attempts_stopped(
    monkeypatch,
):
    client = _FakeAgentApiClient(
        agent_run_rows=[
            {
                "agent_run_id": "run-phase2-retry-stop",
                "status": "running",
                "metadata": json.dumps(
                    {
                        "shadow_clone_mode": "off",
                        "regular_execution_mode": "phase2_supervisor",
                    }
                ),
            }
        ],
        regular_attempt_rows=[
            {
                "attempt_id": "attempt-old",
                "agent_run_id": "run-phase2-retry-stop",
                "attempt_number": 1,
                "execution_epoch": 1,
                "status": "abandoned",
            },
            {
                "attempt_id": "attempt-retry",
                "agent_run_id": "run-phase2-retry-stop",
                "attempt_number": 2,
                "execution_epoch": 2,
                "status": "queued",
            },
        ],
    )

    class _FakeAgentDB:
        @property
        async def client(self):
            return client

    async def _fake_update_status(_client, agent_run_id: str, status: str, error=None):
        assert agent_run_id == "run-phase2-retry-stop"
        assert status == "stopped"
        assert error is None
        for row in client.agent_run_rows:
            if row.get("agent_run_id") == agent_run_id:
                row["status"] = status
        return True

    async def _unexpected_force_terminal(*_args, **_kwargs):
        raise AssertionError("regular phase2 stop must not sync shadow clone state")

    async def _unexpected_update_lease(*_args, **_kwargs):
        raise AssertionError("regular phase2 stop must not touch sandbox lease")

    async def _fake_publish(_channel: str, _message: str):
        return 1

    async def _fake_scan_keys(_pattern: str):
        return []

    async def _fake_cleanup(_run_id: str, **_kwargs):
        return None

    monkeypatch.setattr(agent_api, "db", _FakeAgentDB())
    monkeypatch.setattr(agent_api, "update_agent_run_status", _fake_update_status)
    monkeypatch.setattr(agent_api, "force_terminal_state", _unexpected_force_terminal)
    monkeypatch.setattr(agent_api, "update_run_sandbox_lease", _unexpected_update_lease)
    monkeypatch.setattr(agent_api.redis, "publish", _fake_publish)
    monkeypatch.setattr(agent_api.redis, "scan_keys", _fake_scan_keys, raising=False)
    monkeypatch.setattr(agent_api, "_cleanup_redis_response_list", _fake_cleanup)

    await agent_api.stop_agent_run("run-phase2-retry-stop")

    assert client.regular_attempt_rows[0]["status"] == "abandoned"
    assert client.regular_attempt_rows[1]["status"] == "stopped"
    assert client.regular_attempt_rows[1]["terminal_status"] == "stopped"
    assert client.regular_attempt_rows[1]["terminal_reason"] == "external_stop"


@pytest.mark.asyncio
async def test_stop_agent_run_marks_active_phase2_attempt_terminal_before_stop_publish(
    monkeypatch,
):
    client = _FakeAgentApiClient(
        agent_run_rows=[
            {
                "agent_run_id": "run-phase2-active",
                "status": "running",
                "metadata": json.dumps(
                    {
                        "shadow_clone_mode": "off",
                        "regular_execution_mode": "phase2_supervisor",
                    }
                ),
            }
        ]
    )
    call_order: list[str] = []
    publish_calls: list[tuple[str, str]] = []
    attempt_stop_calls: list[dict[str, object]] = []

    class _FakeAgentDB:
        @property
        async def client(self):
            return client

    async def _fake_update_status(_client, agent_run_id: str, status: str, error=None):
        call_order.append("update-status")
        assert agent_run_id == "run-phase2-active"
        assert status == "stopped"
        assert error is None
        return True

    async def _fake_mark_current_attempt_terminal(
        _client,
        *,
        agent_run_id: str,
        final_status: str,
        error_message,
    ):
        call_order.append("mark-attempt-terminal")
        attempt_stop_calls.append(
            {
                "agent_run_id": agent_run_id,
                "final_status": final_status,
                "error_message": error_message,
            }
        )
        return {"attempt_id": "attempt-1", "status": "stopped"}

    async def _fake_release_capacity(*_args, **_kwargs):
        call_order.append("release-capacity")
        return None

    async def _fake_force_terminal(*_args, **_kwargs):
        call_order.append("force-shadow-terminal")
        return None

    async def _fake_update_lease(*_args, **_kwargs):
        call_order.append("update-shadow-lease")
        return None

    async def _fake_publish(channel: str, message: str):
        call_order.append(f"publish:{channel}")
        publish_calls.append((channel, message))
        return 1

    async def _fake_scan_keys(pattern: str):
        call_order.append(f"scan:{pattern}")
        if pattern == "active_run:*:run-phase2-active":
            return ["active_run:worker-1:run-phase2-active"]
        return []

    async def _fake_cleanup(_run_id):
        call_order.append("cleanup-response-list")
        return None

    monkeypatch.setattr(agent_api, "db", _FakeAgentDB())
    monkeypatch.setattr(agent_api, "update_agent_run_status", _fake_update_status)
    monkeypatch.setattr(
        agent_api.regular_run_attempts,
        "mark_current_attempt_terminal",
        _fake_mark_current_attempt_terminal,
    )
    monkeypatch.setattr(
        agent_api,
        "_release_api_side_run_capacity_reservation_on_stop_best_effort",
        _fake_release_capacity,
    )
    monkeypatch.setattr(agent_api, "force_terminal_state", _fake_force_terminal)
    monkeypatch.setattr(agent_api, "update_run_sandbox_lease", _fake_update_lease)
    monkeypatch.setattr(agent_api.redis, "publish", _fake_publish)
    monkeypatch.setattr(agent_api.redis, "scan_keys", _fake_scan_keys, raising=False)
    monkeypatch.setattr(agent_api, "_cleanup_redis_response_list", _fake_cleanup)

    await agent_api.stop_agent_run("run-phase2-active")

    assert attempt_stop_calls == [
        {
            "agent_run_id": "run-phase2-active",
            "final_status": "stopped",
            "error_message": None,
        }
    ]
    assert publish_calls == [
        ("agent_run:run-phase2-active:control", "STOP"),
        ("agent_run:run-phase2-active:control:worker-1", "STOP"),
    ]
    assert call_order.index("mark-attempt-terminal") < call_order.index(
        "publish:agent_run:run-phase2-active:control"
    )


@pytest.mark.asyncio
async def test_stop_agent_run_phase2_active_marks_queued_recovery_attempt_stopped(
    monkeypatch,
):
    client = _FakeAgentApiClient(
        agent_run_rows=[
            {
                "agent_run_id": "run-phase2-recovery-stop",
                "status": "running",
                "metadata": json.dumps(
                    {
                        "shadow_clone_mode": "off",
                        "regular_execution_mode": "phase2_supervisor",
                    }
                ),
            }
        ],
        regular_attempt_rows=[
            {
                "attempt_id": "attempt-old",
                "agent_run_id": "run-phase2-recovery-stop",
                "attempt_number": 1,
                "execution_epoch": 1,
                "status": "abandoned",
            },
            {
                "attempt_id": "attempt-requeued",
                "agent_run_id": "run-phase2-recovery-stop",
                "attempt_number": 2,
                "execution_epoch": 2,
                "status": "queued",
            },
        ],
    )
    publish_calls: list[tuple[str, str]] = []

    class _FakeAgentDB:
        @property
        async def client(self):
            return client

    async def _fake_update_status(_client, agent_run_id: str, status: str, error=None):
        assert agent_run_id == "run-phase2-recovery-stop"
        assert status == "stopped"
        assert error is None
        client.agent_run_rows[0]["status"] = status
        return True

    async def _unexpected_force_terminal(*_args, **_kwargs):
        raise AssertionError("regular phase2 stop must not sync shadow clone state")

    async def _unexpected_update_lease(*_args, **_kwargs):
        raise AssertionError("regular phase2 stop must not touch sandbox lease")

    async def _fake_publish(channel: str, message: str):
        publish_calls.append((channel, message))
        return 1

    async def _fake_scan_keys(pattern: str):
        assert pattern == "active_run:*:run-phase2-recovery-stop"
        return []

    async def _fake_cleanup(_run_id: str, **_kwargs):
        return None

    monkeypatch.setattr(agent_api, "db", _FakeAgentDB())
    monkeypatch.setattr(agent_api, "update_agent_run_status", _fake_update_status)
    monkeypatch.setattr(agent_api, "force_terminal_state", _unexpected_force_terminal)
    monkeypatch.setattr(agent_api, "update_run_sandbox_lease", _unexpected_update_lease)
    monkeypatch.setattr(agent_api.redis, "publish", _fake_publish)
    monkeypatch.setattr(agent_api.redis, "scan_keys", _fake_scan_keys, raising=False)
    monkeypatch.setattr(agent_api, "_cleanup_redis_response_list", _fake_cleanup)

    await agent_api.stop_agent_run("run-phase2-recovery-stop")

    assert client.regular_attempt_rows[1]["status"] == "stopped"
    assert client.regular_attempt_rows[1]["terminal_status"] == "stopped"
    assert client.regular_attempt_rows[1]["terminal_reason"] == "external_stop"
    assert publish_calls == [
        ("agent_run:run-phase2-recovery-stop:control", "STOP"),
    ]


@pytest.mark.asyncio
async def test_stop_agent_run_phase2_active_still_publishes_stop_when_attempt_mark_fails(
    monkeypatch,
):
    client = _FakeAgentApiClient(
        agent_run_rows=[
            {
                "agent_run_id": "run-phase2-active",
                "status": "running",
                "metadata": json.dumps(
                    {
                        "shadow_clone_mode": "off",
                        "regular_execution_mode": "phase2_supervisor",
                    }
                ),
            }
        ]
    )
    publish_calls: list[tuple[str, str]] = []

    class _FakeAgentDB:
        @property
        async def client(self):
            return client

    async def _fake_update_status(*_args, **_kwargs):
        return True

    async def _failing_mark_current_attempt_terminal(*_args, **_kwargs):
        raise RuntimeError("boom")

    async def _fake_release_capacity(*_args, **_kwargs):
        return None

    async def _fake_force_terminal(*_args, **_kwargs):
        return None

    async def _fake_update_lease(*_args, **_kwargs):
        return None

    async def _fake_publish(channel: str, message: str):
        publish_calls.append((channel, message))
        return 1

    async def _fake_scan_keys(pattern: str):
        if pattern == "active_run:*:run-phase2-active":
            return ["active_run:worker-1:run-phase2-active"]
        return []

    async def _fake_cleanup(_run_id):
        return None

    monkeypatch.setattr(agent_api, "db", _FakeAgentDB())
    monkeypatch.setattr(agent_api, "update_agent_run_status", _fake_update_status)
    monkeypatch.setattr(
        agent_api.regular_run_attempts,
        "mark_current_attempt_terminal",
        _failing_mark_current_attempt_terminal,
        raising=False,
    )
    monkeypatch.setattr(
        agent_api,
        "_release_api_side_run_capacity_reservation_on_stop_best_effort",
        _fake_release_capacity,
    )
    monkeypatch.setattr(agent_api, "force_terminal_state", _fake_force_terminal)
    monkeypatch.setattr(agent_api, "update_run_sandbox_lease", _fake_update_lease)
    monkeypatch.setattr(agent_api.redis, "publish", _fake_publish)
    monkeypatch.setattr(agent_api.redis, "scan_keys", _fake_scan_keys, raising=False)
    monkeypatch.setattr(agent_api, "_cleanup_redis_response_list", _fake_cleanup)

    await agent_api.stop_agent_run("run-phase2-active")

    assert publish_calls == [
        ("agent_run:run-phase2-active:control", "STOP"),
        ("agent_run:run-phase2-active:control:worker-1", "STOP"),
    ]


@pytest.mark.asyncio
async def test_stop_agent_run_keeps_queued_shadow_clone_rows_on_legacy_publish_path(
    monkeypatch,
):
    client = _FakeAgentApiClient(
        agent_run_rows=[
            {
                "agent_run_id": "run-shadow-queued",
                "status": "queued",
                "metadata": json.dumps({"shadow_clone_mode": "on"}),
            }
        ]
    )
    publish_calls = []

    class _FakeAgentDB:
        @property
        async def client(self):
            return client

    async def _fake_update_status(*_args, **_kwargs):
        return True

    async def _fake_force_terminal(*_args, **_kwargs):
        return {"execution_epoch": 3}

    async def _fake_update_lease(*_args, **_kwargs):
        return {"ok": True}

    async def _fake_publish(channel: str, message: str):
        publish_calls.append((channel, message))
        return 1

    async def _fake_scan_keys(pattern: str):
        if pattern == "active_run:*:run-shadow-queued":
            return ["active_run:worker-1:run-shadow-queued"]
        return []

    async def _fake_cleanup(_run_id):
        return None

    monkeypatch.setattr(agent_api, "db", _FakeAgentDB())
    monkeypatch.setattr(agent_api, "update_agent_run_status", _fake_update_status)
    monkeypatch.setattr(agent_api, "force_terminal_state", _fake_force_terminal)
    monkeypatch.setattr(agent_api, "update_run_sandbox_lease", _fake_update_lease)
    monkeypatch.setattr(agent_api.redis, "publish", _fake_publish)
    monkeypatch.setattr(agent_api.redis, "scan_keys", _fake_scan_keys, raising=False)
    monkeypatch.setattr(agent_api, "_cleanup_redis_response_list", _fake_cleanup)

    await agent_api.stop_agent_run("run-shadow-queued")

    assert publish_calls == [
        ("agent_run:run-shadow-queued:control", "STOP"),
        ("agent_run:run-shadow-queued:control:worker-1", "STOP"),
    ]
