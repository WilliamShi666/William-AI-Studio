"""Redis-backed task lease helpers for Shadow Clone V2."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone

from services import redis as redis_service

from .keys import task_key
from .validation import require_key_part
from .models import Task, TaskStatus


def _require_non_blank(value: str, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{field_name} is required")
    return normalized


def _parse_lease_timestamp(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = str(value).strip()
    if not normalized:
        return None
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _is_lease_expired(*, lease_expires_at: str | None, now: str) -> bool:
    lease_time = _parse_lease_timestamp(lease_expires_at)
    now_time = _parse_lease_timestamp(now)
    if lease_time is None:
        return True
    if now_time is None:
        raise ValueError("now must be a valid ISO timestamp")
    return lease_time <= now_time


@dataclass(frozen=True)
class ExpiredTaskLeaseSweepResult:
    """One deterministic expired-task lease recovery result."""

    action: str
    task: Task
    previous_status: str
    previous_owner_agent: str | None
    previous_lease_expires_at: str | None


def _validated_task_key(*, run_id: str, task_id: str) -> str:
    return task_key(
        run_id=require_key_part(run_id, "run_id"),
        task_id=require_key_part(task_id, "task_id"),
    )


def _normalize_task_dependency_fields(payload: dict) -> dict:
    normalized = dict(payload)
    for field_name in ("blocked_by", "blocks"):
        value = normalized.get(field_name)
        if value == {}:
            normalized[field_name] = []
    return normalized


def _task_from_json(raw: str | bytes) -> Task:
    payload = json.loads(raw)
    if isinstance(payload, dict):
        return Task.model_validate(_normalize_task_dependency_fields(payload))
    return Task.model_validate(payload)


_CLAIM_TASK_LUA = """
local task_key = KEYS[1]
local owner_agent = ARGV[1]
local lease_token = ARGV[2]
local lease_expires_at = ARGV[3]
local updated_at = ARGV[4]

local raw_task = redis.call('GET', task_key)
if not raw_task then
  return 0
end

local task = cjson.decode(raw_task)
local status = tostring(task['status'] or '')
if status ~= 'pending' and status ~= 'blocked' then
  return -1
end
local blocked_by = task['blocked_by'] or {}
local blocker_count = tonumber(ARGV[5] or 0)
if type(blocked_by) == 'table' and #blocked_by > 0 then
  if blocker_count ~= #blocked_by then
    return -1
  end
  for i = 1, #blocked_by do
    local blocker_id = tostring(blocked_by[i])
    local expected_blocker_id = tostring(ARGV[5 + i])
    if blocker_id ~= expected_blocker_id then
      return -1
    end
    local blocker_raw = redis.call('GET', KEYS[1 + i])
    if not blocker_raw then
      return -1
    end
    local blocker_task = cjson.decode(blocker_raw)
    if tostring(blocker_task['status'] or '') ~= 'completed' then
      return -1
    end
  end
end

task['status'] = 'in_progress'
task['owner_agent'] = owner_agent
task['lease_token'] = lease_token
task['lease_expires_at'] = lease_expires_at
task['updated_at'] = updated_at
task['attempt'] = tonumber(task['attempt'] or 0) + 1
task['plan_revision'] = tonumber(task['plan_revision'] or 1) + 1
redis.call('SET', task_key, cjson.encode(task), 'KEEPTTL')
return 1
"""

_COMPLETE_TASK_LUA = """
local task_key = KEYS[1]
local owner_agent = ARGV[1]
local lease_token = ARGV[2]
local result_ref = ARGV[3]
local expected_lease_expires_at = ARGV[4]
local updated_at = ARGV[5]

local raw_task = redis.call('GET', task_key)
if not raw_task then
  return 0
end

local task = cjson.decode(raw_task)
if tostring(task['status'] or '') ~= 'in_progress' then
  return -1
end
if tostring(task['owner_agent'] or '') ~= owner_agent then
  return -2
end
if tostring(task['lease_token'] or '') ~= lease_token then
  return -2
end
if tostring(task['lease_expires_at'] or '') ~= expected_lease_expires_at then
  return -3
end

task['status'] = 'completed'
task['result_ref'] = result_ref
task['updated_at'] = updated_at
task['lease_expires_at'] = cjson.null
task['lease_token'] = cjson.null
task['plan_revision'] = tonumber(task['plan_revision'] or 1) + 1
redis.call('SET', task_key, cjson.encode(task), 'KEEPTTL')
return 1
"""

_FAIL_TASK_LUA = """
local task_key = KEYS[1]
local owner_agent = ARGV[1]
local lease_token = ARGV[2]
local error_type = ARGV[3]
local error_message = ARGV[4]
local expected_lease_expires_at = ARGV[5]
local updated_at = ARGV[6]

local raw_task = redis.call('GET', task_key)
if not raw_task then
  return 0
end

local task = cjson.decode(raw_task)
if tostring(task['status'] or '') ~= 'in_progress' then
  return -1
end
if tostring(task['owner_agent'] or '') ~= owner_agent then
  return -2
end
if tostring(task['lease_token'] or '') ~= lease_token then
  return -2
end
if tostring(task['lease_expires_at'] or '') ~= expected_lease_expires_at then
  return -3
end

task['status'] = 'failed'
task['error'] = {
  error_type = error_type,
  message = error_message
}
task['updated_at'] = updated_at
task['lease_expires_at'] = cjson.null
task['lease_token'] = cjson.null
task['plan_revision'] = tonumber(task['plan_revision'] or 1) + 1
redis.call('SET', task_key, cjson.encode(task), 'KEEPTTL')
return 1
"""

_RENEW_TASK_LEASE_LUA = """
local task_key = KEYS[1]
local owner_agent = ARGV[1]
local lease_token = ARGV[2]
local expected_lease_expires_at = ARGV[3]
local lease_expires_at = ARGV[4]
local updated_at = ARGV[5]

local raw_task = redis.call('GET', task_key)
if not raw_task then
  return 0
end

local task = cjson.decode(raw_task)
if tostring(task['status'] or '') ~= 'in_progress' then
  return -1
end
if tostring(task['owner_agent'] or '') ~= owner_agent then
  return -2
end
if tostring(task['lease_token'] or '') ~= lease_token then
  return -2
end
if tostring(task['lease_expires_at'] or '') ~= expected_lease_expires_at then
  return -3
end

task['lease_expires_at'] = lease_expires_at
task['updated_at'] = updated_at
task['plan_revision'] = tonumber(task['plan_revision'] or 1) + 1
redis.call('SET', task_key, cjson.encode(task), 'KEEPTTL')
return 1
"""

_SWEEP_EXPIRED_TASK_LEASE_LUA = """
local task_key = KEYS[1]
local expected_owner = ARGV[1]
local expected_token = ARGV[2]
local expected_expiry = ARGV[3]
local updated_at = ARGV[4]
local ttl_seconds = tonumber(ARGV[5])
local blocker_count = tonumber(ARGV[6] or 0)

local raw_task = redis.call('GET', task_key)
if not raw_task then
  return {'none', ''}
end

local task = cjson.decode(raw_task)
if tostring(task['status'] or '') ~= 'in_progress' then
  return {'none', raw_task}
end
if tostring(task['owner_agent'] or '') ~= expected_owner then
  return {'none', raw_task}
end
if tostring(task['lease_token'] or '') ~= expected_token then
  return {'none', raw_task}
end
if tostring(task['lease_expires_at'] or '') ~= expected_expiry then
  return {'none', raw_task}
end

local blockers_resolved = true
local blocked_by = task['blocked_by'] or {}
if type(blocked_by) == 'table' and #blocked_by > 0 then
  if blocker_count ~= #blocked_by then
    blockers_resolved = false
  else
    for i = 1, #blocked_by do
      local blocker_raw = redis.call('GET', KEYS[1 + i])
      if not blocker_raw then
        blockers_resolved = false
        break
      end
      local blocker_task = cjson.decode(blocker_raw)
      if tostring(blocker_task['status'] or '') ~= 'completed' then
        blockers_resolved = false
        break
      end
    end
  end
end

local attempt = tonumber(task['attempt'] or 0)
local max_attempts = tonumber(task['max_attempts'] or 1)
task['owner_agent'] = cjson.null
task['lease_token'] = cjson.null
task['lease_expires_at'] = cjson.null
task['updated_at'] = updated_at
task['plan_revision'] = tonumber(task['plan_revision'] or 1) + 1

if attempt >= max_attempts then
  task['status'] = 'failed'
  task['error'] = {
    error_type = 'LeaseExpired',
    message = 'Task lease expired after maximum attempts.'
  }
  redis.call('SET', task_key, cjson.encode(task), 'EX', ttl_seconds)
  return {'failed', cjson.encode(task)}
end

if blockers_resolved then
  task['status'] = 'pending'
else
  task['status'] = 'blocked'
end
task['error'] = {
  error_type = 'LeaseExpired',
  message = 'Task lease expired before completion.'
}
redis.call('SET', task_key, cjson.encode(task), 'EX', ttl_seconds)
return {'retried', cjson.encode(task)}
"""

_UPDATE_TASK_LUA = """
local task_key = KEYS[1]
local expected_revision = tonumber(ARGV[1])
local updated_task_json = ARGV[2]
local ttl_seconds = tonumber(ARGV[3])

local raw_task = redis.call('GET', task_key)
if not raw_task then
  return 0
end

local current_task = cjson.decode(raw_task)
local current_revision = tonumber(current_task['plan_revision'] or 1)
if current_revision ~= expected_revision then
  return -1
end

redis.call('SET', task_key, updated_task_json, 'EX', ttl_seconds)
return 1
"""

_UPDATE_TASKS_LUA = """
local ttl_seconds = tonumber(ARGV[1])

for i = 1, #KEYS do
  local raw_task = redis.call('GET', KEYS[i])
  if not raw_task then
    return 0
  end

  local current_task = cjson.decode(raw_task)
  local expected_revision = tonumber(ARGV[1 + ((i - 1) * 2) + 1])
  local current_revision = tonumber(current_task['plan_revision'] or 1)
  if current_revision ~= expected_revision then
    return -1
  end
end

for i = 1, #KEYS do
  local updated_task_json = ARGV[1 + ((i - 1) * 2) + 2]
  redis.call('SET', KEYS[i], updated_task_json, 'EX', ttl_seconds)
end

return 1
"""


async def store_task(
    task: Task,
    *,
    ttl_seconds: int,
    timeout: float | None = None,
) -> None:
    """Persist a task record under its V2 Redis key."""
    await redis_service.set(
        _validated_task_key(run_id=task.run_id, task_id=task.id),
        task.model_dump_json(),
        ex=ttl_seconds,
        timeout=timeout,
    )


async def create_task(
    task: Task,
    *,
    ttl_seconds: int,
    timeout: float | None = None,
) -> bool:
    """Persist a new task only if no task with the same id exists."""
    created = await redis_service.set(
        _validated_task_key(run_id=task.run_id, task_id=task.id),
        task.model_dump_json(),
        ex=ttl_seconds,
        nx=True,
        timeout=timeout,
    )
    return bool(created)


async def read_task(
    *,
    run_id: str,
    task_id: str,
    timeout: float | None = None,
) -> Task | None:
    """Read one task record if it exists."""
    raw = await redis_service.get(
        _validated_task_key(run_id=run_id, task_id=task_id),
        timeout=timeout,
    )
    if not raw:
        return None
    return _task_from_json(raw)


async def claim_task(
    *,
    run_id: str,
    task_id: str,
    agent_name: str,
    lease_token: str,
    lease_expires_at: str,
    updated_at: str,
    timeout: float | None = None,
) -> bool:
    """Atomically claim a pending task by writing an expiring lease."""
    owner = _require_non_blank(agent_name, "agent_name")
    token = _require_non_blank(lease_token, "lease_token")
    task = await read_task(run_id=run_id, task_id=task_id, timeout=timeout)
    if task is None:
        return False
    blocker_ids = list(task.blocked_by)
    result = await redis_service.eval_script(
        _CLAIM_TASK_LUA,
        keys=[
            _validated_task_key(run_id=run_id, task_id=task_id),
            *[
                _validated_task_key(run_id=run_id, task_id=blocker_id)
                for blocker_id in blocker_ids
            ],
        ],
        args=[
            owner,
            token,
            lease_expires_at,
            updated_at,
            len(blocker_ids),
            *blocker_ids,
        ],
        timeout=timeout,
    )
    return int(result or 0) == 1


async def complete_task(
    *,
    run_id: str,
    task_id: str,
    owner_agent: str,
    lease_token: str,
    result_ref: str,
    updated_at: str,
    now: str | None = None,
    timeout: float | None = None,
) -> bool:
    """Atomically complete a task only if the owner still holds its lease."""
    owner = _require_non_blank(owner_agent, "owner_agent")
    token = _require_non_blank(lease_token, "lease_token")
    existing_task = await read_task(run_id=run_id, task_id=task_id, timeout=timeout)
    if existing_task is None or _is_lease_expired(
        lease_expires_at=existing_task.lease_expires_at,
        now=now or updated_at,
    ):
        return False
    result = await redis_service.eval_script(
        _COMPLETE_TASK_LUA,
        keys=[_validated_task_key(run_id=run_id, task_id=task_id)],
        args=[
            owner,
            token,
            result_ref,
            existing_task.lease_expires_at or "",
            updated_at,
            now or updated_at,
        ],
        timeout=timeout,
    )
    return int(result or 0) == 1


async def fail_task(
    *,
    run_id: str,
    task_id: str,
    owner_agent: str,
    lease_token: str,
    error_type: str,
    error_message: str,
    updated_at: str,
    now: str | None = None,
    timeout: float | None = None,
) -> bool:
    """Atomically mark a leased task failed without stealing another lease."""
    owner = _require_non_blank(owner_agent, "owner_agent")
    token = _require_non_blank(lease_token, "lease_token")
    existing_task = await read_task(run_id=run_id, task_id=task_id, timeout=timeout)
    if existing_task is None or _is_lease_expired(
        lease_expires_at=existing_task.lease_expires_at,
        now=now or updated_at,
    ):
        return False
    result = await redis_service.eval_script(
        _FAIL_TASK_LUA,
        keys=[_validated_task_key(run_id=run_id, task_id=task_id)],
        args=[
            owner,
            token,
            error_type,
            error_message,
            existing_task.lease_expires_at or "",
            updated_at,
            now or updated_at,
        ],
        timeout=timeout,
    )
    return int(result or 0) == 1


async def renew_task_lease(
    *,
    run_id: str,
    task_id: str,
    owner_agent: str,
    lease_token: str,
    lease_expires_at: str,
    updated_at: str,
    now: str | None = None,
    timeout: float | None = None,
) -> bool:
    """Atomically extend an active task lease for its current owner/token."""
    owner = _require_non_blank(owner_agent, "owner_agent")
    token = _require_non_blank(lease_token, "lease_token")
    effective_now = now or updated_at
    existing_task = await read_task(run_id=run_id, task_id=task_id, timeout=timeout)
    if existing_task is None or _is_lease_expired(
        lease_expires_at=existing_task.lease_expires_at,
        now=effective_now,
    ):
        return False
    if _is_lease_expired(lease_expires_at=lease_expires_at, now=effective_now):
        return False
    result = await redis_service.eval_script(
        _RENEW_TASK_LEASE_LUA,
        keys=[_validated_task_key(run_id=run_id, task_id=task_id)],
        args=[
            owner,
            token,
            existing_task.lease_expires_at or "",
            lease_expires_at,
            updated_at,
            effective_now,
        ],
        timeout=timeout,
    )
    return int(result or 0) == 1


async def sweep_expired_task_leases(
    *,
    run_id: str,
    now: str,
    ttl_seconds: int,
    timeout: float | None = None,
) -> list[ExpiredTaskLeaseSweepResult]:
    """Recover or fail expired in-progress task leases for one run."""
    from .keys import V2_REDIS_PREFIX

    normalized_run_id = require_key_part(run_id, "run_id")
    keys = await redis_service.scan_keys(
        f"{V2_REDIS_PREFIX}:run:{normalized_run_id}:task:*",
        timeout=timeout,
    )
    results: list[ExpiredTaskLeaseSweepResult] = []
    for key in sorted(keys or []):
        task_id = str(key).rsplit(":", 1)[-1]
        task = await read_task(
            run_id=normalized_run_id,
            task_id=task_id,
            timeout=timeout,
        )
        if task is None or task.status != TaskStatus.IN_PROGRESS:
            continue
        if not _is_lease_expired(lease_expires_at=task.lease_expires_at, now=now):
            continue
        previous_status = str(task.status.value)
        previous_owner = task.owner_agent
        previous_expiry = task.lease_expires_at
        blocker_ids = list(task.blocked_by)
        raw_result = await redis_service.eval_script(
            _SWEEP_EXPIRED_TASK_LEASE_LUA,
            keys=[
                _validated_task_key(run_id=normalized_run_id, task_id=task.id),
                *[
                    _validated_task_key(run_id=normalized_run_id, task_id=blocker_id)
                    for blocker_id in blocker_ids
                ],
            ],
            args=[
                previous_owner or "",
                task.lease_token or "",
                previous_expiry or "",
                now,
                ttl_seconds,
                len(blocker_ids),
            ],
            timeout=timeout,
        )
        if not raw_result:
            continue
        action = str(raw_result[0])
        if action not in {"retried", "failed"}:
            continue
        updated_raw = raw_result[1] if len(raw_result) > 1 else None
        if not updated_raw:
            continue
        updated_task = _task_from_json(updated_raw)
        results.append(
            ExpiredTaskLeaseSweepResult(
                action=action,
                task=updated_task,
                previous_status=previous_status,
                previous_owner_agent=previous_owner,
                previous_lease_expires_at=previous_expiry,
            )
        )
    return results


async def update_task(
    task: Task,
    *,
    expected_plan_revision: int,
    ttl_seconds: int,
    timeout: float | None = None,
) -> bool:
    """Atomically replace a task if its plan revision still matches."""
    result = await redis_service.eval_script(
        _UPDATE_TASK_LUA,
        keys=[_validated_task_key(run_id=task.run_id, task_id=task.id)],
        args=[expected_plan_revision, task.model_dump_json(), ttl_seconds],
        timeout=timeout,
    )
    return int(result or 0) == 1


async def update_tasks(
    *,
    tasks: list[Task],
    expected_plan_revisions: dict[str, int],
    ttl_seconds: int,
    timeout: float | None = None,
) -> bool:
    """Atomically replace multiple tasks if every revision still matches."""
    if not tasks:
        raise ValueError("tasks are required")
    run_ids = {require_key_part(task.run_id, "run_id") for task in tasks}
    if len(run_ids) != 1:
        raise ValueError("all tasks must belong to one run")
    ordered_tasks = sorted(tasks, key=lambda task: task.id)
    args: list[object] = [ttl_seconds]
    for task in ordered_tasks:
        expected_revision = expected_plan_revisions.get(task.id)
        if expected_revision is None:
            raise ValueError(f"expected revision missing for task: {task.id}")
        args.extend([expected_revision, task.model_dump_json()])
    result = await redis_service.eval_script(
        _UPDATE_TASKS_LUA,
        keys=[
            _validated_task_key(run_id=task.run_id, task_id=task.id)
            for task in ordered_tasks
        ],
        args=args,
        timeout=timeout,
    )
    return int(result or 0) == 1


async def list_tasks(
    *,
    run_id: str,
    timeout: float | None = None,
) -> list[Task]:
    """List all task records for one run.

    This is the task-list primitive used by agent-facing TaskList. It scans the
    V2 run task namespace instead of relying on runner-owned in-memory plan
    state, which lets tasks become living documents created during execution.
    """
    from .keys import V2_REDIS_PREFIX

    normalized_run_id = require_key_part(run_id, "run_id")

    keys = await redis_service.scan_keys(
        f"{V2_REDIS_PREFIX}:run:{normalized_run_id}:task:*",
        timeout=timeout,
    )
    tasks: list[Task] = []
    for key in sorted(keys or []):
        task_id = str(key).rsplit(":", 1)[-1]
        task = await read_task(
            run_id=normalized_run_id, task_id=task_id, timeout=timeout
        )
        if task is not None:
            tasks.append(task)
    return tasks
