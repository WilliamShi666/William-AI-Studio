from __future__ import annotations

import os
import time
from typing import Any, Optional

from services import redis
from utils.logger import logger


_LEASES_KEY = "server_run_capacity:leases"
_OWNERS_KEY = "server_run_capacity:owners"
_EXPIRIES_KEY = "server_run_capacity:expiries"

_SHADOW_CLONE_REQUESTED_MODES = frozenset({"on", "auto", "v2"})
_LOCAL_SUBAGENT_EXECUTION_MODES = frozenset({"local"})


def _read_int_env(name: str, default: int, *, min_value: int = 0) -> int:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        parsed = int(raw_value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= min_value else min_value


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


def _normalize_shadow_clone_mode(mode: Any) -> str:
    if hasattr(mode, "value"):
        mode = getattr(mode, "value")
    return str(mode or "").strip().lower()


def is_shadow_clone_capacity_mode(mode: Any) -> bool:
    return _normalize_shadow_clone_mode(mode) in _SHADOW_CLONE_REQUESTED_MODES


def _get_default_shadow_clone_run_cost() -> int:
    execution_mode = str(
        os.getenv("SHADOW_CLONE_SUBAGENT_EXECUTION_MODE") or ""
    ).strip().lower()
    if execution_mode in _LOCAL_SUBAGENT_EXECUTION_MODES:
        return 2
    return 3


def get_run_capacity_budget(mode: Any) -> int:
    total_budget = get_server_concurrency_budget()
    if total_budget <= 0:
        return 0
    if is_shadow_clone_capacity_mode(mode):
        return total_budget

    regular_admission_budget = _read_optional_int_env(
        "AGENTSCOPE_SERVER_REGULAR_ADMISSION_BUDGET"
    )
    if regular_admission_budget is None:
        return total_budget
    return min(total_budget, max(0, regular_admission_budget))


def get_server_concurrency_budget() -> int:
    explicit_budget = _read_optional_int_env("AGENTSCOPE_SERVER_CONCURRENCY_BUDGET")
    if explicit_budget is not None:
        return max(0, explicit_budget)

    dramatiq_processes = _read_int_env("DRAMATIQ_PROCESSES", 0)
    dramatiq_threads = _read_int_env("DRAMATIQ_THREADS", 0)
    if dramatiq_processes > 0 and dramatiq_threads > 0:
        return dramatiq_processes * dramatiq_threads

    return _read_int_env("AGENTSCOPE_SERVER_CONCURRENCY_BUDGET_FALLBACK", 16)


def is_server_concurrency_budget_enabled() -> bool:
    return get_server_concurrency_budget() > 0


def get_run_capacity_cost(mode: Any) -> int:
    if is_shadow_clone_capacity_mode(mode):
        return _read_int_env(
            "AGENTSCOPE_SERVER_SHADOW_CLONE_RUN_COST",
            _get_default_shadow_clone_run_cost(),
            min_value=1,
        )
    return _read_int_env("AGENTSCOPE_SERVER_REGULAR_RUN_COST", 1, min_value=1)


def get_run_capacity_kind(mode: Any) -> str:
    return "shadow_clone" if is_shadow_clone_capacity_mode(mode) else "regular"


def get_run_capacity_ttl_seconds() -> int:
    return _read_int_env("AGENTSCOPE_SERVER_RUN_CAPACITY_TTL_SECONDS", 90, min_value=15)


_PRUNE_EXPIRED_LEASES_LUA = """
local leases_key = KEYS[1]
local owners_key = KEYS[2]
local expiries_key = KEYS[3]
local now = tonumber(ARGV[1]) or 0

local expired = redis.call('ZRANGEBYSCORE', expiries_key, '-inf', now)
for _, run_id in ipairs(expired) do
  redis.call('HDEL', leases_key, run_id)
  redis.call('HDEL', owners_key, run_id)
end
if #expired > 0 then
  redis.call('ZREM', expiries_key, unpack(expired))
end

local current_total = 0
local current_values = redis.call('HVALS', leases_key)
for _, raw_cost in ipairs(current_values) do
  current_total = current_total + (tonumber(raw_cost) or 0)
end

return current_total
"""

_ACQUIRE_RUN_CAPACITY_LUA = """
local leases_key = KEYS[1]
local owners_key = KEYS[2]
local expiries_key = KEYS[3]
local budget = tonumber(ARGV[1]) or 0
local run_id = tostring(ARGV[2] or '')
local owner_token = tostring(ARGV[3] or '')
local requested_cost = tonumber(ARGV[4]) or 0
local ttl = tonumber(ARGV[5]) or 0
local now = tonumber(ARGV[6]) or 0
local allow_takeover = tonumber(ARGV[7]) or 0

local expired = redis.call('ZRANGEBYSCORE', expiries_key, '-inf', now)
for _, expired_run_id in ipairs(expired) do
  redis.call('HDEL', leases_key, expired_run_id)
  redis.call('HDEL', owners_key, expired_run_id)
end
if #expired > 0 then
  redis.call('ZREM', expiries_key, unpack(expired))
end

local current_total = 0
local current_values = redis.call('HVALS', leases_key)
for _, raw_cost in ipairs(current_values) do
  current_total = current_total + (tonumber(raw_cost) or 0)
end

local existing_cost = tonumber(redis.call('HGET', leases_key, run_id) or '0')
local existing_owner = tostring(redis.call('HGET', owners_key, run_id) or '')
if existing_cost > 0 and existing_owner ~= '' and existing_owner ~= owner_token and allow_takeover ~= 1 then
  return {3, current_total, budget, requested_cost, existing_cost}
end

local next_total = current_total + requested_cost
if existing_cost > 0 then
  next_total = current_total - existing_cost + requested_cost
end

if next_total > budget then
  return {0, current_total, budget, requested_cost, existing_cost}
end

redis.call('HSET', leases_key, run_id, tostring(requested_cost))
redis.call('HSET', owners_key, run_id, owner_token)
redis.call('ZADD', expiries_key, now + ttl, run_id)

if existing_cost > 0 then
  if existing_owner ~= '' and existing_owner ~= owner_token then
    return {4, next_total, budget, requested_cost, existing_cost}
  end
  return {2, next_total, budget, requested_cost, existing_cost}
end
return {1, next_total, budget, requested_cost, existing_cost}
"""

_RELEASE_RUN_CAPACITY_LUA = """
local leases_key = KEYS[1]
local owners_key = KEYS[2]
local expiries_key = KEYS[3]
local run_id = tostring(ARGV[1] or '')
local owner_token = tostring(ARGV[2] or '')
local now = tonumber(ARGV[3]) or 0

local expired = redis.call('ZRANGEBYSCORE', expiries_key, '-inf', now)
for _, expired_run_id in ipairs(expired) do
  redis.call('HDEL', leases_key, expired_run_id)
  redis.call('HDEL', owners_key, expired_run_id)
end
if #expired > 0 then
  redis.call('ZREM', expiries_key, unpack(expired))
end

local existing_cost = tonumber(redis.call('HGET', leases_key, run_id) or '0')
local existing_owner = tostring(redis.call('HGET', owners_key, run_id) or '')
local current_total = 0
local current_values = redis.call('HVALS', leases_key)
for _, raw_cost in ipairs(current_values) do
  current_total = current_total + (tonumber(raw_cost) or 0)
end

if existing_cost > 0 and existing_owner ~= '' and existing_owner ~= owner_token then
  return {-1, current_total, existing_cost}
end

if existing_cost > 0 then
  redis.call('HDEL', leases_key, run_id)
  redis.call('HDEL', owners_key, run_id)
  redis.call('ZREM', expiries_key, run_id)
end

current_total = 0
current_values = redis.call('HVALS', leases_key)
for _, raw_cost in ipairs(current_values) do
  current_total = current_total + (tonumber(raw_cost) or 0)
end

if existing_cost > 0 then
  return {1, current_total, existing_cost}
end
return {0, current_total, existing_cost}
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


def _build_capacity_snapshot(
    *,
    budget: int,
    in_use: int,
    requested_cost: Optional[int] = None,
    unavailable: bool = False,
) -> dict[str, Any]:
    remaining = max(0, budget - max(0, in_use))
    snapshot = {
        "enabled": budget > 0,
        "budget": budget,
        "in_use": max(0, in_use),
        "remaining": remaining,
    }
    if requested_cost is not None:
        snapshot["requested_cost"] = max(0, requested_cost)
        snapshot["can_admit"] = max(0, in_use) + max(0, requested_cost) <= budget
    if unavailable:
        snapshot["unavailable"] = True
    return snapshot


def _build_unavailable_capacity_snapshot(
    *,
    mode: Any = None,
    requested_cost: Optional[int] = None,
) -> dict[str, Any]:
    budget = get_run_capacity_budget(mode)
    effective_budget = max(0, budget)
    return _build_capacity_snapshot(
        budget=effective_budget,
        in_use=effective_budget,
        requested_cost=requested_cost,
        unavailable=True,
    )


async def get_run_capacity_snapshot(
    *,
    mode: Any = None,
    requested_cost: Optional[int] = None,
) -> dict[str, Any]:
    budget = get_run_capacity_budget(mode)
    if budget <= 0:
        return _build_capacity_snapshot(budget=0, in_use=0, requested_cost=requested_cost)

    current_epoch_seconds = int(time.time())
    current_total = int(
        await redis.eval_script(
            _PRUNE_EXPIRED_LEASES_LUA,
            keys=[_LEASES_KEY, _OWNERS_KEY, _EXPIRIES_KEY],
            args=[str(current_epoch_seconds)],
        )
        or 0
    )
    return _build_capacity_snapshot(
        budget=budget,
        in_use=current_total,
        requested_cost=requested_cost,
    )


async def try_acquire_run_capacity_lease(
    *,
    agent_run_id: str,
    mode: Any,
    owner_token: str,
    allow_takeover: bool = False,
) -> dict[str, Any]:
    budget = get_run_capacity_budget(mode)
    requested_cost = get_run_capacity_cost(mode)
    if budget <= 0:
        snapshot = _build_capacity_snapshot(
            budget=0,
            in_use=0,
            requested_cost=requested_cost,
        )
        return {
            **snapshot,
            "capacity_kind": get_run_capacity_kind(mode),
            "acquired": True,
            "refreshed": False,
            "taken_over": False,
            "owner_conflict": False,
            "lease_ttl_seconds": 0,
        }

    normalized_run_id = str(agent_run_id or "").strip()
    if not normalized_run_id:
        raise ValueError("agent_run_id is required to acquire run capacity")
    normalized_owner_token = str(owner_token or "").strip()
    if not normalized_owner_token:
        raise ValueError("owner_token is required to acquire run capacity")

    lease_ttl_seconds = get_run_capacity_ttl_seconds()
    current_epoch_seconds = int(time.time())
    raw_result = await redis.eval_script(
        _ACQUIRE_RUN_CAPACITY_LUA,
        keys=[_LEASES_KEY, _OWNERS_KEY, _EXPIRIES_KEY],
        args=[
            str(budget),
            normalized_run_id,
            normalized_owner_token,
            str(requested_cost),
            str(lease_ttl_seconds),
            str(current_epoch_seconds),
            "1" if allow_takeover else "0",
        ],
    )
    status_code, in_use_after, resolved_budget, requested_cost_value, _existing_cost = (
        _coerce_eval_result(raw_result) + [0, 0, 0, 0, 0]
    )[:5]

    snapshot = _build_capacity_snapshot(
        budget=resolved_budget or budget,
        in_use=in_use_after,
        requested_cost=requested_cost_value or requested_cost,
    )
    return {
        **snapshot,
        "capacity_kind": get_run_capacity_kind(mode),
        "acquired": status_code in {1, 2, 4},
        "refreshed": status_code == 2,
        "taken_over": status_code == 4,
        "owner_conflict": status_code == 3,
        "lease_ttl_seconds": lease_ttl_seconds,
    }


async def refresh_run_capacity_lease(
    *,
    agent_run_id: str,
    mode: Any,
    owner_token: str,
) -> dict[str, Any]:
    return await try_acquire_run_capacity_lease(
        agent_run_id=agent_run_id,
        mode=mode,
        owner_token=owner_token,
        allow_takeover=False,
    )


async def release_run_capacity_lease(
    *,
    agent_run_id: str,
    owner_token: str,
) -> dict[str, Any]:
    budget = get_server_concurrency_budget()
    normalized_run_id = str(agent_run_id or "").strip()
    normalized_owner_token = str(owner_token or "").strip()
    if budget <= 0 or not normalized_run_id:
        return {
            **_build_capacity_snapshot(budget=budget, in_use=0),
            "released": False,
            "released_cost": 0,
            "owner_conflict": False,
        }
    if not normalized_owner_token:
        raise ValueError("owner_token is required to release run capacity")

    current_epoch_seconds = int(time.time())
    raw_result = await redis.eval_script(
        _RELEASE_RUN_CAPACITY_LUA,
        keys=[_LEASES_KEY, _OWNERS_KEY, _EXPIRIES_KEY],
        args=[normalized_run_id, normalized_owner_token, str(current_epoch_seconds)],
    )
    released, in_use_after, released_cost = (_coerce_eval_result(raw_result) + [0, 0, 0])[:3]
    snapshot = _build_capacity_snapshot(
        budget=budget,
        in_use=in_use_after,
    )
    return {
        **snapshot,
        "released": released == 1,
        "released_cost": max(0, released_cost if released == 1 else 0),
        "owner_conflict": released == -1,
    }


def build_run_capacity_limit_detail(
    *,
    mode: Any,
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    capacity_kind = get_run_capacity_kind(mode)
    code = "shadow_clone_limit" if capacity_kind == "shadow_clone" else "server_concurrency_limit"
    requested_cost = int(snapshot.get("requested_cost") or get_run_capacity_cost(mode))
    budget = int(snapshot.get("budget") or 0)
    in_use = int(snapshot.get("in_use") or 0)
    remaining = int(snapshot.get("remaining") or max(0, budget - in_use))
    unavailable = bool(snapshot.get("unavailable"))
    return {
        "code": code,
        "message": (
            "Server concurrency guard is temporarily unavailable. Please retry shortly."
            if unavailable
            else "Server concurrency budget is exhausted. Please retry shortly."
        ),
        "capacity_kind": capacity_kind,
        "budget": budget,
        "in_use": in_use,
        "remaining": remaining,
        "requested_cost": requested_cost,
    }


async def get_safe_run_capacity_snapshot(
    *,
    mode: Any = None,
    requested_cost: Optional[int] = None,
) -> dict[str, Any]:
    try:
        return await get_run_capacity_snapshot(mode=mode, requested_cost=requested_cost)
    except Exception as snapshot_error:
        logger.warning(
            "Failed to read server concurrency budget snapshot: %s",
            snapshot_error,
        )
        return _build_unavailable_capacity_snapshot(
            mode=mode,
            requested_cost=requested_cost,
        )
