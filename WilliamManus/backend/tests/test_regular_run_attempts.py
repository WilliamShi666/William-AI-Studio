from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from types import SimpleNamespace

import pytest

from services import regular_run_attempts


@dataclass(frozen=True)
class _Filter:
    kind: str
    field: str
    value: object


class _FakeTableQuery:
    def __init__(self, tables: dict[str, list[dict]], table_name: str):
        self._tables = tables
        self._table_name = table_name
        self._action = "select"
        self._filters: list[_Filter] = []
        self._insert_payload: dict | None = None
        self._update_payload: dict | None = None
        self._order_field: str | None = None
        self._order_desc = False
        self._limit: int | None = None

    def select(self, *_args, **_kwargs):
        self._action = "select"
        return self

    def insert(self, payload: dict):
        self._action = "insert"
        self._insert_payload = dict(payload)
        return self

    def update(self, payload: dict):
        self._action = "update"
        self._update_payload = dict(payload)
        return self

    def eq(self, field: str, value: object):
        self._filters.append(_Filter("eq", field, value))
        return self

    def in_(self, field: str, values: list[object]):
        self._filters.append(_Filter("in", field, tuple(values)))
        return self

    def is_(self, field: str, value: object):
        self._filters.append(_Filter("is", field, value))
        return self

    def lte(self, field: str, value: object):
        self._filters.append(_Filter("lte", field, value))
        return self

    def order(self, field: str, *, desc: bool = False):
        self._order_field = field
        self._order_desc = bool(desc)
        return self

    def limit(self, count: int):
        self._limit = count
        return self

    async def execute(self):
        rows = self._tables.setdefault(self._table_name, [])

        if self._action == "insert":
            assert self._insert_payload is not None
            inserted = dict(self._insert_payload)
            rows.append(inserted)
            return SimpleNamespace(data=[dict(inserted)])

        def _matches(row: dict) -> bool:
            for current_filter in self._filters:
                row_value = row.get(current_filter.field)
                if current_filter.kind == "eq":
                    if row_value != current_filter.value:
                        return False
                    continue
                if current_filter.kind == "in":
                    if row_value not in current_filter.value:
                        return False
                    continue
                if current_filter.kind == "is":
                    if current_filter.value is None:
                        if row_value is not None:
                            return False
                        continue
                    if row_value is not current_filter.value:
                        return False
                    continue
                if current_filter.kind == "lte":
                    if row_value is None or row_value > current_filter.value:
                        return False
                    continue
                return False
            return True

        matched_rows = [row for row in rows if _matches(row)]
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
                key=lambda row: row.get(self._order_field) or "",
                reverse=self._order_desc,
            )
        if self._limit is not None:
            selected_rows = selected_rows[: self._limit]
        return SimpleNamespace(data=selected_rows)


class _FakeClient:
    def __init__(
        self,
        *,
        attempt_rows: list[dict] | None = None,
        agent_run_rows: list[dict] | None = None,
    ):
        self.tables = {
            "regular_run_attempts": [dict(row) for row in (attempt_rows or [])],
            "agent_runs": [dict(row) for row in (agent_run_rows or [])],
        }

    @classmethod
    def with_attempt_rows(cls, rows: list[dict]) -> "_FakeClient":
        return cls(attempt_rows=rows)

    @classmethod
    def with_rows(
        cls,
        *,
        attempt_rows: list[dict] | None = None,
        agent_run_rows: list[dict] | None = None,
    ) -> "_FakeClient":
        return cls(
            attempt_rows=attempt_rows,
            agent_run_rows=agent_run_rows,
        )

    def table(self, table_name: str) -> _FakeTableQuery:
        return _FakeTableQuery(self.tables, table_name)


class _AsyncMutationTableQuery(_FakeTableQuery):
    async def insert(self, payload: dict):
        self._action = "insert"
        self._insert_payload = dict(payload)
        return await self.execute()

    async def update(self, payload: dict):
        self._action = "update"
        self._update_payload = dict(payload)
        return await self.execute()


class _AsyncMutationClient(_FakeClient):
    def table(self, table_name: str) -> _AsyncMutationTableQuery:
        return _AsyncMutationTableQuery(self.tables, table_name)


class _ServerClaimConnection:
    def __init__(self, row: dict | None):
        self._row = dict(row) if row is not None else None
        self.fetch_calls: list[tuple[str, tuple[object, ...]]] = []

    async def fetchrow(self, query: str, *params: object):
        self.fetch_calls.append((query, params))
        if self._row is None:
            return None
        return dict(self._row)


class _ServerClaimPoolAcquire:
    def __init__(self, connection: _ServerClaimConnection):
        self._connection = connection

    async def __aenter__(self) -> _ServerClaimConnection:
        return self._connection

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        return False


class _ServerClaimPool:
    def __init__(self, row: dict | None):
        self.connection = _ServerClaimConnection(row)

    def acquire(self) -> _ServerClaimPoolAcquire:
        return _ServerClaimPoolAcquire(self.connection)


class _ServerClaimClient:
    def __init__(self, row: dict | None):
        self.pool = _ServerClaimPool(row)
        self.table_calls: list[str] = []

    def table(self, table_name: str):
        self.table_calls.append(table_name)
        raise AssertionError(
            "server-side claim cursor should avoid table scan fallback"
        )


class _FilteredRecoverableQuery(_FakeTableQuery):
    async def execute(self):
        if (
            self._table_name == "regular_run_attempts"
            and self._action == "select"
            and not self._filters
        ):
            raise AssertionError(
                "regular_run_attempts hot path should not execute an unfiltered scan"
            )
        return await super().execute()


class _FilteredRecoverableClient(_FakeClient):
    def table(self, table_name: str) -> _FilteredRecoverableQuery:
        return _FilteredRecoverableQuery(self.tables, table_name)


def test_get_regular_execution_mode_prefers_phase2_supervisor(monkeypatch):
    monkeypatch.setenv("REGULAR_EXECUTION_MODE", "phase2_supervisor")
    monkeypatch.delenv("REGULAR_ASYNC_POOL_ENABLED", raising=False)

    assert regular_run_attempts.get_regular_execution_mode() == "phase2_supervisor"


def test_get_regular_execution_mode_honors_explicit_phase1_pool(monkeypatch):
    monkeypatch.setenv("REGULAR_EXECUTION_MODE", "phase1_pool")
    monkeypatch.delenv("REGULAR_ASYNC_POOL_ENABLED", raising=False)

    assert regular_run_attempts.get_regular_execution_mode() == "phase1_pool"


def test_get_regular_execution_mode_falls_back_to_phase1_pool(monkeypatch):
    monkeypatch.delenv("REGULAR_EXECUTION_MODE", raising=False)
    monkeypatch.setenv("REGULAR_ASYNC_POOL_ENABLED", "true")

    assert regular_run_attempts.get_regular_execution_mode() == "phase1_pool"


def test_get_regular_execution_mode_defaults_to_legacy(monkeypatch):
    monkeypatch.delenv("REGULAR_EXECUTION_MODE", raising=False)
    monkeypatch.delenv("REGULAR_ASYNC_POOL_ENABLED", raising=False)

    assert regular_run_attempts.get_regular_execution_mode() == "legacy"


def test_get_regular_execution_mode_ignores_invalid_env_and_uses_phase1_alias(
    monkeypatch,
):
    monkeypatch.setenv("REGULAR_EXECUTION_MODE", "not-a-real-mode")
    monkeypatch.setenv("REGULAR_ASYNC_POOL_ENABLED", "true")

    assert regular_run_attempts.get_regular_execution_mode() == "phase1_pool"


def test_get_regular_supervisor_queue_max_depth_falls_back_to_legacy_env(
    monkeypatch,
):
    monkeypatch.delenv("REGULAR_SUPERVISOR_QUEUE_MAX_DEPTH", raising=False)
    monkeypatch.setenv("REGULAR_QUEUE_MAX_DEPTH", "7")

    assert regular_run_attempts.get_regular_supervisor_queue_max_depth() == 7


@pytest.mark.asyncio
async def test_create_initial_attempt_inserts_queued_attempt():
    client = _FakeClient()

    attempt = await regular_run_attempts.create_initial_attempt(
        client,
        agent_run_id="run-1",
    )

    assert attempt["agent_run_id"] == "run-1"
    assert attempt["attempt_number"] == 1
    assert attempt["execution_epoch"] == 1
    assert attempt["status"] == "queued"
    assert (
        client.tables["regular_run_attempts"][0]["attempt_id"] == attempt["attempt_id"]
    )


@pytest.mark.asyncio
async def test_create_initial_attempt_supports_async_insert_clients():
    client = _AsyncMutationClient()

    attempt = await regular_run_attempts.create_initial_attempt(
        client,
        agent_run_id="run-async-1",
    )

    assert attempt["agent_run_id"] == "run-async-1"
    assert attempt["status"] == "queued"
    assert isinstance(attempt["queued_at"], datetime)
    assert json.loads(attempt["metadata"]) == {}


@pytest.mark.asyncio
async def test_create_initial_attempt_rejects_blank_run_id():
    client = _FakeClient()

    with pytest.raises(ValueError, match="agent_run_id is required"):
        await regular_run_attempts.create_initial_attempt(client, agent_run_id="  ")


@pytest.mark.asyncio
async def test_claim_next_attempt_returns_oldest_queued_attempt():
    client = _FakeClient.with_attempt_rows(
        [
            {
                "attempt_id": "a2",
                "agent_run_id": "run-2",
                "status": "queued",
                "queued_at": 20,
            },
            {
                "attempt_id": "a1",
                "agent_run_id": "run-1",
                "status": "queued",
                "queued_at": 10,
            },
        ]
    )

    claimed = await regular_run_attempts.claim_next_attempt(
        client,
        supervisor_id="sup-1",
        owner_token="sup-1:1",
    )

    assert claimed["attempt_id"] == "a1"
    assert claimed["status"] == "claimed"
    assert claimed["supervisor_id"] == "sup-1"
    assert claimed["owner_token"] == "sup-1:1"
    assert claimed["claimed_at"]


@pytest.mark.asyncio
async def test_claim_next_attempt_sets_heartbeat_and_lease_expiry(monkeypatch):
    queued_at = datetime(2026, 4, 4, 0, 0, 0)
    client = _FakeClient.with_attempt_rows(
        [
            {
                "attempt_id": "a1",
                "agent_run_id": "run-1",
                "status": "queued",
                "queued_at": queued_at,
            }
        ]
    )
    monkeypatch.setenv("REGULAR_SUPERVISOR_ATTEMPT_LEASE_TTL_SECONDS", "30")

    claimed = await regular_run_attempts.claim_next_attempt(
        client,
        supervisor_id="sup-1",
        owner_token="sup-1:slot-1",
    )

    assert claimed is not None
    assert claimed["status"] == "claimed"
    assert claimed["heartbeat_at"] >= claimed["claimed_at"]
    assert claimed["lease_expires_at"] > claimed["heartbeat_at"]


@pytest.mark.asyncio
async def test_claim_next_attempt_supports_async_update_clients():
    client = _AsyncMutationClient.with_attempt_rows(
        [
            {
                "attempt_id": "async-a1",
                "agent_run_id": "run-async-1",
                "status": "queued",
                "queued_at": 10,
            }
        ]
    )

    claimed = await regular_run_attempts.claim_next_attempt(
        client,
        supervisor_id="sup-async",
        owner_token="sup-async:1",
    )

    assert claimed["attempt_id"] == "async-a1"
    assert claimed["status"] == "claimed"
    assert isinstance(claimed["claimed_at"], datetime)


@pytest.mark.asyncio
async def test_claim_next_attempt_uses_server_side_claim_cursor(monkeypatch):
    claim_time = datetime(2026, 4, 4, 0, 0, 0, tzinfo=timezone.utc)
    claim_row = {
        "attempt_id": "server-a1",
        "agent_run_id": "run-1",
        "attempt_number": 1,
        "execution_epoch": 1,
        "status": "claimed",
        "supervisor_id": "sup-1",
        "owner_token": "sup-1:slot-1",
        "claimed_at": claim_time,
        "heartbeat_at": claim_time,
        "lease_expires_at": datetime(2026, 4, 4, 0, 0, 30, tzinfo=timezone.utc),
    }
    client = _ServerClaimClient(claim_row)
    monkeypatch.setenv("REGULAR_SUPERVISOR_ATTEMPT_LEASE_TTL_SECONDS", "30")
    monkeypatch.setattr(regular_run_attempts, "_utcnow", lambda: claim_time)

    claimed = await regular_run_attempts.claim_next_attempt(
        client,
        supervisor_id="sup-1",
        owner_token="sup-1:slot-1",
    )

    assert claimed == claim_row
    assert client.table_calls == []
    assert len(client.pool.connection.fetch_calls) == 1
    query, params = client.pool.connection.fetch_calls[0]
    assert "FOR UPDATE SKIP LOCKED" in query
    assert "status = 'queued'" in query
    assert params == (
        "sup-1",
        "sup-1:slot-1",
        claim_time,
        claim_row["lease_expires_at"],
    )


@pytest.mark.asyncio
async def test_refresh_attempt_heartbeat_extends_lease_for_current_owner(monkeypatch):
    original_heartbeat = datetime(2026, 4, 4, 0, 0, 0, tzinfo=timezone.utc)
    original_expiry = datetime(2026, 4, 4, 0, 0, 30, tzinfo=timezone.utc)
    client = _FakeClient.with_rows(
        attempt_rows=[
            {
                "attempt_id": "a1",
                "agent_run_id": "run-1",
                "status": "running",
                "owner_token": "sup-1:slot-1",
                "heartbeat_at": original_heartbeat,
                "lease_expires_at": original_expiry,
            }
        ]
    )
    monkeypatch.setenv("REGULAR_SUPERVISOR_ATTEMPT_LEASE_TTL_SECONDS", "30")

    refreshed = await regular_run_attempts.refresh_attempt_heartbeat(
        client,
        attempt_id="a1",
        owner_token="sup-1:slot-1",
    )

    assert refreshed is not None
    assert refreshed["heartbeat_at"] > original_heartbeat
    assert refreshed["lease_expires_at"] > refreshed["heartbeat_at"]


@pytest.mark.asyncio
async def test_refresh_attempt_heartbeat_does_not_clear_parent_execution_pointer():
    original_heartbeat = datetime(2026, 4, 4, 0, 0, 0, tzinfo=timezone.utc)
    original_expiry = datetime(2026, 4, 4, 0, 0, 30, tzinfo=timezone.utc)
    client = _FakeClient.with_rows(
        attempt_rows=[
            {
                "attempt_id": "a1",
                "agent_run_id": "run-1",
                "attempt_number": 1,
                "execution_epoch": 1,
                "status": "running",
                "owner_token": "sup-1:slot-1",
                "supervisor_id": "sup-1",
                "heartbeat_at": original_heartbeat,
                "lease_expires_at": original_expiry,
            }
        ],
        agent_run_rows=[
            {
                "agent_run_id": "run-1",
                "status": "running",
                "active_attempt_id": "a1",
                "current_execution_epoch": 1,
                "stream_source_epoch": 1,
                "active_supervisor_id": "sup-1",
                "regular_execution_backend": "phase2_supervisor",
            }
        ],
    )

    refreshed = await regular_run_attempts.refresh_attempt_heartbeat(
        client,
        attempt_id="a1",
        owner_token="sup-1:slot-1",
    )

    assert refreshed is not None
    assert client.tables["agent_runs"][0]["active_attempt_id"] == "a1"
    assert client.tables["agent_runs"][0]["current_execution_epoch"] == 1
    assert client.tables["agent_runs"][0]["stream_source_epoch"] == 1
    assert client.tables["agent_runs"][0]["active_supervisor_id"] == "sup-1"


@pytest.mark.asyncio
async def test_mark_attempt_running_promotes_parent_run_from_queued_to_running():
    client = _FakeClient.with_rows(
        attempt_rows=[
            {
                "attempt_id": "a1",
                "agent_run_id": "run-1",
                "attempt_number": 1,
                "execution_epoch": 1,
                "status": "claimed",
            }
        ],
        agent_run_rows=[
            {
                "agent_run_id": "run-1",
                "status": "queued",
            }
        ],
    )

    running_attempt = await regular_run_attempts.mark_attempt_running(
        client,
        attempt_id="a1",
    )

    assert running_attempt is not None
    assert running_attempt["status"] == "running"
    assert running_attempt["started_at"]
    assert running_attempt["heartbeat_at"]
    assert client.tables["agent_runs"][0]["status"] == "running"


@pytest.mark.asyncio
async def test_mark_attempt_running_updates_parent_execution_pointers():
    client = _FakeClient.with_rows(
        attempt_rows=[
            {
                "attempt_id": "a1",
                "agent_run_id": "run-1",
                "attempt_number": 1,
                "execution_epoch": 1,
                "status": "claimed",
                "supervisor_id": "sup-1",
            }
        ],
        agent_run_rows=[
            {
                "agent_run_id": "run-1",
                "status": "queued",
                "active_attempt_id": None,
                "current_execution_epoch": None,
                "stream_source_epoch": None,
                "active_supervisor_id": None,
                "regular_execution_backend": None,
            }
        ],
    )

    running_attempt = await regular_run_attempts.mark_attempt_running(
        client,
        attempt_id="a1",
    )

    assert running_attempt is not None
    assert client.tables["agent_runs"][0]["active_attempt_id"] == "a1"
    assert client.tables["agent_runs"][0]["current_execution_epoch"] == 1
    assert client.tables["agent_runs"][0]["stream_source_epoch"] == 1
    assert client.tables["agent_runs"][0]["active_supervisor_id"] == "sup-1"
    assert client.tables["agent_runs"][0]["regular_execution_backend"] == (
        "phase2_supervisor"
    )


@pytest.mark.asyncio
async def test_mark_attempt_running_marks_attempt_stopped_when_parent_already_stopped():
    client = _FakeClient.with_rows(
        attempt_rows=[
            {
                "attempt_id": "a1",
                "agent_run_id": "run-1",
                "attempt_number": 1,
                "execution_epoch": 1,
                "status": "claimed",
            }
        ],
        agent_run_rows=[
            {
                "agent_run_id": "run-1",
                "status": "stopped",
                "error": None,
            }
        ],
    )

    running_attempt = await regular_run_attempts.mark_attempt_running(
        client,
        attempt_id="a1",
    )

    assert running_attempt is None
    assert client.tables["regular_run_attempts"][0]["status"] == "stopped"
    assert client.tables["regular_run_attempts"][0]["terminal_status"] == "stopped"
    assert client.tables["regular_run_attempts"][0]["terminal_reason"] == "external_stop"


@pytest.mark.asyncio
async def test_claim_next_attempt_is_atomic_against_duplicate_claimers():
    client = _FakeClient.with_attempt_rows(
        [
            {
                "attempt_id": "a1",
                "agent_run_id": "run-1",
                "status": "queued",
                "queued_at": 10,
            }
        ]
    )

    first_claim = await regular_run_attempts.claim_next_attempt(
        client,
        supervisor_id="sup-1",
        owner_token="sup-1:1",
    )
    second_claim = await regular_run_attempts.claim_next_attempt(
        client,
        supervisor_id="sup-2",
        owner_token="sup-2:1",
    )

    assert first_claim["attempt_id"] == "a1"
    assert second_claim is None


@pytest.mark.asyncio
async def test_claim_next_attempt_rejects_blank_supervisor_id():
    client = _FakeClient()

    with pytest.raises(ValueError, match="supervisor_id is required"):
        await regular_run_attempts.claim_next_attempt(
            client,
            supervisor_id=" ",
            owner_token="sup-1:1",
        )


@pytest.mark.asyncio
async def test_claim_next_attempt_rejects_blank_owner_token():
    client = _FakeClient()

    with pytest.raises(ValueError, match="owner_token is required"):
        await regular_run_attempts.claim_next_attempt(
            client,
            supervisor_id="sup-1",
            owner_token=" ",
        )


@pytest.mark.asyncio
async def test_ensure_attempt_queue_capacity_rejects_when_limit_is_reached(monkeypatch):
    monkeypatch.setenv("REGULAR_SUPERVISOR_QUEUE_MAX_DEPTH", "2")
    client = _FakeClient.with_attempt_rows(
        [
            {"attempt_id": "a1", "status": "queued"},
            {"attempt_id": "a2", "status": "queued"},
        ]
    )

    with pytest.raises(regular_run_attempts.RegularAttemptQueueFullError):
        await regular_run_attempts.ensure_attempt_queue_capacity(client)


@pytest.mark.asyncio
async def test_ensure_attempt_queue_capacity_ignores_nonqueued_rows(monkeypatch):
    monkeypatch.setenv("REGULAR_SUPERVISOR_QUEUE_MAX_DEPTH", "2")
    client = _FakeClient.with_attempt_rows(
        [
            {"attempt_id": "a1", "status": "queued"},
            {"attempt_id": "a2", "status": "running"},
        ]
    )

    await regular_run_attempts.ensure_attempt_queue_capacity(client)


@pytest.mark.asyncio
async def test_reconcile_lost_attempt_requeues_new_attempt_for_running_parent():
    client = _FakeClient.with_rows(
        attempt_rows=[
            {
                "attempt_id": "a1",
                "agent_run_id": "run-1",
                "attempt_number": 1,
                "execution_epoch": 1,
                "status": "running",
                "owner_token": "sup-1:slot-1",
                "supervisor_id": "sup-1",
            }
        ],
        agent_run_rows=[
            {
                "agent_run_id": "run-1",
                "status": "running",
            }
        ],
    )

    new_attempt = await regular_run_attempts.reconcile_lost_attempt(
        client,
        attempt_id="a1",
        recovery_reason="supervisor_lost",
    )

    assert new_attempt is not None
    assert new_attempt["agent_run_id"] == "run-1"
    assert new_attempt["attempt_number"] == 2
    assert new_attempt["execution_epoch"] == 2
    assert new_attempt["status"] == "queued"
    assert new_attempt["recovery_reason"] == "supervisor_lost"

    previous_attempt = client.tables["regular_run_attempts"][0]
    assert previous_attempt["status"] == "abandoned"
    assert previous_attempt["terminal_reason"] == "supervisor_lost"


@pytest.mark.asyncio
async def test_reconcile_lost_attempt_advances_parent_epoch_and_preserves_stream_source():
    client = _FakeClient.with_rows(
        attempt_rows=[
            {
                "attempt_id": "a1",
                "agent_run_id": "run-1",
                "attempt_number": 1,
                "execution_epoch": 1,
                "status": "running",
                "owner_token": "sup-1:slot-1",
                "supervisor_id": "sup-1",
            }
        ],
        agent_run_rows=[
            {
                "agent_run_id": "run-1",
                "status": "running",
                "active_attempt_id": "a1",
                "current_execution_epoch": 1,
                "stream_source_epoch": 1,
                "active_supervisor_id": "sup-1",
                "regular_execution_backend": "phase2_supervisor",
            }
        ],
    )

    new_attempt = await regular_run_attempts.reconcile_lost_attempt(
        client,
        attempt_id="a1",
        recovery_reason="supervisor_lost",
    )

    assert new_attempt is not None
    assert new_attempt["execution_epoch"] == 2
    assert client.tables["agent_runs"][0]["active_attempt_id"] is None
    assert client.tables["agent_runs"][0]["current_execution_epoch"] == 2
    assert client.tables["agent_runs"][0]["stream_source_epoch"] == 1
    assert client.tables["agent_runs"][0]["active_supervisor_id"] is None


@pytest.mark.asyncio
async def test_reconcile_lost_attempt_does_not_requeue_terminal_parent():
    client = _FakeClient.with_rows(
        attempt_rows=[
            {
                "attempt_id": "a1",
                "agent_run_id": "run-1",
                "attempt_number": 2,
                "execution_epoch": 3,
                "status": "running",
            }
        ],
        agent_run_rows=[
            {
                "agent_run_id": "run-1",
                "status": "stopped",
            }
        ],
    )

    new_attempt = await regular_run_attempts.reconcile_lost_attempt(
        client,
        attempt_id="a1",
        recovery_reason="supervisor_lost",
    )

    assert new_attempt is None
    assert client.tables["regular_run_attempts"][0]["status"] == "stopped"
    assert client.tables["regular_run_attempts"][0]["terminal_status"] == "stopped"
    assert client.tables["regular_run_attempts"][0]["terminal_reason"] == "external_stop"
    assert len(client.tables["regular_run_attempts"]) == 1


@pytest.mark.asyncio
async def test_reconcile_lost_attempt_accepts_claimed_attempt_before_kernel_start():
    client = _FakeClient.with_rows(
        attempt_rows=[
            {
                "attempt_id": "a1",
                "agent_run_id": "run-1",
                "attempt_number": 1,
                "execution_epoch": 1,
                "status": "claimed",
            }
        ],
        agent_run_rows=[
            {
                "agent_run_id": "run-1",
                "status": "running",
            }
        ],
    )

    new_attempt = await regular_run_attempts.reconcile_lost_attempt(
        client,
        attempt_id="a1",
        recovery_reason="supervisor_lost",
    )

    assert new_attempt is not None
    assert new_attempt["attempt_number"] == 2
    assert client.tables["regular_run_attempts"][0]["status"] == "abandoned"


@pytest.mark.asyncio
async def test_mark_current_attempt_terminal_ignores_newer_nonrecoverable_attempts():
    client = _FakeClient.with_rows(
        attempt_rows=[
            {
                "attempt_id": "attempt-old",
                "agent_run_id": "run-1",
                "attempt_number": 1,
                "execution_epoch": 1,
                "status": "running",
            },
            {
                "attempt_id": "attempt-latest",
                "agent_run_id": "run-1",
                "attempt_number": 2,
                "execution_epoch": 2,
                "status": "claimed",
            },
            {
                "attempt_id": "attempt-debug",
                "agent_run_id": "run-1",
                "attempt_number": 3,
                "execution_epoch": 3,
                "status": "abandoned",
            },
        ]
    )

    stopped_attempt = await regular_run_attempts.mark_current_attempt_terminal(
        client,
        agent_run_id="run-1",
        final_status="stopped",
        error_message=None,
    )

    assert stopped_attempt is not None
    assert stopped_attempt["attempt_id"] == "attempt-latest"
    assert stopped_attempt["status"] == "stopped"
    assert stopped_attempt["terminal_status"] == "stopped"
    assert stopped_attempt["terminal_reason"] == "external_stop"
    assert client.tables["regular_run_attempts"][0]["status"] == "running"
    assert client.tables["regular_run_attempts"][2]["status"] == "abandoned"


@pytest.mark.asyncio
async def test_finalize_completed_attempt_marks_completed_from_parent_run():
    client = _FakeClient.with_rows(
        attempt_rows=[
            {
                "attempt_id": "a1",
                "agent_run_id": "run-1",
                "attempt_number": 1,
                "execution_epoch": 1,
                "status": "running",
            },
        ],
        agent_run_rows=[
            {
                "agent_run_id": "run-1",
                "status": "completed",
                "error": None,
            }
        ],
    )

    finalized_attempt = await regular_run_attempts.finalize_completed_attempt(
        client,
        attempt_id="a1",
    )

    assert finalized_attempt is not None
    assert finalized_attempt["attempt_id"] == "a1"
    assert finalized_attempt["status"] == "completed"
    assert finalized_attempt["terminal_status"] == "completed"
    assert finalized_attempt["terminal_reason"] == "parent_run_completed"


@pytest.mark.asyncio
async def test_finalize_completed_attempt_uses_parent_failed_error_message():
    client = _FakeClient.with_rows(
        attempt_rows=[
            {
                "attempt_id": "a1",
                "agent_run_id": "run-1",
                "attempt_number": 1,
                "execution_epoch": 1,
                "status": "running",
            },
        ],
        agent_run_rows=[
            {
                "agent_run_id": "run-1",
                "status": "failed",
                "error": "provider timeout",
            }
        ],
    )

    finalized_attempt = await regular_run_attempts.finalize_completed_attempt(
        client,
        attempt_id="a1",
    )

    assert finalized_attempt is not None
    assert finalized_attempt["status"] == "failed"
    assert finalized_attempt["terminal_status"] == "failed"
    assert finalized_attempt["terminal_reason"] == "provider timeout"


@pytest.mark.asyncio
async def test_finalize_completed_attempt_returns_none_for_nonterminal_parent():
    client = _FakeClient.with_rows(
        attempt_rows=[
            {
                "attempt_id": "a1",
                "agent_run_id": "run-1",
                "attempt_number": 1,
                "execution_epoch": 1,
                "status": "running",
            },
        ],
        agent_run_rows=[
            {
                "agent_run_id": "run-1",
                "status": "running",
                "error": None,
            }
        ],
    )

    finalized_attempt = await regular_run_attempts.finalize_completed_attempt(
        client,
        attempt_id="a1",
    )

    assert finalized_attempt is None
    assert client.tables["regular_run_attempts"][0]["status"] == "running"


@pytest.mark.asyncio
async def test_finalize_completed_attempt_returns_existing_terminal_attempt_when_parent_stop_already_won():
    client = _FakeClient.with_rows(
        attempt_rows=[
            {
                "attempt_id": "a1",
                "agent_run_id": "run-1",
                "attempt_number": 1,
                "execution_epoch": 1,
                "status": "stopped",
                "terminal_status": "stopped",
                "terminal_reason": "external_stop",
            },
        ],
        agent_run_rows=[
            {
                "agent_run_id": "run-1",
                "status": "stopped",
                "error": "external_stop",
            }
        ],
    )

    finalized_attempt = await regular_run_attempts.finalize_completed_attempt(
        client,
        attempt_id="a1",
    )

    assert finalized_attempt is not None
    assert finalized_attempt["attempt_id"] == "a1"
    assert finalized_attempt["status"] == "stopped"
    assert finalized_attempt["terminal_status"] == "stopped"
    assert finalized_attempt["terminal_reason"] == "external_stop"


@pytest.mark.asyncio
async def test_mark_current_attempt_terminal_marks_latest_recoverable_attempt_stopped():
    client = _FakeClient.with_rows(
        attempt_rows=[
            {
                "attempt_id": "a1",
                "agent_run_id": "run-1",
                "attempt_number": 1,
                "execution_epoch": 1,
                "status": "running",
            },
            {
                "attempt_id": "a2",
                "agent_run_id": "run-1",
                "attempt_number": 2,
                "execution_epoch": 2,
                "status": "claimed",
            },
        ]
    )

    updated_attempt = await regular_run_attempts.mark_current_attempt_terminal(
        client,
        agent_run_id="run-1",
        final_status="stopped",
        error_message=None,
    )

    assert updated_attempt is not None
    assert updated_attempt["attempt_id"] == "a2"
    assert updated_attempt["status"] == "stopped"
    assert updated_attempt["terminal_status"] == "stopped"
    assert updated_attempt["terminal_reason"] == "external_stop"
    assert client.tables["regular_run_attempts"][0]["status"] == "running"
    assert client.tables["regular_run_attempts"][1]["status"] == "stopped"


@pytest.mark.asyncio
async def test_has_recoverable_attempts_avoids_unfiltered_attempt_scan():
    client = _FilteredRecoverableClient.with_rows(
        attempt_rows=[
            {"attempt_id": "a1", "status": "abandoned"},
            {"attempt_id": "a2", "status": "running"},
        ]
    )

    has_recoverable = await regular_run_attempts.has_recoverable_attempts(client)

    assert has_recoverable is True


@pytest.mark.asyncio
async def test_reconcile_lost_attempt_preserves_stopped_attempt_without_requeue():
    client = _FakeClient.with_rows(
        attempt_rows=[
            {
                "attempt_id": "a1",
                "agent_run_id": "run-1",
                "attempt_number": 2,
                "execution_epoch": 2,
                "status": "stopped",
                "terminal_status": "stopped",
                "terminal_reason": "external_stop",
            }
        ],
        agent_run_rows=[
            {
                "agent_run_id": "run-1",
                "status": "stopped",
            }
        ],
    )

    new_attempt = await regular_run_attempts.reconcile_lost_attempt(
        client,
        attempt_id="a1",
        recovery_reason="supervisor_lost",
    )

    assert new_attempt is None
    assert client.tables["regular_run_attempts"][0]["status"] == "stopped"


@pytest.mark.asyncio
async def test_reconcile_lost_attempt_abandons_older_recoverable_attempt_without_requeue():
    client = _FakeClient.with_rows(
        attempt_rows=[
            {
                "attempt_id": "a1",
                "agent_run_id": "run-1",
                "attempt_number": 1,
                "execution_epoch": 1,
                "status": "running",
            },
            {
                "attempt_id": "a2",
                "agent_run_id": "run-1",
                "attempt_number": 2,
                "execution_epoch": 2,
                "status": "claimed",
            },
        ],
        agent_run_rows=[
            {
                "agent_run_id": "run-1",
                "status": "running",
            }
        ],
    )

    new_attempt = await regular_run_attempts.reconcile_lost_attempt(
        client,
        attempt_id="a1",
        recovery_reason="supervisor_unresponsive",
    )

    assert new_attempt is None
    assert len(client.tables["regular_run_attempts"]) == 2
    assert client.tables["regular_run_attempts"][0]["status"] == "abandoned"
    assert client.tables["regular_run_attempts"][1]["status"] == "claimed"


@pytest.mark.asyncio
async def test_reconcile_expired_attempts_avoids_unfiltered_attempt_scan(monkeypatch):
    stale_time = datetime(2026, 4, 4, 0, 0, 0)
    recovered_now = datetime(2026, 4, 4, 0, 1, 0)
    client = _FilteredRecoverableClient.with_rows(
        attempt_rows=[
            {
                "attempt_id": "a1",
                "agent_run_id": "run-1",
                "attempt_number": 1,
                "execution_epoch": 1,
                "status": "running",
                "owner_token": "sup-1:slot-1",
                "supervisor_id": "sup-1",
                "heartbeat_at": stale_time,
                "lease_expires_at": None,
            },
            {
                "attempt_id": "a2",
                "agent_run_id": "run-2",
                "attempt_number": 1,
                "execution_epoch": 1,
                "status": "completed",
                "heartbeat_at": stale_time,
                "lease_expires_at": None,
            },
        ],
        agent_run_rows=[
            {
                "agent_run_id": "run-1",
                "status": "running",
            },
            {
                "agent_run_id": "run-2",
                "status": "completed",
            },
        ],
    )
    monkeypatch.setenv("REGULAR_SUPERVISOR_ATTEMPT_LEASE_TTL_SECONDS", "15")
    monkeypatch.setattr(regular_run_attempts, "_utcnow", lambda: recovered_now)

    recovered_attempts = await regular_run_attempts.reconcile_expired_attempts(
        client,
        recovery_reason="supervisor_unresponsive",
    )

    assert len(recovered_attempts) == 1
    assert recovered_attempts[0]["agent_run_id"] == "run-1"
    assert [row["status"] for row in client.tables["regular_run_attempts"]] == [
        "abandoned",
        "completed",
        "queued",
    ]


@pytest.mark.asyncio
async def test_reconcile_expired_attempts_requeues_stale_running_attempt_without_lease(
    monkeypatch,
):
    stale_heartbeat = datetime(2026, 4, 4, 0, 0, 0)
    recovered_now = datetime(2026, 4, 4, 0, 1, 0)
    client = _FakeClient.with_rows(
        attempt_rows=[
            {
                "attempt_id": "a1",
                "agent_run_id": "run-1",
                "attempt_number": 1,
                "execution_epoch": 1,
                "status": "running",
                "owner_token": "sup-1:slot-1",
                "supervisor_id": "sup-1",
                "heartbeat_at": stale_heartbeat,
                "lease_expires_at": None,
            }
        ],
        agent_run_rows=[
            {
                "agent_run_id": "run-1",
                "status": "running",
            }
        ],
    )
    monkeypatch.setenv("REGULAR_SUPERVISOR_ATTEMPT_LEASE_TTL_SECONDS", "15")
    monkeypatch.setattr(regular_run_attempts, "_utcnow", lambda: recovered_now)

    recovered_attempts = await regular_run_attempts.reconcile_expired_attempts(
        client,
        recovery_reason="supervisor_unresponsive",
    )

    assert len(recovered_attempts) == 1
    assert recovered_attempts[0]["agent_run_id"] == "run-1"
    assert recovered_attempts[0]["attempt_number"] == 2
    assert recovered_attempts[0]["execution_epoch"] == 2
    assert recovered_attempts[0]["status"] == "queued"
    assert client.tables["regular_run_attempts"][0]["status"] == "abandoned"
    assert (
        client.tables["regular_run_attempts"][0]["terminal_reason"]
        == "supervisor_unresponsive"
    )


@pytest.mark.asyncio
async def test_reconcile_expired_attempts_only_requeues_latest_expired_attempt_per_run(
    monkeypatch,
):
    stale_time = datetime(2026, 4, 4, 0, 0, 0)
    recovered_now = datetime(2026, 4, 4, 0, 1, 0)
    client = _FakeClient.with_rows(
        attempt_rows=[
            {
                "attempt_id": "a1",
                "agent_run_id": "run-1",
                "attempt_number": 1,
                "execution_epoch": 1,
                "status": "running",
                "owner_token": "sup-1:slot-1",
                "supervisor_id": "sup-1",
                "heartbeat_at": stale_time,
                "lease_expires_at": None,
            },
            {
                "attempt_id": "a2",
                "agent_run_id": "run-1",
                "attempt_number": 2,
                "execution_epoch": 2,
                "status": "running",
                "owner_token": "sup-2:slot-1",
                "supervisor_id": "sup-2",
                "heartbeat_at": stale_time,
                "lease_expires_at": None,
            },
        ],
        agent_run_rows=[
            {
                "agent_run_id": "run-1",
                "status": "running",
            }
        ],
    )
    monkeypatch.setenv("REGULAR_SUPERVISOR_ATTEMPT_LEASE_TTL_SECONDS", "15")
    monkeypatch.setattr(regular_run_attempts, "_utcnow", lambda: recovered_now)

    recovered_attempts = await regular_run_attempts.reconcile_expired_attempts(
        client,
        recovery_reason="supervisor_unresponsive",
    )

    assert len(recovered_attempts) == 1
    assert recovered_attempts[0]["attempt_number"] == 3
    assert recovered_attempts[0]["execution_epoch"] == 3
    assert [row["status"] for row in client.tables["regular_run_attempts"]] == [
        "abandoned",
        "abandoned",
        "queued",
    ]


@pytest.mark.asyncio
async def test_claim_next_attempt_skips_queued_attempt_when_same_thread_has_running_attempt():
    client = _FakeClient.with_rows(
        attempt_rows=[
            {
                "attempt_id": "attempt-running",
                "agent_run_id": "run-running",
                "attempt_number": 1,
                "execution_epoch": 1,
                "status": "running",
                "queued_at": datetime(2026, 4, 27, 7, 24, 17, tzinfo=timezone.utc),
            },
            {
                "attempt_id": "attempt-next",
                "agent_run_id": "run-next",
                "attempt_number": 1,
                "execution_epoch": 1,
                "status": "queued",
                "queued_at": datetime(2026, 4, 27, 7, 24, 22, tzinfo=timezone.utc),
            },
        ],
        agent_run_rows=[
            {
                "agent_run_id": "run-running",
                "thread_id": "thread-1",
                "status": "running",
            },
            {
                "agent_run_id": "run-next",
                "thread_id": "thread-1",
                "status": "queued",
            },
        ],
    )

    claimed_attempt = await regular_run_attempts.claim_next_attempt(
        client,
        supervisor_id="supervisor-1",
        owner_token="owner-1",
    )

    assert claimed_attempt is None
    assert client.tables["regular_run_attempts"][1]["status"] == "queued"


@pytest.mark.asyncio
async def test_claim_next_attempt_preserves_same_thread_queue_order():
    client = _FakeClient.with_rows(
        attempt_rows=[
            {
                "attempt_id": "attempt-first",
                "agent_run_id": "run-first",
                "attempt_number": 1,
                "execution_epoch": 1,
                "status": "queued",
                "queued_at": datetime(2026, 4, 27, 7, 24, 22, tzinfo=timezone.utc),
            },
            {
                "attempt_id": "attempt-second",
                "agent_run_id": "run-second",
                "attempt_number": 1,
                "execution_epoch": 1,
                "status": "queued",
                "queued_at": datetime(2026, 4, 27, 7, 24, 23, tzinfo=timezone.utc),
            },
        ],
        agent_run_rows=[
            {
                "agent_run_id": "run-first",
                "thread_id": "thread-1",
                "status": "queued",
            },
            {
                "agent_run_id": "run-second",
                "thread_id": "thread-1",
                "status": "queued",
            },
        ],
    )

    claimed_attempt = await regular_run_attempts.claim_next_attempt(
        client,
        supervisor_id="supervisor-1",
        owner_token="owner-1",
    )

    assert claimed_attempt is not None
    assert claimed_attempt["attempt_id"] == "attempt-first"
    assert client.tables["regular_run_attempts"][0]["status"] == "claimed"
    assert client.tables["regular_run_attempts"][1]["status"] == "queued"


@pytest.mark.asyncio
async def test_claim_next_attempt_can_claim_different_thread_when_first_queued_thread_is_busy():
    client = _FakeClient.with_rows(
        attempt_rows=[
            {
                "attempt_id": "attempt-running",
                "agent_run_id": "run-running",
                "attempt_number": 1,
                "execution_epoch": 1,
                "status": "running",
                "queued_at": datetime(2026, 4, 27, 7, 24, 17, tzinfo=timezone.utc),
            },
            {
                "attempt_id": "attempt-busy-thread",
                "agent_run_id": "run-busy-thread",
                "attempt_number": 1,
                "execution_epoch": 1,
                "status": "queued",
                "queued_at": datetime(2026, 4, 27, 7, 24, 22, tzinfo=timezone.utc),
            },
            {
                "attempt_id": "attempt-free-thread",
                "agent_run_id": "run-free-thread",
                "attempt_number": 1,
                "execution_epoch": 1,
                "status": "queued",
                "queued_at": datetime(2026, 4, 27, 7, 24, 23, tzinfo=timezone.utc),
            },
        ],
        agent_run_rows=[
            {
                "agent_run_id": "run-running",
                "thread_id": "thread-1",
                "status": "running",
            },
            {
                "agent_run_id": "run-busy-thread",
                "thread_id": "thread-1",
                "status": "queued",
            },
            {
                "agent_run_id": "run-free-thread",
                "thread_id": "thread-2",
                "status": "queued",
            },
        ],
    )

    claimed_attempt = await regular_run_attempts.claim_next_attempt(
        client,
        supervisor_id="supervisor-1",
        owner_token="owner-1",
    )

    assert claimed_attempt is not None
    assert claimed_attempt["attempt_id"] == "attempt-free-thread"
    assert client.tables["regular_run_attempts"][1]["status"] == "queued"
    assert client.tables["regular_run_attempts"][2]["status"] == "claimed"


@pytest.mark.asyncio
async def test_reconcile_expired_attempts_skips_fresh_running_attempt(monkeypatch):
    heartbeat_at = datetime(2026, 4, 4, 0, 0, 0)
    current_now = datetime(2026, 4, 4, 0, 0, 10)
    client = _FakeClient.with_rows(
        attempt_rows=[
            {
                "attempt_id": "a1",
                "agent_run_id": "run-1",
                "attempt_number": 1,
                "execution_epoch": 1,
                "status": "running",
                "owner_token": "sup-1:slot-1",
                "supervisor_id": "sup-1",
                "heartbeat_at": heartbeat_at,
                "lease_expires_at": datetime(2026, 4, 4, 0, 0, 25),
            }
        ],
        agent_run_rows=[
            {
                "agent_run_id": "run-1",
                "status": "running",
            }
        ],
    )
    monkeypatch.setenv("REGULAR_SUPERVISOR_ATTEMPT_LEASE_TTL_SECONDS", "30")
    monkeypatch.setattr(regular_run_attempts, "_utcnow", lambda: current_now)

    recovered_attempts = await regular_run_attempts.reconcile_expired_attempts(
        client,
        recovery_reason="supervisor_unresponsive",
    )

    assert recovered_attempts == []
    assert client.tables["regular_run_attempts"][0]["status"] == "running"


@pytest.mark.asyncio
async def test_reconcile_expired_attempts_terminalizes_stale_attempt_when_parent_completed(
    monkeypatch,
):
    stale_heartbeat = datetime(2026, 4, 4, 0, 0, 0)
    recovered_now = datetime(2026, 4, 4, 0, 1, 0)
    client = _FakeClient.with_rows(
        attempt_rows=[
            {
                "attempt_id": "a1",
                "agent_run_id": "run-1",
                "attempt_number": 1,
                "execution_epoch": 1,
                "status": "running",
                "owner_token": "sup-1:slot-1",
                "supervisor_id": "sup-1",
                "heartbeat_at": stale_heartbeat,
                "lease_expires_at": None,
            }
        ],
        agent_run_rows=[
            {
                "agent_run_id": "run-1",
                "status": "completed",
                "error": None,
            }
        ],
    )
    monkeypatch.setenv("REGULAR_SUPERVISOR_ATTEMPT_LEASE_TTL_SECONDS", "15")
    monkeypatch.setattr(regular_run_attempts, "_utcnow", lambda: recovered_now)

    recovered_attempts = await regular_run_attempts.reconcile_expired_attempts(
        client,
        recovery_reason="supervisor_unresponsive",
    )

    assert recovered_attempts == []
    assert client.tables["regular_run_attempts"][0]["status"] == "completed"
    assert client.tables["regular_run_attempts"][0]["terminal_status"] == "completed"
    assert (
        client.tables["regular_run_attempts"][0]["terminal_reason"]
        == "parent_run_completed"
    )
