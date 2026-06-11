from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
import uuid
from typing import Any

_TRUE_ENV_VALUES = frozenset({"1", "true", "yes", "on"})
_VALID_EXECUTION_MODES = frozenset({"legacy", "phase1_pool", "phase2_supervisor"})
_TERMINAL_RUN_STATUSES = frozenset({"completed", "failed", "stopped"})
_RECOVERABLE_ATTEMPT_STATUSES = frozenset({"claimed", "running"})
_SERVER_SIDE_CLAIM_SQL = """
WITH next_attempt AS (
    SELECT attempts.attempt_id
    FROM regular_run_attempts AS attempts
    JOIN agent_runs AS queued_runs
      ON queued_runs.agent_run_id = attempts.agent_run_id
    WHERE attempts.status = 'queued'
      AND NOT EXISTS (
          SELECT 1
          FROM regular_run_attempts AS active_attempts
          JOIN agent_runs AS active_runs
            ON active_runs.agent_run_id = active_attempts.agent_run_id
          WHERE active_runs.thread_id = queued_runs.thread_id
            AND active_attempts.status IN ('claimed', 'running')
      )
      AND NOT EXISTS (
          SELECT 1
          FROM regular_run_attempts AS earlier_attempts
          JOIN agent_runs AS earlier_runs
            ON earlier_runs.agent_run_id = earlier_attempts.agent_run_id
          WHERE earlier_runs.thread_id = queued_runs.thread_id
            AND earlier_attempts.status = 'queued'
            AND (
                earlier_attempts.queued_at,
                earlier_attempts.attempt_number,
                earlier_attempts.attempt_id
            ) < (
                attempts.queued_at,
                attempts.attempt_number,
                attempts.attempt_id
            )
      )
    ORDER BY attempts.queued_at ASC, attempts.attempt_number ASC, attempts.attempt_id ASC
    FOR UPDATE SKIP LOCKED
    LIMIT 1
)
UPDATE regular_run_attempts AS attempts
SET
    status = 'claimed',
    supervisor_id = $1,
    owner_token = $2,
    claimed_at = $3,
    heartbeat_at = $3,
    lease_expires_at = $4
FROM next_attempt
WHERE attempts.attempt_id = next_attempt.attempt_id
  AND attempts.status = 'queued'
RETURNING attempts.*;
""".strip()


class RegularAttemptQueueFullError(RuntimeError):
    """Raised when the durable regular attempt queue is at capacity."""


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _normalize_status(value: Any) -> str:
    return str(value or "").strip().lower()


def _safe_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _coerce_metadata(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        normalized_value = value.strip()
        if not normalized_value:
            return {}
        try:
            parsed_value = json.loads(normalized_value)
        except Exception:
            return {}
        return dict(parsed_value) if isinstance(parsed_value, dict) else {}
    return {}


def _latest_attempt_sort_key(row: dict[str, Any]) -> tuple[int, int, str]:
    return (
        _safe_int(row.get("execution_epoch")),
        _safe_int(row.get("attempt_number")),
        str(row.get("attempt_id") or ""),
    )


def _serialize_json_field(value: Any) -> Any:
    if isinstance(value, (dict, list)):
        return json.dumps(value)
    return value


def _normalize_optional_text(value: Any) -> str | None:
    normalized_value = str(value or "").strip()
    return normalized_value or None


def _recoverable_attempt_statuses() -> list[str]:
    return sorted(_RECOVERABLE_ATTEMPT_STATUSES)


async def _load_agent_run_thread_map(
    client: Any,
    *,
    agent_run_ids: list[str],
) -> dict[str, str | None]:
    normalized_agent_run_ids = [
        str(agent_run_id or "").strip()
        for agent_run_id in agent_run_ids
        if str(agent_run_id or "").strip()
    ]
    if not normalized_agent_run_ids:
        return {}

    result = (
        await client.table("agent_runs")
        .select("agent_run_id, thread_id")
        .in_("agent_run_id", normalized_agent_run_ids)
        .execute()
    )
    rows = list(getattr(result, "data", None) or [])
    thread_by_run_id: dict[str, str | None] = {}
    for row in rows:
        agent_run_id = str(row.get("agent_run_id") or "").strip()
        if not agent_run_id:
            continue
        thread_by_run_id[agent_run_id] = _normalize_optional_text(row.get("thread_id"))
    return thread_by_run_id


async def _select_claimable_queued_attempt(
    client: Any,
    *,
    queued_rows: list[dict[str, Any]],
) -> dict[str, Any] | None:
    queued_agent_run_ids = [str(row.get("agent_run_id") or "").strip() for row in queued_rows]
    active_result = (
        await client.table("regular_run_attempts")
        .select("attempt_id, agent_run_id, status")
        .in_("status", _recoverable_attempt_statuses())
        .execute()
    )
    active_rows = list(getattr(active_result, "data", None) or [])
    active_agent_run_ids = [str(row.get("agent_run_id") or "").strip() for row in active_rows]
    thread_by_run_id = await _load_agent_run_thread_map(
        client,
        agent_run_ids=[*queued_agent_run_ids, *active_agent_run_ids],
    )
    busy_thread_ids = {
        thread_id
        for row in active_rows
        for thread_id in [thread_by_run_id.get(str(row.get("agent_run_id") or "").strip())]
        if thread_id
    }

    for queued_row in queued_rows:
        queued_agent_run_id = str(queued_row.get("agent_run_id") or "").strip()
        queued_thread_id = thread_by_run_id.get(queued_agent_run_id)
        if queued_thread_id and queued_thread_id in busy_thread_ids:
            continue
        return dict(queued_row)
    return None


def _build_stop_terminal_reason(
    *,
    error_message: str | None,
) -> str:
    normalized_error_message = str(error_message or "").strip()
    if normalized_error_message:
        return normalized_error_message
    return "external_stop"


def _build_terminal_reason_from_parent_run(
    *,
    parent_status: str,
    parent_error: Any,
) -> str:
    normalized_parent_status = _normalize_status(parent_status)
    normalized_parent_error = str(parent_error or "").strip()
    if normalized_parent_status == "failed":
        return normalized_parent_error or "parent_run_failed"
    if normalized_parent_status == "stopped":
        return _build_stop_terminal_reason(
            error_message=normalized_parent_error or None,
        )
    if normalized_parent_status == "completed":
        return "parent_run_completed"
    raise ValueError("parent_status must be terminal")


def _read_positive_int_env(name: str, default: int) -> int:
    raw_value = str(os.getenv(name, str(default))).strip()
    try:
        parsed_value = int(raw_value)
    except (TypeError, ValueError):
        return default
    return max(1, parsed_value)


def get_regular_supervisor_attempt_lease_ttl_seconds() -> int:
    return _read_positive_int_env(
        "REGULAR_SUPERVISOR_ATTEMPT_LEASE_TTL_SECONDS",
        30,
    )


def _build_attempt_lease_expires_at(*, current_time: datetime) -> datetime:
    return current_time + timedelta(
        seconds=get_regular_supervisor_attempt_lease_ttl_seconds()
    )


async def _await_mutation_result(operation: Any) -> Any:
    if hasattr(operation, "execute"):
        return await operation.execute()
    if hasattr(operation, "__await__"):
        return await operation
    return operation


async def _claim_next_attempt_via_server_cursor(
    client: Any,
    *,
    supervisor_id: str,
    owner_token: str,
) -> dict[str, Any] | None:
    pool = getattr(client, "pool", None)
    if pool is None or not hasattr(pool, "acquire"):
        return None

    claim_time = _utcnow()
    lease_expires_at = _build_attempt_lease_expires_at(current_time=claim_time)
    async with pool.acquire() as connection:
        row = await connection.fetchrow(
            _SERVER_SIDE_CLAIM_SQL,
            supervisor_id,
            owner_token,
            claim_time,
            lease_expires_at,
        )
    if row is None:
        return None
    return dict(row)


def get_regular_execution_mode() -> str:
    configured_mode = str(os.getenv("REGULAR_EXECUTION_MODE", "")).strip().lower()
    if configured_mode in _VALID_EXECUTION_MODES:
        return configured_mode

    phase1_alias_enabled = (
        str(os.getenv("REGULAR_ASYNC_POOL_ENABLED", "false")).strip().lower()
        in _TRUE_ENV_VALUES
    )
    if phase1_alias_enabled:
        return "phase1_pool"
    return "legacy"


def get_regular_supervisor_queue_max_depth() -> int:
    if os.getenv("REGULAR_SUPERVISOR_QUEUE_MAX_DEPTH") is not None:
        return _read_positive_int_env("REGULAR_SUPERVISOR_QUEUE_MAX_DEPTH", 32)
    return _read_positive_int_env("REGULAR_QUEUE_MAX_DEPTH", 32)


async def ensure_attempt_queue_capacity(client: Any, additional_slots: int = 1) -> None:
    queued_result = (
        await client.table("regular_run_attempts")
        .select("attempt_id, status")
        .eq("status", "queued")
        .execute()
    )
    queued_rows = list(getattr(queued_result, "data", None) or [])
    requested_slots = max(0, int(additional_slots))
    if len(queued_rows) + requested_slots > get_regular_supervisor_queue_max_depth():
        raise RegularAttemptQueueFullError("regular attempt queue depth exhausted")


async def create_initial_attempt(
    client: Any,
    *,
    agent_run_id: str,
) -> dict[str, Any]:
    normalized_agent_run_id = str(agent_run_id or "").strip()
    if not normalized_agent_run_id:
        raise ValueError("agent_run_id is required")

    payload = {
        "attempt_id": str(uuid.uuid4()),
        "agent_run_id": normalized_agent_run_id,
        "attempt_number": 1,
        "execution_epoch": 1,
        "status": "queued",
        "queued_at": _utcnow(),
        "metadata": _serialize_json_field({}),
    }
    result = await _await_mutation_result(
        client.table("regular_run_attempts").insert(payload)
    )
    rows = list(getattr(result, "data", None) or [])
    if not rows:
        raise RuntimeError("failed to create initial regular run attempt")
    inserted_attempt = dict(rows[0])
    await _update_parent_run_execution_pointer(
        client,
        agent_run_id=normalized_agent_run_id,
        active_attempt_id=None,
        current_execution_epoch=_safe_int(inserted_attempt.get("execution_epoch")) or None,
        stream_source_epoch=_safe_int(inserted_attempt.get("execution_epoch")) or None,
        active_supervisor_id=None,
    )
    return inserted_attempt


async def load_attempt(
    client: Any,
    *,
    attempt_id: str,
) -> dict[str, Any] | None:
    normalized_attempt_id = str(attempt_id or "").strip()
    if not normalized_attempt_id:
        raise ValueError("attempt_id is required")

    result = (
        await client.table("regular_run_attempts")
        .select("*")
        .eq("attempt_id", normalized_attempt_id)
        .execute()
    )
    rows = list(getattr(result, "data", None) or [])
    if not rows:
        return None
    return dict(rows[0])


async def load_latest_attempt_for_run(
    client: Any,
    *,
    agent_run_id: str,
) -> dict[str, Any] | None:
    normalized_agent_run_id = str(agent_run_id or "").strip()
    if not normalized_agent_run_id:
        raise ValueError("agent_run_id is required")

    result = (
        await client.table("regular_run_attempts")
        .select("*")
        .eq("agent_run_id", normalized_agent_run_id)
        .execute()
    )
    rows = [dict(row) for row in (getattr(result, "data", None) or [])]
    if not rows:
        return None
    return max(rows, key=_latest_attempt_sort_key)


async def _load_parent_run_row(
    client: Any,
    *,
    agent_run_id: str,
) -> dict[str, Any] | None:
    normalized_agent_run_id = str(agent_run_id or "").strip()
    if not normalized_agent_run_id:
        raise ValueError("agent_run_id is required")

    result = (
        await client.table("agent_runs")
        .select(
            "agent_run_id, status, error, started_at, "
            "active_attempt_id, current_execution_epoch, stream_source_epoch, "
            "active_supervisor_id, regular_execution_backend"
        )
        .eq("agent_run_id", normalized_agent_run_id)
        .execute()
    )
    rows = list(getattr(result, "data", None) or [])
    if not rows:
        return None
    return dict(rows[0])


async def _update_parent_run_execution_pointer(
    client: Any,
    *,
    agent_run_id: str,
    active_attempt_id: str | None,
    current_execution_epoch: int | None,
    stream_source_epoch: int | None,
    active_supervisor_id: str | None,
) -> dict[str, Any] | None:
    parent_run_row = await _load_parent_run_row(client, agent_run_id=agent_run_id)
    if parent_run_row is None:
        return None

    parent_execution_epoch = _safe_int(parent_run_row.get("current_execution_epoch"))
    requested_execution_epoch = _safe_int(current_execution_epoch)
    resolved_execution_epoch = max(parent_execution_epoch, requested_execution_epoch) or None

    parent_stream_source_epoch = _safe_int(parent_run_row.get("stream_source_epoch"))
    requested_stream_source_epoch = _safe_int(stream_source_epoch)
    resolved_stream_source_epoch = max(
        parent_stream_source_epoch,
        requested_stream_source_epoch,
    ) or None

    requested_attempt_id = _normalize_optional_text(active_attempt_id)
    requested_supervisor_id = _normalize_optional_text(active_supervisor_id)
    parent_active_attempt_id = _normalize_optional_text(
        parent_run_row.get("active_attempt_id")
    )
    parent_active_supervisor_id = _normalize_optional_text(
        parent_run_row.get("active_supervisor_id")
    )

    if requested_execution_epoch >= parent_execution_epoch:
        resolved_active_attempt_id = requested_attempt_id
        resolved_active_supervisor_id = requested_supervisor_id
    else:
        resolved_active_attempt_id = parent_active_attempt_id
        resolved_active_supervisor_id = parent_active_supervisor_id

    update_result = await _await_mutation_result(
        client.table("agent_runs")
        .eq("agent_run_id", agent_run_id)
        .update(
            {
                "active_attempt_id": resolved_active_attempt_id,
                "current_execution_epoch": resolved_execution_epoch,
                "stream_source_epoch": resolved_stream_source_epoch,
                "active_supervisor_id": resolved_active_supervisor_id,
                "regular_execution_backend": "phase2_supervisor",
            }
        )
    )
    rows = list(getattr(update_result, "data", None) or [])
    if not rows:
        return parent_run_row
    return dict(rows[0])


async def claim_next_attempt(
    client: Any,
    *,
    supervisor_id: str,
    owner_token: str,
) -> dict[str, Any] | None:
    normalized_supervisor_id = str(supervisor_id or "").strip()
    normalized_owner_token = str(owner_token or "").strip()
    if not normalized_supervisor_id:
        raise ValueError("supervisor_id is required")
    if not normalized_owner_token:
        raise ValueError("owner_token is required")

    server_claim = await _claim_next_attempt_via_server_cursor(
        client,
        supervisor_id=normalized_supervisor_id,
        owner_token=normalized_owner_token,
    )
    if server_claim is not None:
        return server_claim

    queued_result = (
        await client.table("regular_run_attempts")
        .select(
            "attempt_id, agent_run_id, attempt_number, execution_epoch, status, queued_at"
        )
        .eq("status", "queued")
        .order("queued_at")
        .execute()
    )
    queued_rows = list(getattr(queued_result, "data", None) or [])
    if not queued_rows:
        return None

    queued_row = await _select_claimable_queued_attempt(
        client,
        queued_rows=[dict(row) for row in queued_rows],
    )
    if queued_row is None:
        return None

    claim_time = _utcnow()
    claim_payload = {
        "status": "claimed",
        "supervisor_id": normalized_supervisor_id,
        "owner_token": normalized_owner_token,
        "claimed_at": claim_time,
        "heartbeat_at": claim_time,
        "lease_expires_at": _build_attempt_lease_expires_at(current_time=claim_time),
    }
    claim_result = await _await_mutation_result(
        client.table("regular_run_attempts")
        .eq("attempt_id", queued_row["attempt_id"])
        .eq("status", "queued")
        .update(claim_payload)
    )
    claimed_rows = list(getattr(claim_result, "data", None) or [])
    if not claimed_rows:
        return None
    return dict(claimed_rows[0])


async def mark_attempt_running(
    client: Any,
    *,
    attempt_id: str,
) -> dict[str, Any] | None:
    current_attempt = await load_attempt(client, attempt_id=attempt_id)
    if current_attempt is None:
        return None

    current_status = _normalize_status(current_attempt.get("status"))
    if current_status == "running":
        return current_attempt
    if current_status != "claimed":
        return None

    parent_run_row = await _load_parent_run_row(
        client,
        agent_run_id=str(current_attempt.get("agent_run_id") or ""),
    )
    if parent_run_row is None:
        return None

    parent_status = _normalize_status(parent_run_row.get("status"))
    if parent_status in _TERMINAL_RUN_STATUSES:
        await mark_attempt_terminal(
            client,
            attempt_id=str(current_attempt.get("attempt_id") or ""),
            final_status=parent_status,
            terminal_reason=_build_terminal_reason_from_parent_run(
                parent_status=parent_status,
                parent_error=parent_run_row.get("error"),
            ),
        )
        return None

    running_at = _utcnow()
    update_result = await _await_mutation_result(
        client.table("regular_run_attempts")
        .eq("attempt_id", str(current_attempt["attempt_id"]))
        .eq("status", current_attempt.get("status"))
        .update(
            {
                "status": "running",
                "started_at": running_at,
                "heartbeat_at": running_at,
                "lease_expires_at": _build_attempt_lease_expires_at(
                    current_time=running_at
                ),
            }
        )
    )
    rows = list(getattr(update_result, "data", None) or [])
    if not rows:
        return None
    running_attempt = dict(rows[0])

    if parent_status != "running":
        parent_update_result = await _await_mutation_result(
            client.table("agent_runs")
            .eq("agent_run_id", str(current_attempt.get("agent_run_id") or ""))
            .eq("status", parent_run_row.get("status"))
            .update(
                {
                    "status": "running",
                    "started_at": running_at,
                }
            )
        )
        updated_parent_rows = list(getattr(parent_update_result, "data", None) or [])
        if not updated_parent_rows:
            latest_parent_row = await _load_parent_run_row(
                client,
                agent_run_id=str(current_attempt.get("agent_run_id") or ""),
            )
            latest_parent_status = _normalize_status(
                latest_parent_row.get("status") if latest_parent_row else None
            )
            if latest_parent_status in _TERMINAL_RUN_STATUSES:
                await mark_attempt_terminal(
                    client,
                    attempt_id=str(current_attempt.get("attempt_id") or ""),
                    final_status=latest_parent_status,
                    terminal_reason=_build_terminal_reason_from_parent_run(
                        parent_status=latest_parent_status,
                        parent_error=(
                            latest_parent_row.get("error")
                            if latest_parent_row is not None
                            else None
                        ),
                    ),
                )
                return None
            if latest_parent_status != "running":
                await _await_mutation_result(
                    client.table("agent_runs")
                    .eq("agent_run_id", str(current_attempt.get("agent_run_id") or ""))
                    .update(
                        {
                            "status": "running",
                            "started_at": running_at,
                        }
                    )
                )

    await _update_parent_run_execution_pointer(
        client,
        agent_run_id=str(running_attempt.get("agent_run_id") or ""),
        active_attempt_id=_normalize_optional_text(running_attempt.get("attempt_id")),
        current_execution_epoch=_safe_int(running_attempt.get("execution_epoch")) or None,
        stream_source_epoch=_safe_int(running_attempt.get("execution_epoch")) or None,
        active_supervisor_id=_normalize_optional_text(running_attempt.get("supervisor_id")),
    )
    return running_attempt


async def refresh_attempt_heartbeat(
    client: Any,
    *,
    attempt_id: str,
    owner_token: str,
) -> dict[str, Any] | None:
    normalized_attempt_id = str(attempt_id or "").strip()
    normalized_owner_token = str(owner_token or "").strip()
    if not normalized_attempt_id:
        raise ValueError("attempt_id is required")
    if not normalized_owner_token:
        raise ValueError("owner_token is required")

    current_attempt = await load_attempt(client, attempt_id=normalized_attempt_id)
    if current_attempt is None:
        return None
    if _normalize_status(current_attempt.get("status")) not in _RECOVERABLE_ATTEMPT_STATUSES:
        return None
    if str(current_attempt.get("owner_token") or "").strip() != normalized_owner_token:
        return None

    refresh_time = _utcnow()
    update_result = await _await_mutation_result(
        client.table("regular_run_attempts")
        .eq("attempt_id", normalized_attempt_id)
        .eq("owner_token", normalized_owner_token)
        .eq("status", current_attempt.get("status"))
        .update(
            {
                "heartbeat_at": refresh_time,
                "lease_expires_at": _build_attempt_lease_expires_at(
                    current_time=refresh_time
                ),
            }
        )
    )
    rows = list(getattr(update_result, "data", None) or [])
    if not rows:
        return None
    return dict(rows[0])


async def mark_attempt_abandoned(
    client: Any,
    *,
    attempt_id: str,
    reason: str,
) -> dict[str, Any] | None:
    normalized_reason = str(reason or "").strip()
    if not normalized_reason:
        raise ValueError("reason is required")

    current_attempt = await load_attempt(client, attempt_id=attempt_id)
    if current_attempt is None:
        return None

    current_status = _normalize_status(current_attempt.get("status"))
    if current_status not in _RECOVERABLE_ATTEMPT_STATUSES:
        return None

    update_result = await _await_mutation_result(
        client.table("regular_run_attempts")
        .eq("attempt_id", str(current_attempt["attempt_id"]))
        .eq("status", current_attempt.get("status"))
        .update(
            {
                "status": "abandoned",
                "terminal_reason": normalized_reason,
                "recovery_reason": normalized_reason,
            }
        )
    )
    rows = list(getattr(update_result, "data", None) or [])
    if not rows:
        return None
    terminal_attempt = dict(rows[0])
    await _update_parent_run_execution_pointer(
        client,
        agent_run_id=str(terminal_attempt.get("agent_run_id") or ""),
        active_attempt_id=None,
        current_execution_epoch=_safe_int(terminal_attempt.get("execution_epoch")) or None,
        stream_source_epoch=_safe_int(terminal_attempt.get("execution_epoch")) or None,
        active_supervisor_id=None,
    )
    return terminal_attempt


async def mark_attempt_terminal(
    client: Any,
    *,
    attempt_id: str,
    final_status: str,
    terminal_reason: str,
) -> dict[str, Any] | None:
    normalized_final_status = _normalize_status(final_status)
    normalized_terminal_reason = str(terminal_reason or "").strip()
    if normalized_final_status not in _TERMINAL_RUN_STATUSES:
        raise ValueError("final_status must be terminal")
    if not normalized_terminal_reason:
        raise ValueError("terminal_reason is required")

    current_attempt = await load_attempt(client, attempt_id=attempt_id)
    if current_attempt is None:
        return None

    current_status = _normalize_status(current_attempt.get("status"))
    if current_status == normalized_final_status:
        return current_attempt
    if current_status not in _RECOVERABLE_ATTEMPT_STATUSES:
        return None

    current_metadata = _coerce_metadata(current_attempt.get("metadata"))
    updated_metadata = {
        **current_metadata,
        "terminal_status": normalized_final_status,
        "terminal_reason": normalized_terminal_reason,
        "terminalized_at": _utcnow().isoformat(),
    }
    if normalized_final_status == "stopped":
        updated_metadata["stop_requested"] = True
        updated_metadata["stop_reason"] = normalized_terminal_reason
        updated_metadata["stop_requested_at"] = _utcnow().isoformat()
    update_result = await _await_mutation_result(
        client.table("regular_run_attempts")
        .eq("attempt_id", str(current_attempt["attempt_id"]))
        .eq("status", current_attempt.get("status"))
        .update(
            {
                "status": normalized_final_status,
                "terminal_status": normalized_final_status,
                "terminal_reason": normalized_terminal_reason,
                "metadata": _serialize_json_field(updated_metadata),
            }
        )
    )
    rows = list(getattr(update_result, "data", None) or [])
    if not rows:
        return None
    return dict(rows[0])


async def finalize_completed_attempt(
    client: Any,
    *,
    attempt_id: str,
) -> dict[str, Any] | None:
    current_attempt = await load_attempt(client, attempt_id=attempt_id)
    if current_attempt is None:
        return None

    normalized_agent_run_id = str(current_attempt.get("agent_run_id") or "").strip()
    if not normalized_agent_run_id:
        return None

    parent_run_result = (
        await client.table("agent_runs")
        .select("status, error")
        .eq("agent_run_id", normalized_agent_run_id)
        .execute()
    )
    parent_run_rows = list(getattr(parent_run_result, "data", None) or [])
    if not parent_run_rows:
        return None

    parent_run_row = dict(parent_run_rows[0])
    parent_status = _normalize_status(parent_run_row.get("status"))
    if parent_status not in _TERMINAL_RUN_STATUSES:
        return None

    current_status = _normalize_status(current_attempt.get("status"))
    if current_status == parent_status and current_status in _TERMINAL_RUN_STATUSES:
        return current_attempt
    if current_status not in _RECOVERABLE_ATTEMPT_STATUSES:
        return None

    return await mark_attempt_terminal(
        client,
        attempt_id=str(current_attempt.get("attempt_id") or ""),
        final_status=parent_status,
        terminal_reason=_build_terminal_reason_from_parent_run(
            parent_status=parent_status,
            parent_error=parent_run_row.get("error"),
        ),
    )


async def mark_current_attempt_terminal(
    client: Any,
    *,
    agent_run_id: str,
    final_status: str,
    error_message: str | None,
) -> dict[str, Any] | None:
    return await mark_current_attempt_terminal_on_stop(
        client,
        agent_run_id=agent_run_id,
        final_status=final_status,
        reason=_build_stop_terminal_reason(error_message=error_message),
    )


async def mark_current_attempt_terminal_on_stop(
    client: Any,
    *,
    agent_run_id: str,
    final_status: str,
    reason: str,
) -> dict[str, Any] | None:
    normalized_agent_run_id = str(agent_run_id or "").strip()
    if not normalized_agent_run_id:
        raise ValueError("agent_run_id is required")

    result = (
        await client.table("regular_run_attempts")
        .select("*")
        .eq("agent_run_id", normalized_agent_run_id)
        .execute()
    )
    attempt_rows = list(getattr(result, "data", None) or [])
    recoverable_attempts = [
        dict(row)
        for row in attempt_rows
        if _normalize_status(row.get("status")) in _RECOVERABLE_ATTEMPT_STATUSES
    ]
    if not recoverable_attempts:
        return None

    current_attempt = max(
        recoverable_attempts,
        key=lambda row: (
            _safe_int(row.get("execution_epoch")),
            _safe_int(row.get("attempt_number")),
            str(row.get("attempt_id") or ""),
        ),
    )
    return await mark_attempt_terminal(
        client,
        attempt_id=str(current_attempt.get("attempt_id") or ""),
        final_status=final_status,
        terminal_reason=str(reason or "").strip(),
    )


def _select_latest_recoverable_attempt(
    attempt_rows: list[dict[str, Any]],
) -> dict[str, Any] | None:
    recoverable_attempts = [
        dict(row)
        for row in attempt_rows
        if _normalize_status(row.get("status")) in _RECOVERABLE_ATTEMPT_STATUSES
    ]
    if not recoverable_attempts:
        return None
    return max(recoverable_attempts, key=_latest_attempt_sort_key)


async def parent_run_is_terminal(
    client: Any,
    *,
    agent_run_id: str,
) -> bool:
    normalized_agent_run_id = str(agent_run_id or "").strip()
    if not normalized_agent_run_id:
        raise ValueError("agent_run_id is required")

    result = (
        await client.table("agent_runs")
        .select("status")
        .eq("agent_run_id", normalized_agent_run_id)
        .execute()
    )
    rows = list(getattr(result, "data", None) or [])
    if not rows:
        return False
    return _normalize_status(rows[0].get("status")) in _TERMINAL_RUN_STATUSES


async def has_recoverable_attempts(client: Any) -> bool:
    if not hasattr(client, "table"):
        return False
    result = (
        await client.table("regular_run_attempts")
        .select("attempt_id, status")
        .in_("status", _recoverable_attempt_statuses())
        .limit(1)
        .execute()
    )
    attempt_rows = list(getattr(result, "data", None) or [])
    return any(
        _normalize_status(row.get("status")) in _RECOVERABLE_ATTEMPT_STATUSES
        for row in attempt_rows
    )


async def create_retry_attempt(
    client: Any,
    *,
    agent_run_id: str,
    previous_attempt: dict[str, Any],
    recovery_reason: str,
) -> dict[str, Any]:
    normalized_agent_run_id = str(agent_run_id or "").strip()
    normalized_reason = str(recovery_reason or "").strip()
    if not normalized_agent_run_id:
        raise ValueError("agent_run_id is required")
    if not normalized_reason:
        raise ValueError("recovery_reason is required")

    attempts_result = (
        await client.table("regular_run_attempts")
        .select("attempt_number, execution_epoch")
        .eq("agent_run_id", normalized_agent_run_id)
        .execute()
    )
    attempt_rows = list(getattr(attempts_result, "data", None) or [])

    next_attempt_number = max(
        [_safe_int(row.get("attempt_number")) for row in attempt_rows] or [0]
    ) + 1
    next_execution_epoch = max(
        [_safe_int(row.get("execution_epoch")) for row in attempt_rows] or [0]
    ) + 1
    payload = {
        "attempt_id": str(uuid.uuid4()),
        "agent_run_id": normalized_agent_run_id,
        "attempt_number": next_attempt_number,
        "execution_epoch": next_execution_epoch,
        "status": "queued",
        "queued_at": _utcnow(),
        "recovery_reason": normalized_reason,
        "metadata": _serialize_json_field(
            {
                "recovery_from_attempt_id": str(
                    previous_attempt.get("attempt_id") or ""
                ),
            }
        ),
    }
    result = await _await_mutation_result(
        client.table("regular_run_attempts").insert(payload)
    )
    rows = list(getattr(result, "data", None) or [])
    if not rows:
        raise RuntimeError("failed to create retry regular run attempt")
    retry_attempt = dict(rows[0])
    await _update_parent_run_execution_pointer(
        client,
        agent_run_id=normalized_agent_run_id,
        active_attempt_id=None,
        current_execution_epoch=_safe_int(retry_attempt.get("execution_epoch")) or None,
        stream_source_epoch=_safe_int(previous_attempt.get("execution_epoch")) or None,
        active_supervisor_id=None,
    )
    return retry_attempt


async def reconcile_lost_attempt(
    client: Any,
    *,
    attempt_id: str,
    recovery_reason: str,
) -> dict[str, Any] | None:
    normalized_attempt_id = str(attempt_id or "").strip()
    normalized_reason = str(recovery_reason or "").strip()
    if not normalized_attempt_id:
        raise ValueError("attempt_id is required")
    if not normalized_reason:
        raise ValueError("recovery_reason is required")

    current_attempt = await load_attempt(client, attempt_id=normalized_attempt_id)
    if current_attempt is None:
        return None
    if _normalize_status(current_attempt.get("status")) not in _RECOVERABLE_ATTEMPT_STATUSES:
        return None

    attempts_result = (
        await client.table("regular_run_attempts")
        .select("*")
        .eq("agent_run_id", str(current_attempt.get("agent_run_id") or ""))
        .in_("status", _recoverable_attempt_statuses())
        .execute()
    )
    latest_recoverable_attempt = _select_latest_recoverable_attempt(
        list(getattr(attempts_result, "data", None) or [])
    )
    latest_recoverable_attempt_id = str(
        latest_recoverable_attempt.get("attempt_id") or ""
    ).strip() if latest_recoverable_attempt else ""
    if latest_recoverable_attempt_id and latest_recoverable_attempt_id != normalized_attempt_id:
        await mark_attempt_abandoned(
            client,
            attempt_id=normalized_attempt_id,
            reason=normalized_reason,
        )
        return None

    parent_run_row = await _load_parent_run_row(
        client,
        agent_run_id=str(current_attempt.get("agent_run_id") or ""),
    )
    parent_run_status = _normalize_status(parent_run_row.get("status") if parent_run_row else None)
    if parent_run_status in _TERMINAL_RUN_STATUSES:
        await mark_attempt_terminal(
            client,
            attempt_id=normalized_attempt_id,
            final_status=parent_run_status,
            terminal_reason=_build_terminal_reason_from_parent_run(
                parent_status=parent_run_status,
                parent_error=parent_run_row.get("error") if parent_run_row else None,
            ),
        )
        return None

    abandoned_attempt = await mark_attempt_abandoned(
        client,
        attempt_id=normalized_attempt_id,
        reason=normalized_reason,
    )
    if abandoned_attempt is None:
        return None

    return await create_retry_attempt(
        client,
        agent_run_id=str(current_attempt.get("agent_run_id") or ""),
        previous_attempt=current_attempt,
        recovery_reason=normalized_reason,
    )


def _row_is_expired(
    row: dict[str, Any],
    *,
    current_time: datetime,
) -> bool:
    if _normalize_status(row.get("status")) not in _RECOVERABLE_ATTEMPT_STATUSES:
        return False

    lease_expires_at = row.get("lease_expires_at")
    if isinstance(lease_expires_at, datetime):
        return lease_expires_at <= current_time

    last_activity = (
        row.get("heartbeat_at")
        or row.get("started_at")
        or row.get("claimed_at")
        or row.get("queued_at")
    )
    if not isinstance(last_activity, datetime):
        return False
    expiration_cutoff = current_time - timedelta(
        seconds=get_regular_supervisor_attempt_lease_ttl_seconds()
    )
    return last_activity <= expiration_cutoff


async def reconcile_expired_attempts(
    client: Any,
    *,
    recovery_reason: str,
) -> list[dict[str, Any]]:
    if not hasattr(client, "table"):
        return []
    normalized_reason = str(recovery_reason or "").strip()
    if not normalized_reason:
        raise ValueError("recovery_reason is required")

    current_time = _utcnow()
    expiring_result = (
        await client.table("regular_run_attempts")
        .select("*")
        .in_("status", _recoverable_attempt_statuses())
        .lte("lease_expires_at", current_time)
        .execute()
    )
    expiring_rows = [dict(row) for row in (getattr(expiring_result, "data", None) or [])]
    no_lease_result = (
        await client.table("regular_run_attempts")
        .select("*")
        .in_("status", _recoverable_attempt_statuses())
        .is_("lease_expires_at", None)
        .execute()
    )
    no_lease_rows = [
        dict(row)
        for row in (getattr(no_lease_result, "data", None) or [])
        if _row_is_expired(dict(row), current_time=current_time)
    ]
    attempt_rows_by_id: dict[str, dict[str, Any]] = {}
    for row in [*expiring_rows, *no_lease_rows]:
        attempt_id = str(row.get("attempt_id") or "").strip()
        if not attempt_id:
            continue
        attempt_rows_by_id[attempt_id] = row

    attempt_rows = sorted(
        attempt_rows_by_id.values(),
        key=lambda row: (
            _safe_int(row.get("attempt_number")),
            _safe_int(row.get("execution_epoch")),
            str(row.get("attempt_id") or ""),
        ),
    )
    recovered_attempts: list[dict[str, Any]] = []
    for attempt_row in attempt_rows:
        recovered_attempt = await reconcile_lost_attempt(
            client,
            attempt_id=str(attempt_row.get("attempt_id") or ""),
            recovery_reason=normalized_reason,
        )
        if recovered_attempt is not None:
            recovered_attempts.append(recovered_attempt)
    return recovered_attempts
