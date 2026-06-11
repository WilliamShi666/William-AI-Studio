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


_LEASES_KEY = "sandbox_global_capacity:leases"
_EXPIRIES_KEY = "sandbox_global_capacity:expiries"
_META_KEY = "sandbox_global_capacity:meta"
_PER_SANDBOX_LEASES_KEY = "sandbox_per_sandbox_capacity:leases"
_PER_SANDBOX_EXPIRIES_KEY = "sandbox_per_sandbox_capacity:expiries"
_PER_SANDBOX_META_KEY = "sandbox_per_sandbox_capacity:meta"
_PER_SANDBOX_COUNTS_KEY = "sandbox_per_sandbox_capacity:counts"

_ACQUIRE_POLL_SECONDS = 0.1


def _read_int_env(name: str, default: int, *, min_value: int = 0) -> int:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        parsed = int(raw_value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= min_value else default


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


def get_sandbox_global_concurrency_budget() -> int:
    explicit_budget = _read_optional_int_env("SANDBOX_SHARED_MAX_CONCURRENT_GLOBAL")
    if explicit_budget is None:
        return 0
    return max(0, explicit_budget)


def is_sandbox_global_capacity_enabled() -> bool:
    return get_sandbox_global_concurrency_budget() > 0


def get_sandbox_per_sandbox_concurrency_budget() -> int:
    explicit_budget = _read_optional_int_env("SANDBOX_SHARED_MAX_CONCURRENT_PER_SANDBOX")
    if explicit_budget is None:
        return 0
    return max(0, explicit_budget)


def is_sandbox_per_sandbox_capacity_enabled() -> bool:
    return get_sandbox_per_sandbox_concurrency_budget() > 0


def get_sandbox_global_capacity_lease_ttl_seconds() -> int:
    return _read_int_env("SANDBOX_SHARED_LEASE_TTL_SECONDS", 120, min_value=15)


def _normalize_lease_ttl_seconds(lease_ttl_seconds: Optional[int]) -> int:
    if lease_ttl_seconds is None:
        return get_sandbox_global_capacity_lease_ttl_seconds()
    try:
        parsed = int(lease_ttl_seconds)
    except (TypeError, ValueError):
        return get_sandbox_global_capacity_lease_ttl_seconds()
    return max(15, parsed)


def compute_sandbox_capacity_lease_ttl_seconds(
    *,
    operation_timeout_seconds: Optional[float],
    queue_timeout_seconds: Optional[float] = None,
) -> int:
    base_ttl_seconds = get_sandbox_global_capacity_lease_ttl_seconds()
    resolved_operation_timeout = max(0.0, float(operation_timeout_seconds or 0.0))
    resolved_queue_timeout = max(0.0, float(queue_timeout_seconds or 0.0))
    derived_ttl_seconds = math.ceil(resolved_operation_timeout + resolved_queue_timeout + 10.0)
    return max(base_ttl_seconds, derived_ttl_seconds)


class SandboxGlobalCapacityTimeoutError(TimeoutError):
    def __init__(
        self,
        *,
        holder_kind: str,
        sandbox_id: str,
        operation: str,
        queue_timeout_seconds: float,
    ) -> None:
        self.holder_kind = holder_kind
        self.sandbox_id = sandbox_id
        self.operation = operation
        self.queue_timeout_seconds = queue_timeout_seconds
        super().__init__(
            f"Sandbox global capacity timed out after {queue_timeout_seconds:.1f}s "
            f"(holder_kind={holder_kind}, sandbox={sandbox_id}, operation={operation})"
        )


class SandboxGlobalCapacityBackendUnavailableError(RuntimeError):
    pass


class SandboxPerSandboxCapacityTimeoutError(TimeoutError):
    def __init__(
        self,
        *,
        holder_kind: str,
        sandbox_id: str,
        operation: str,
        queue_timeout_seconds: float,
    ) -> None:
        self.holder_kind = holder_kind
        self.sandbox_id = sandbox_id
        self.operation = operation
        self.queue_timeout_seconds = queue_timeout_seconds
        super().__init__(
            f"Sandbox per-sandbox capacity timed out after {queue_timeout_seconds:.1f}s "
            f"(holder_kind={holder_kind}, sandbox={sandbox_id}, operation={operation})"
        )


class SandboxPerSandboxCapacityBackendUnavailableError(RuntimeError):
    pass


_PRUNE_EXPIRED_LEASES_LUA = """
local leases_key = KEYS[1]
local expiries_key = KEYS[2]
local meta_key = KEYS[3]
local now = tonumber(ARGV[1]) or 0

local expired = redis.call('ZRANGEBYSCORE', expiries_key, '-inf', now)
for _, lease_id in ipairs(expired) do
  redis.call('HDEL', leases_key, lease_id)
  redis.call('HDEL', meta_key, lease_id)
end
if #expired > 0 then
  redis.call('ZREM', expiries_key, unpack(expired))
end

return redis.call('HLEN', leases_key)
"""

_ACQUIRE_SANDBOX_CAPACITY_LUA = """
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

_RELEASE_SANDBOX_CAPACITY_LUA = """
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

_PRUNE_SANDBOX_PER_SANDBOX_CAPACITY_LUA = """
local leases_key = KEYS[1]
local expiries_key = KEYS[2]
local meta_key = KEYS[3]
local counts_key = KEYS[4]
local now = tonumber(ARGV[1]) or 0
local sandbox_id = tostring(ARGV[2] or '')

local expired = redis.call('ZRANGEBYSCORE', expiries_key, '-inf', now)
for _, lease_id in ipairs(expired) do
  local expired_sandbox_id = tostring(redis.call('HGET', leases_key, lease_id) or '')
  if expired_sandbox_id ~= '' then
    local remaining = redis.call('HINCRBY', counts_key, expired_sandbox_id, -1)
    if remaining <= 0 then
      redis.call('HDEL', counts_key, expired_sandbox_id)
    end
  end
  redis.call('HDEL', leases_key, lease_id)
  redis.call('HDEL', meta_key, lease_id)
end
if #expired > 0 then
  redis.call('ZREM', expiries_key, unpack(expired))
end

if sandbox_id == '' then
  return 0
end
return tonumber(redis.call('HGET', counts_key, sandbox_id) or '0')
"""

_ACQUIRE_SANDBOX_PER_SANDBOX_CAPACITY_LUA = """
local leases_key = KEYS[1]
local expiries_key = KEYS[2]
local meta_key = KEYS[3]
local counts_key = KEYS[4]
local budget = tonumber(ARGV[1]) or 0
local lease_id = tostring(ARGV[2] or '')
local sandbox_id = tostring(ARGV[3] or '')
local ttl = tonumber(ARGV[4]) or 0
local metadata_json = tostring(ARGV[5] or '{}')
local now = tonumber(ARGV[6]) or 0

local expired = redis.call('ZRANGEBYSCORE', expiries_key, '-inf', now)
for _, expired_lease_id in ipairs(expired) do
  local expired_sandbox_id = tostring(redis.call('HGET', leases_key, expired_lease_id) or '')
  if expired_sandbox_id ~= '' then
    local remaining = redis.call('HINCRBY', counts_key, expired_sandbox_id, -1)
    if remaining <= 0 then
      redis.call('HDEL', counts_key, expired_sandbox_id)
    end
  end
  redis.call('HDEL', leases_key, expired_lease_id)
  redis.call('HDEL', meta_key, expired_lease_id)
end
if #expired > 0 then
  redis.call('ZREM', expiries_key, unpack(expired))
end

local current_count = tonumber(redis.call('HGET', counts_key, sandbox_id) or '0')
local existing_sandbox_id = tostring(redis.call('HGET', leases_key, lease_id) or '')
if existing_sandbox_id ~= '' then
  if existing_sandbox_id == sandbox_id then
    redis.call('HSET', meta_key, lease_id, metadata_json)
    redis.call('ZADD', expiries_key, now + ttl, lease_id)
    return {2, current_count, budget}
  end
  return {3, current_count, budget}
end

if current_count >= budget then
  return {0, current_count, budget}
end

redis.call('HSET', leases_key, lease_id, sandbox_id)
redis.call('HSET', meta_key, lease_id, metadata_json)
redis.call('HINCRBY', counts_key, sandbox_id, 1)
redis.call('ZADD', expiries_key, now + ttl, lease_id)
return {1, current_count + 1, budget}
"""

_RELEASE_SANDBOX_PER_SANDBOX_CAPACITY_LUA = """
local leases_key = KEYS[1]
local expiries_key = KEYS[2]
local meta_key = KEYS[3]
local counts_key = KEYS[4]
local lease_id = tostring(ARGV[1] or '')
local sandbox_id = tostring(ARGV[2] or '')
local now = tonumber(ARGV[3]) or 0

local expired = redis.call('ZRANGEBYSCORE', expiries_key, '-inf', now)
for _, expired_lease_id in ipairs(expired) do
  local expired_sandbox_id = tostring(redis.call('HGET', leases_key, expired_lease_id) or '')
  if expired_sandbox_id ~= '' then
    local remaining = redis.call('HINCRBY', counts_key, expired_sandbox_id, -1)
    if remaining <= 0 then
      redis.call('HDEL', counts_key, expired_sandbox_id)
    end
  end
  redis.call('HDEL', leases_key, expired_lease_id)
  redis.call('HDEL', meta_key, expired_lease_id)
end
if #expired > 0 then
  redis.call('ZREM', expiries_key, unpack(expired))
end

local existing_sandbox_id = tostring(redis.call('HGET', leases_key, lease_id) or '')
if existing_sandbox_id == '' or (sandbox_id ~= '' and existing_sandbox_id ~= sandbox_id) then
  return {0, tonumber(redis.call('HGET', counts_key, sandbox_id) or '0')}
end

redis.call('HDEL', leases_key, lease_id)
redis.call('HDEL', meta_key, lease_id)
redis.call('ZREM', expiries_key, lease_id)
local remaining = redis.call('HINCRBY', counts_key, existing_sandbox_id, -1)
if remaining <= 0 then
  redis.call('HDEL', counts_key, existing_sandbox_id)
  remaining = 0
end
return {1, remaining}
"""


def _coerce_eval_result(raw_result: Any) -> list[int]:
    if isinstance(raw_result, (list, tuple)):
        normalized_result = []
        for item in raw_result:
            try:
                normalized_result.append(int(item))
            except (TypeError, ValueError):
                normalized_result.append(0)
        return normalized_result
    try:
        return [int(raw_result)]
    except (TypeError, ValueError):
        return [0]


def _build_snapshot(*, budget: int, in_use: int) -> dict[str, Any]:
    return {
        "enabled": budget > 0,
        "budget": max(0, budget),
        "in_use": max(0, in_use),
        "remaining": max(0, budget - max(0, in_use)),
    }


async def try_acquire_sandbox_global_capacity_lease(
    *,
    lease_id: str,
    holder_kind: str,
    sandbox_id: str,
    operation: str,
    lease_ttl_seconds: Optional[int] = None,
) -> dict[str, Any]:
    budget = get_sandbox_global_concurrency_budget()
    if budget <= 0:
        return {
            **_build_snapshot(budget=0, in_use=0),
            "acquired": True,
            "lease_ttl_seconds": 0,
        }

    normalized_lease_id = str(lease_id or "").strip()
    if not normalized_lease_id:
        raise ValueError("lease_id is required to acquire sandbox global capacity")

    resolved_lease_ttl_seconds = _normalize_lease_ttl_seconds(lease_ttl_seconds)
    metadata_json = json.dumps(
        {
            "holder_kind": str(holder_kind or "").strip() or "unknown",
            "sandbox_id": str(sandbox_id or "").strip() or "unknown",
            "operation": str(operation or "").strip() or "unknown",
        }
    )
    current_epoch_seconds = int(time.time())

    try:
        raw_result = await redis.eval_script(
            _ACQUIRE_SANDBOX_CAPACITY_LUA,
            keys=[_LEASES_KEY, _EXPIRIES_KEY, _META_KEY],
            args=[
                str(budget),
                normalized_lease_id,
                str(resolved_lease_ttl_seconds),
                metadata_json,
                str(current_epoch_seconds),
            ],
        )
    except Exception as redis_error:
        raise SandboxGlobalCapacityBackendUnavailableError(
            f"sandbox global capacity acquire failed: {redis_error}"
        ) from redis_error

    status_code, in_use_after, resolved_budget = (_coerce_eval_result(raw_result) + [0, 0, 0])[:3]
    return {
        **_build_snapshot(budget=resolved_budget or budget, in_use=in_use_after),
        "acquired": status_code in {1, 2},
        "lease_ttl_seconds": resolved_lease_ttl_seconds,
    }


async def release_sandbox_global_capacity_lease(
    *,
    lease_id: str,
) -> dict[str, Any]:
    budget = get_sandbox_global_concurrency_budget()
    normalized_lease_id = str(lease_id or "").strip()
    if budget <= 0 or not normalized_lease_id:
        return {
            **_build_snapshot(budget=budget, in_use=0),
            "released": False,
        }

    try:
        raw_result = await redis.eval_script(
            _RELEASE_SANDBOX_CAPACITY_LUA,
            keys=[_LEASES_KEY, _EXPIRIES_KEY, _META_KEY],
            args=[normalized_lease_id, str(int(time.time()))],
        )
    except Exception as redis_error:
        raise SandboxGlobalCapacityBackendUnavailableError(
            f"sandbox global capacity release failed: {redis_error}"
        ) from redis_error

    released, in_use_after = (_coerce_eval_result(raw_result) + [0, 0])[:2]
    return {
        **_build_snapshot(budget=budget, in_use=in_use_after),
        "released": released == 1,
    }


async def try_acquire_sandbox_per_sandbox_capacity_lease(
    *,
    lease_id: str,
    holder_kind: str,
    sandbox_id: str,
    operation: str,
    lease_ttl_seconds: Optional[int] = None,
) -> dict[str, Any]:
    budget = get_sandbox_per_sandbox_concurrency_budget()
    if budget <= 0:
        return {
            **_build_snapshot(budget=0, in_use=0),
            "acquired": True,
            "lease_ttl_seconds": 0,
        }

    normalized_lease_id = str(lease_id or "").strip()
    if not normalized_lease_id:
        raise ValueError("lease_id is required to acquire sandbox per-sandbox capacity")

    normalized_sandbox_id = str(sandbox_id or "").strip()
    if not normalized_sandbox_id:
        raise ValueError("sandbox_id is required to acquire sandbox per-sandbox capacity")

    resolved_lease_ttl_seconds = _normalize_lease_ttl_seconds(lease_ttl_seconds)
    metadata_json = json.dumps(
        {
            "holder_kind": str(holder_kind or "").strip() or "unknown",
            "sandbox_id": normalized_sandbox_id,
            "operation": str(operation or "").strip() or "unknown",
        }
    )
    current_epoch_seconds = int(time.time())

    try:
        raw_result = await redis.eval_script(
            _ACQUIRE_SANDBOX_PER_SANDBOX_CAPACITY_LUA,
            keys=[
                _PER_SANDBOX_LEASES_KEY,
                _PER_SANDBOX_EXPIRIES_KEY,
                _PER_SANDBOX_META_KEY,
                _PER_SANDBOX_COUNTS_KEY,
            ],
            args=[
                str(budget),
                normalized_lease_id,
                normalized_sandbox_id,
                str(resolved_lease_ttl_seconds),
                metadata_json,
                str(current_epoch_seconds),
            ],
        )
    except Exception as redis_error:
        raise SandboxPerSandboxCapacityBackendUnavailableError(
            f"sandbox per-sandbox capacity acquire failed: {redis_error}"
        ) from redis_error

    status_code, in_use_after, resolved_budget = (_coerce_eval_result(raw_result) + [0, 0, 0])[:3]
    return {
        **_build_snapshot(budget=resolved_budget or budget, in_use=in_use_after),
        "acquired": status_code in {1, 2},
        "lease_ttl_seconds": resolved_lease_ttl_seconds,
    }


async def release_sandbox_per_sandbox_capacity_lease(
    *,
    lease_id: str,
    sandbox_id: str,
) -> dict[str, Any]:
    budget = get_sandbox_per_sandbox_concurrency_budget()
    normalized_lease_id = str(lease_id or "").strip()
    normalized_sandbox_id = str(sandbox_id or "").strip()
    if budget <= 0 or not normalized_lease_id or not normalized_sandbox_id:
        return {
            **_build_snapshot(budget=budget, in_use=0),
            "released": False,
        }

    try:
        raw_result = await redis.eval_script(
            _RELEASE_SANDBOX_PER_SANDBOX_CAPACITY_LUA,
            keys=[
                _PER_SANDBOX_LEASES_KEY,
                _PER_SANDBOX_EXPIRIES_KEY,
                _PER_SANDBOX_META_KEY,
                _PER_SANDBOX_COUNTS_KEY,
            ],
            args=[normalized_lease_id, normalized_sandbox_id, str(int(time.time()))],
        )
    except Exception as redis_error:
        raise SandboxPerSandboxCapacityBackendUnavailableError(
            f"sandbox per-sandbox capacity release failed: {redis_error}"
        ) from redis_error

    released, in_use_after = (_coerce_eval_result(raw_result) + [0, 0])[:2]
    return {
        **_build_snapshot(budget=budget, in_use=in_use_after),
        "released": released == 1,
    }


@asynccontextmanager
async def acquire_sandbox_global_capacity(
    *,
    holder_kind: str,
    sandbox_id: str,
    operation: str,
    queue_timeout_seconds: float,
    lease_ttl_seconds: Optional[int] = None,
    retain_lease_on_exceptions: tuple[type[BaseException], ...] = (),
):
    if not is_sandbox_global_capacity_enabled():
        yield
        return

    lease_id = str(uuid.uuid4())
    deadline = time.monotonic() + max(0.0, queue_timeout_seconds)

    while True:
        acquire_result = await try_acquire_sandbox_global_capacity_lease(
            lease_id=lease_id,
            holder_kind=holder_kind,
            sandbox_id=sandbox_id,
            operation=operation,
            lease_ttl_seconds=lease_ttl_seconds,
        )
        if bool(acquire_result.get("acquired")):
            break

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SandboxGlobalCapacityTimeoutError(
                holder_kind=holder_kind,
                sandbox_id=sandbox_id,
                operation=operation,
                queue_timeout_seconds=queue_timeout_seconds,
            )
        await asyncio.sleep(min(_ACQUIRE_POLL_SECONDS, remaining))

    release_on_exit = True
    try:
        try:
            yield
        except BaseException as exc:
            if retain_lease_on_exceptions and isinstance(exc, retain_lease_on_exceptions):
                release_on_exit = False
                logger.warning(
                    "Retaining sandbox global capacity lease %s until TTL expiry after %s "
                    "(holder_kind=%s sandbox=%s operation=%s)",
                    lease_id,
                    type(exc).__name__,
                    holder_kind,
                    sandbox_id,
                    operation,
                )
            raise
    finally:
        if release_on_exit:
            try:
                await release_sandbox_global_capacity_lease(lease_id=lease_id)
            except Exception as release_error:
                logger.warning(
                    "Failed to release sandbox global capacity lease %s: %s",
                    lease_id,
                    release_error,
                )


@asynccontextmanager
async def acquire_sandbox_per_sandbox_capacity(
    *,
    holder_kind: str,
    sandbox_id: str,
    operation: str,
    queue_timeout_seconds: float,
    lease_ttl_seconds: Optional[int] = None,
    retain_lease_on_exceptions: tuple[type[BaseException], ...] = (),
):
    if not is_sandbox_per_sandbox_capacity_enabled():
        yield
        return

    normalized_sandbox_id = str(sandbox_id or "").strip()
    if not normalized_sandbox_id:
        raise ValueError("sandbox_id is required to acquire sandbox per-sandbox capacity")

    lease_id = str(uuid.uuid4())
    deadline = time.monotonic() + max(0.0, queue_timeout_seconds)

    while True:
        acquire_result = await try_acquire_sandbox_per_sandbox_capacity_lease(
            lease_id=lease_id,
            holder_kind=holder_kind,
            sandbox_id=normalized_sandbox_id,
            operation=operation,
            lease_ttl_seconds=lease_ttl_seconds,
        )
        if bool(acquire_result.get("acquired")):
            break

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SandboxPerSandboxCapacityTimeoutError(
                holder_kind=holder_kind,
                sandbox_id=normalized_sandbox_id,
                operation=operation,
                queue_timeout_seconds=queue_timeout_seconds,
            )
        await asyncio.sleep(min(_ACQUIRE_POLL_SECONDS, remaining))

    release_on_exit = True
    try:
        try:
            yield
        except BaseException as exc:
            if retain_lease_on_exceptions and isinstance(exc, retain_lease_on_exceptions):
                release_on_exit = False
                logger.warning(
                    "Retaining sandbox per-sandbox capacity lease %s until TTL expiry after %s "
                    "(holder_kind=%s sandbox=%s operation=%s)",
                    lease_id,
                    type(exc).__name__,
                    holder_kind,
                    normalized_sandbox_id,
                    operation,
                )
            raise
    finally:
        if release_on_exit:
            try:
                await release_sandbox_per_sandbox_capacity_lease(
                    lease_id=lease_id,
                    sandbox_id=normalized_sandbox_id,
                )
            except Exception as release_error:
                logger.warning(
                    "Failed to release sandbox per-sandbox capacity lease %s for sandbox %s: %s",
                    lease_id,
                    normalized_sandbox_id,
                    release_error,
                )
