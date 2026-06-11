from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import os
import threading
from typing import Any

from services import redis
from utils.logger import logger

_TRUE_ENV_VALUES = frozenset({"1", "true", "yes", "on"})
_OWNER_LOST_ERROR = "regular_pool_owner_lost"
_DISPATCHER_LOCK = threading.Lock()
_DISPATCHER_LOCK_OWNER: str | None = None


class RegularQueueFullError(RuntimeError):
    """Raised when the durable regular queue is already at capacity."""


def _get_positive_int_env(name: str, default: int) -> int:
    raw_value = str(os.getenv(name, str(default))).strip()
    try:
        parsed_value = int(raw_value)
    except (TypeError, ValueError):
        return default
    return max(1, parsed_value)


def _get_positive_float_env(name: str, default: float) -> float:
    raw_value = str(os.getenv(name, str(default))).strip()
    try:
        parsed_value = float(raw_value)
    except (TypeError, ValueError):
        return default
    return max(0.01, parsed_value)


def is_regular_async_pool_enabled(mode: Any) -> bool:
    enabled = str(os.getenv("REGULAR_ASYNC_POOL_ENABLED", "false")).strip().lower()
    if enabled not in _TRUE_ENV_VALUES:
        return False

    normalized_mode = str(getattr(mode, "value", mode) or "").strip().lower()
    return normalized_mode in {"", "off"}


def get_regular_async_pool_size_per_process() -> int:
    return _get_positive_int_env("REGULAR_ASYNC_POOL_SIZE_PER_PROCESS", 2)


def get_regular_queue_max_depth() -> int:
    return _get_positive_int_env("REGULAR_QUEUE_MAX_DEPTH", 32)


def get_regular_async_pool_heartbeat_interval_seconds() -> float:
    return _get_positive_float_env(
        "REGULAR_ASYNC_POOL_HEARTBEAT_INTERVAL_SECONDS",
        15.0,
    )


def get_regular_async_pool_claim_ttl_seconds() -> int:
    default_ttl_seconds = _get_positive_int_env(
        "AGENTSCOPE_SERVER_RUN_CAPACITY_TTL_SECONDS",
        90,
    )
    return _get_positive_int_env(
        "REGULAR_ASYNC_POOL_CLAIM_TTL_SECONDS",
        default_ttl_seconds,
    )


def get_regular_async_pool_orphan_grace_seconds() -> int:
    default_grace_seconds = max(
        30,
        int(get_regular_async_pool_heartbeat_interval_seconds() * 2) + 1,
    )
    return _get_positive_int_env(
        "REGULAR_ASYNC_POOL_ORPHAN_GRACE_SECONDS",
        default_grace_seconds,
    )


def get_regular_async_pool_idle_reconcile_interval_seconds() -> int:
    return _get_positive_int_env(
        "REGULAR_ASYNC_POOL_IDLE_RECONCILE_INTERVAL_SECONDS",
        30,
    )


def _parse_metadata(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        raw_value = value.strip()
        if not raw_value:
            return {}
        try:
            parsed_value = json.loads(raw_value)
        except json.JSONDecodeError:
            return {}
        if isinstance(parsed_value, dict):
            return parsed_value
    return {}


def _is_regular_mode_row(row: dict[str, Any]) -> bool:
    metadata = _parse_metadata(row.get("metadata"))
    shadow_clone_mode = str(metadata.get("shadow_clone_mode") or "").strip().lower()
    return shadow_clone_mode in {"", "off"}


def _is_regular_async_pool_row(row: dict[str, Any]) -> bool:
    metadata = _parse_metadata(row.get("metadata"))
    return _is_regular_mode_row(row) and bool(metadata.get("regular_async_pool"))


def _regular_run_claim_key(agent_run_id: str) -> str:
    normalized_run_id = str(agent_run_id or "").strip()
    if not normalized_run_id:
        raise ValueError("agent_run_id is required")
    return f"regular_async_pool:claim:{normalized_run_id}"


def _idle_dispatch_scheduled_key() -> str:
    return "regular_async_pool:idle_dispatch_scheduled"


def _coerce_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        normalized_value = value.strip()
        if not normalized_value:
            return None
        try:
            parsed_value = datetime.fromisoformat(
                normalized_value.replace("Z", "+00:00")
            )
        except ValueError:
            return None
        return parsed_value if parsed_value.tzinfo else parsed_value.replace(
            tzinfo=timezone.utc
        )
    return None


def _is_within_orphan_grace_window(row: dict[str, Any]) -> bool:
    started_at = _coerce_datetime(row.get("started_at"))
    if started_at is None:
        return False
    age_seconds = (datetime.now(timezone.utc) - started_at).total_seconds()
    return age_seconds < float(get_regular_async_pool_orphan_grace_seconds())


async def ensure_regular_queue_capacity(client: Any, additional_slots: int = 1) -> None:
    queued_result = (
        await client.table("agent_runs")
        .select("agent_run_id, metadata")
        .eq("status", "queued")
        .execute()
    )
    queued_rows = list(getattr(queued_result, "data", None) or [])
    regular_queued_rows = [row for row in queued_rows if _is_regular_mode_row(row)]
    requested_slots = max(0, int(additional_slots))
    if len(regular_queued_rows) + requested_slots > get_regular_queue_max_depth():
        raise RegularQueueFullError("regular queue depth exhausted")


async def claim_next_regular_run(
    client: Any, owner_token: str
) -> dict[str, Any] | None:
    _ = owner_token
    queued_result = (
        await client.table("agent_runs")
        .select(
            "agent_run_id, thread_id, project_id, status, created_at, metadata, started_at"
        )
        .eq("status", "queued")
        .order("created_at")
        .execute()
    )
    queued_rows = list(getattr(queued_result, "data", None) or [])
    queued_row = next(
        (dict(row) for row in queued_rows if _is_regular_mode_row(row)),
        None,
    )
    if queued_row is None:
        return None

    started_at = datetime.now(timezone.utc).isoformat()
    claimed_result = (
        await client.table("agent_runs")
        .eq("agent_run_id", queued_row["agent_run_id"])
        .eq("status", "queued")
        .update({"status": "running", "started_at": started_at})
        .execute()
    )
    claimed_rows = list(getattr(claimed_result, "data", None) or [])
    if not claimed_rows:
        return None
    return dict(claimed_rows[0])


async def heartbeat_regular_run_claim(
    agent_run_id: str, owner_token: str
) -> dict[str, Any]:
    heartbeat_at = datetime.now(timezone.utc).isoformat()
    claim_key = _regular_run_claim_key(agent_run_id)
    await redis.hset(
        claim_key,
        mapping={
            "owner_token": str(owner_token or "").strip(),
            "heartbeat_at": heartbeat_at,
        },
    )
    await redis.expire(claim_key, get_regular_async_pool_claim_ttl_seconds())
    return {
        "agent_run_id": agent_run_id,
        "owner_token": owner_token,
        "heartbeat_at": heartbeat_at,
        "refreshed": True,
    }


async def _read_regular_run_claim(agent_run_id: str) -> dict[str, Any]:
    claim_key = _regular_run_claim_key(agent_run_id)
    claim = await redis.hgetall(claim_key)
    if not claim:
        return {}
    return {str(key): value for key, value in dict(claim).items()}


async def find_orphaned_regular_async_pool_run_ids(client: Any) -> list[str]:
    running_result = (
        await client.table("agent_runs")
        .select("agent_run_id, status, metadata, started_at")
        .eq("status", "running")
        .execute()
    )
    running_rows = list(getattr(running_result, "data", None) or [])
    orphaned_run_ids: list[str] = []
    for row in running_rows:
        current_row = dict(row)
        if not _is_regular_async_pool_row(current_row):
            continue
        if _is_within_orphan_grace_window(current_row):
            continue
        run_id = str(current_row.get("agent_run_id") or "").strip()
        if not run_id:
            continue
        claim = await _read_regular_run_claim(run_id)
        if not claim:
            orphaned_run_ids.append(run_id)
    return orphaned_run_ids


async def clear_idle_dispatch_wakeup() -> bool:
    deleted = await redis.delete(_idle_dispatch_scheduled_key())
    return bool(deleted)


async def schedule_idle_dispatch_wakeup(*, actor_handle: Any) -> bool:
    delay_seconds = get_regular_async_pool_idle_reconcile_interval_seconds()
    scheduled = await redis.set(
        _idle_dispatch_scheduled_key(),
        "1",
        ex=delay_seconds + 15,
        nx=True,
    )
    if not scheduled:
        return False
    try:
        actor_handle.send_with_options(
            kwargs={"trigger": "idle"},
            delay=delay_seconds * 1000,
        )
    except Exception as schedule_error:
        await redis.delete(_idle_dispatch_scheduled_key())
        logger.warning(
            "Failed to enqueue delayed regular async pool idle wakeup: %s",
            schedule_error,
        )
        return False
    return True


async def try_acquire_dispatcher_lock(owner_token: str) -> dict[str, Any]:
    global _DISPATCHER_LOCK_OWNER

    normalized_owner = str(owner_token or "").strip()
    if not normalized_owner:
        raise ValueError("owner_token is required")

    with _DISPATCHER_LOCK:
        current_owner = _DISPATCHER_LOCK_OWNER
        if current_owner and current_owner != normalized_owner:
            return {
                "acquired": False,
                "owner_token": current_owner,
            }
        _DISPATCHER_LOCK_OWNER = normalized_owner
        return {
            "acquired": True,
            "owner_token": normalized_owner,
        }


async def release_dispatcher_lock(owner_token: str) -> dict[str, Any]:
    global _DISPATCHER_LOCK_OWNER

    normalized_owner = str(owner_token or "").strip()
    with _DISPATCHER_LOCK:
        if _DISPATCHER_LOCK_OWNER != normalized_owner:
            return {
                "released": False,
                "owner_conflict": bool(_DISPATCHER_LOCK_OWNER),
            }
        _DISPATCHER_LOCK_OWNER = None
        return {
            "released": True,
            "owner_conflict": False,
        }


async def run_dispatch_loop(
    client: Any,
    executor: Any,
    owner_token: str,
) -> list[str]:
    pool_size = get_regular_async_pool_size_per_process()
    heartbeat_interval_seconds = get_regular_async_pool_heartbeat_interval_seconds()
    completed_run_ids: list[str] = []
    in_flight: dict[asyncio.Task, str] = {}

    async def _start_one() -> bool:
        claimed_run = await claim_next_regular_run(client, owner_token)
        if claimed_run is None:
            return False

        run_id = str(claimed_run.get("agent_run_id") or "").strip()
        if not run_id:
            return False

        await heartbeat_regular_run_claim(run_id, owner_token)
        task = asyncio.create_task(executor(dict(claimed_run), owner_token))
        in_flight[task] = run_id
        return True

    async def _refresh_in_flight_heartbeats() -> None:
        for task, run_id in tuple(in_flight.items()):
            if task.done():
                continue
            try:
                await heartbeat_regular_run_claim(run_id, owner_token)
            except Exception as heartbeat_error:
                logger.warning(
                    "Failed to refresh regular async pool heartbeat for %s owner=%s: %s",
                    run_id,
                    owner_token,
                    heartbeat_error,
                )

    while len(in_flight) < pool_size and await _start_one():
        continue

    while in_flight:
        done, _pending = await asyncio.wait(
            tuple(in_flight.keys()),
            return_when=asyncio.FIRST_COMPLETED,
            timeout=heartbeat_interval_seconds,
        )
        if not done:
            await _refresh_in_flight_heartbeats()
            continue
        for task in done:
            run_id = in_flight.pop(task)
            try:
                await asyncio.shield(task)
            except Exception as executor_error:
                logger.warning(
                    "Regular async pool executor failed for %s owner=%s: %s",
                    run_id,
                    owner_token,
                    executor_error,
                )
            completed_run_ids.append(run_id)

        while len(in_flight) < pool_size and await _start_one():
            continue

    return completed_run_ids


async def reconcile_expired_regular_run_claims(
    client: Any,
    expired_run_ids: list[str],
) -> list[str]:
    reconciled_run_ids: list[str] = []
    for run_id in expired_run_ids:
        result = (
            await client.table("agent_runs")
            .eq("agent_run_id", run_id)
            .eq("status", "running")
            .update(
                {
                    "status": "failed",
                    "error": _OWNER_LOST_ERROR,
                }
            )
            .execute()
        )
        if getattr(result, "data", None):
            reconciled_run_ids.append(run_id)
    return reconciled_run_ids
