from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import math
import os
import time
import uuid
from typing import Any, Optional

from services import redis
from utils.logger import logger


_LEASES_KEY = "sandbox_create_capacity:leases"
_EXPIRIES_KEY = "sandbox_create_capacity:expiries"
_META_KEY = "sandbox_create_capacity:meta"
_START_WINDOW_KEY = "sandbox_create_capacity:start_window"

_ACQUIRE_POLL_SECONDS = 0.1


def _read_optional_int_env(name: str) -> Optional[int]:
    raw_value = os.getenv(name)
    if raw_value is None:
        return None
    normalized = raw_value.strip().lower()
    if normalized in {"", "none", "null", "off", "disabled"}:
        return None
    try:
        return int(raw_value)
    except (TypeError, ValueError):
        return None


def _read_float_env(name: str, default: float, *, min_value: float = 0.0) -> float:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        parsed = float(raw_value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= min_value else default


def get_sandbox_create_concurrency_budget() -> int:
    explicit_budget = _read_optional_int_env("SANDBOX_CREATE_MAX_CONCURRENT_GLOBAL")
    if explicit_budget is None:
        return 0
    return max(0, explicit_budget)


def is_sandbox_create_capacity_enabled() -> bool:
    return get_sandbox_create_concurrency_budget() > 0


def get_sandbox_create_queue_timeout_seconds() -> float:
    return _read_float_env("SANDBOX_CREATE_QUEUE_TIMEOUT_SECONDS", 120.0, min_value=0.1)


def get_sandbox_create_lease_ttl_seconds() -> int:
    raw_value = _read_optional_int_env("SANDBOX_CREATE_LEASE_TTL_SECONDS")
    if raw_value is None:
        return 180
    return max(30, raw_value)


def get_sandbox_create_min_start_interval_seconds() -> float:
    return _read_float_env(
        "SANDBOX_CREATE_MIN_START_INTERVAL_SECONDS",
        0.0,
        min_value=0.0,
    )


def compute_sandbox_create_lease_ttl_seconds(
    *,
    operation_timeout_seconds: Optional[float],
    queue_timeout_seconds: Optional[float] = None,
) -> int:
    base_ttl_seconds = get_sandbox_create_lease_ttl_seconds()
    resolved_operation_timeout = max(0.0, float(operation_timeout_seconds or 0.0))
    resolved_queue_timeout = max(0.0, float(queue_timeout_seconds or 0.0))
    derived_ttl_seconds = math.ceil(resolved_operation_timeout + resolved_queue_timeout + 10.0)
    return max(base_ttl_seconds, derived_ttl_seconds)


class SandboxCreateCapacityTimeoutError(TimeoutError):
    def __init__(
        self,
        *,
        holder_kind: str,
        project_id: str,
        sandbox_type: str,
        queue_timeout_seconds: float,
    ) -> None:
        self.holder_kind = holder_kind
        self.project_id = project_id
        self.sandbox_type = sandbox_type
        self.queue_timeout_seconds = queue_timeout_seconds
        super().__init__(
            f"Sandbox create capacity timed out after {queue_timeout_seconds:.1f}s "
            f"(holder_kind={holder_kind}, project={project_id}, sandbox_type={sandbox_type})"
        )


class SandboxCreateStartSlotTimeoutError(TimeoutError):
    def __init__(
        self,
        *,
        project_id: str,
        sandbox_type: str,
        queue_timeout_seconds: float,
    ) -> None:
        self.project_id = project_id
        self.sandbox_type = sandbox_type
        self.queue_timeout_seconds = queue_timeout_seconds
        super().__init__(
            f"Sandbox create start slot timed out after {queue_timeout_seconds:.1f}s "
            f"(project={project_id}, sandbox_type={sandbox_type})"
        )


class SandboxCreateCapacityBackendUnavailableError(RuntimeError):
    pass


_ACQUIRE_SANDBOX_CREATE_CAPACITY_LUA = """
local leases_key = KEYS[1]
local expiries_key = KEYS[2]
local meta_key = KEYS[3]
local budget = tonumber(ARGV[1]) or 0
local lease_id = tostring(ARGV[2] or '')
local ttl = tonumber(ARGV[3]) or 0
local metadata_json = tostring(ARGV[4] or '{}')
local now = tonumber(ARGV[5]) or 0

local expired = redis.call('ZRANGEBYSCORE', expiries_key, '-inf', now)
for _, expired_lease_id in ipairs(expired) do
  redis.call('HDEL', leases_key, expired_lease_id)
  redis.call('HDEL', meta_key, expired_lease_id)
end
if #expired > 0 then
  redis.call('ZREM', expiries_key, unpack(expired))
end

local current_total = redis.call('HLEN', leases_key)
if redis.call('HEXISTS', leases_key, lease_id) == 1 then
  redis.call('HSET', meta_key, lease_id, metadata_json)
  redis.call('ZADD', expiries_key, now + ttl, lease_id)
  return {2, current_total, budget}
end

if current_total >= budget then
  return {0, current_total, budget}
end

redis.call('HSET', leases_key, lease_id, '1')
redis.call('HSET', meta_key, lease_id, metadata_json)
redis.call('ZADD', expiries_key, now + ttl, lease_id)
return {1, current_total + 1, budget}
"""

_RELEASE_SANDBOX_CREATE_CAPACITY_LUA = """
local leases_key = KEYS[1]
local expiries_key = KEYS[2]
local meta_key = KEYS[3]
local lease_id = tostring(ARGV[1] or '')
local now = tonumber(ARGV[2]) or 0

local expired = redis.call('ZRANGEBYSCORE', expiries_key, '-inf', now)
for _, expired_lease_id in ipairs(expired) do
  redis.call('HDEL', leases_key, expired_lease_id)
  redis.call('HDEL', meta_key, expired_lease_id)
end
if #expired > 0 then
  redis.call('ZREM', expiries_key, unpack(expired))
end

local released = redis.call('HDEL', leases_key, lease_id)
if released == 1 then
  redis.call('HDEL', meta_key, lease_id)
  redis.call('ZREM', expiries_key, lease_id)
end

return {released, redis.call('HLEN', leases_key)}
"""

_RESERVE_SANDBOX_CREATE_START_SLOT_LUA = """
local start_window_key = KEYS[1]
local interval_ms = tonumber(ARGV[1]) or 0
local now_ms = tonumber(ARGV[2]) or 0

if interval_ms <= 0 then
  return {1, 0}
end

local next_available_at = tonumber(redis.call('GET', start_window_key) or '0')
if next_available_at <= now_ms then
  redis.call('PSETEX', start_window_key, interval_ms, tostring(now_ms + interval_ms))
  return {1, 0}
end

return {0, next_available_at - now_ms}
"""


def _coerce_eval_result(raw_result: Any) -> list[int]:
    if isinstance(raw_result, (list, tuple)):
        coerced: list[int] = []
        for value in raw_result:
            try:
                coerced.append(int(value))
            except (TypeError, ValueError):
                coerced.append(0)
        return coerced
    return [0, 0, 0]


async def try_acquire_sandbox_create_capacity_lease(
    *,
    holder_kind: str,
    project_id: str,
    sandbox_type: str,
    lease_id: str | None = None,
    lease_ttl_seconds: int | None = None,
) -> dict[str, Any]:
    budget = get_sandbox_create_concurrency_budget()
    if budget <= 0:
        return {
            "acquired": True,
            "lease_id": None,
            "budget": 0,
            "in_use": 0,
            "disabled": True,
        }

    resolved_lease_id = str(lease_id or uuid.uuid4())
    resolved_ttl = max(
        30,
        int(lease_ttl_seconds or get_sandbox_create_lease_ttl_seconds()),
    )
    now_seconds = int(time.time())
    metadata_json = json.dumps(
        {
            "holder_kind": str(holder_kind or "").strip() or "unknown",
            "project_id": str(project_id or "").strip(),
            "sandbox_type": str(sandbox_type or "").strip(),
        }
    )

    try:
        raw_result = await redis.eval_script(
            _ACQUIRE_SANDBOX_CREATE_CAPACITY_LUA,
            keys=[_LEASES_KEY, _EXPIRIES_KEY, _META_KEY],
            args=[
                budget,
                resolved_lease_id,
                resolved_ttl,
                metadata_json,
                now_seconds,
            ],
        )
    except Exception as exc:
        raise SandboxCreateCapacityBackendUnavailableError(str(exc)) from exc

    result = _coerce_eval_result(raw_result)
    acquisition_state = result[0] if result else 0
    in_use = result[1] if len(result) > 1 else 0
    reported_budget = result[2] if len(result) > 2 else budget
    return {
        "acquired": acquisition_state in {1, 2},
        "lease_id": resolved_lease_id,
        "in_use": in_use,
        "budget": reported_budget,
        "disabled": False,
    }


async def release_sandbox_create_capacity_lease(*, lease_id: str | None) -> dict[str, Any]:
    normalized_lease_id = str(lease_id or "").strip()
    if not normalized_lease_id:
        return {"released": False, "in_use": 0}

    try:
        raw_result = await redis.eval_script(
            _RELEASE_SANDBOX_CREATE_CAPACITY_LUA,
            keys=[_LEASES_KEY, _EXPIRIES_KEY, _META_KEY],
            args=[normalized_lease_id, int(time.time())],
        )
    except Exception as exc:
        raise SandboxCreateCapacityBackendUnavailableError(str(exc)) from exc

    result = _coerce_eval_result(raw_result)
    released = result[0] if result else 0
    in_use = result[1] if len(result) > 1 else 0
    return {
        "released": bool(released),
        "in_use": in_use,
    }


@asynccontextmanager
async def acquire_sandbox_create_capacity(
    *,
    holder_kind: str,
    project_id: str,
    sandbox_type: str,
    queue_timeout_seconds: float | None = None,
    lease_ttl_seconds: int | None = None,
):
    if not is_sandbox_create_capacity_enabled():
        yield {
            "acquired": True,
            "lease_id": None,
            "budget": 0,
            "in_use": 0,
            "disabled": True,
        }
        return

    resolved_queue_timeout = max(
        0.1,
        float(queue_timeout_seconds or get_sandbox_create_queue_timeout_seconds()),
    )
    started_at = time.monotonic()
    last_result: dict[str, Any] | None = None

    while time.monotonic() - started_at < resolved_queue_timeout:
        last_result = await try_acquire_sandbox_create_capacity_lease(
            holder_kind=holder_kind,
            project_id=project_id,
            sandbox_type=sandbox_type,
            lease_ttl_seconds=lease_ttl_seconds,
        )
        if bool(last_result.get("acquired")):
            try:
                yield last_result
            finally:
                await release_sandbox_create_capacity_lease(
                    lease_id=last_result.get("lease_id")
                )
            return
        await asyncio.sleep(_ACQUIRE_POLL_SECONDS)

    logger.warning(
        "Sandbox create capacity timed out holder_kind=%s project=%s sandbox_type=%s last_result=%s",
        holder_kind,
        project_id,
        sandbox_type,
        last_result,
    )
    raise SandboxCreateCapacityTimeoutError(
        holder_kind=holder_kind,
        project_id=project_id,
        sandbox_type=sandbox_type,
        queue_timeout_seconds=resolved_queue_timeout,
    )


async def wait_for_sandbox_create_start_slot(
    *,
    project_id: str,
    sandbox_type: str,
    queue_timeout_seconds: float | None = None,
    interval_seconds: float | None = None,
) -> float:
    resolved_interval_seconds = max(
        0.0,
        float(
            interval_seconds
            if interval_seconds is not None
            else get_sandbox_create_min_start_interval_seconds()
        ),
    )
    if resolved_interval_seconds <= 0.0:
        return 0.0

    resolved_queue_timeout = max(
        0.1,
        float(queue_timeout_seconds or get_sandbox_create_queue_timeout_seconds()),
    )
    started_at = time.monotonic()

    while time.monotonic() - started_at < resolved_queue_timeout:
        try:
            raw_result = await redis.eval_script(
                _RESERVE_SANDBOX_CREATE_START_SLOT_LUA,
                keys=[_START_WINDOW_KEY],
                args=[
                    int(resolved_interval_seconds * 1000),
                    int(time.time() * 1000),
                ],
            )
        except Exception as exc:
            raise SandboxCreateCapacityBackendUnavailableError(str(exc)) from exc

        result = _coerce_eval_result(raw_result)
        if result and result[0] == 1:
            return round(time.monotonic() - started_at, 3)

        wait_ms = result[1] if len(result) > 1 else int(_ACQUIRE_POLL_SECONDS * 1000)
        sleep_seconds = max(_ACQUIRE_POLL_SECONDS, wait_ms / 1000.0)
        remaining_seconds = resolved_queue_timeout - (time.monotonic() - started_at)
        if remaining_seconds <= 0:
            break
        await asyncio.sleep(min(sleep_seconds, remaining_seconds))

    raise SandboxCreateStartSlotTimeoutError(
        project_id=project_id,
        sandbox_type=sandbox_type,
        queue_timeout_seconds=resolved_queue_timeout,
    )
