"""Redis-backed state machine for Shadow Clone runs."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from services import redis as redis_service
from utils.logger import logger


STATE_KEY_TEMPLATE = "shadow_clone:{run_id}:state"
STATE_TTL_SECONDS = int(os.getenv("SHADOW_CLONE_STATE_TTL", "7200"))
_UNSET = object()

STATE_TRANSITIONS: Dict[str, list[str]] = {
    "pending": ["confirming", "completed", "failed"],
    "confirming": ["running", "denied", "cancelled", "timeout", "completed", "failed"],
    "running": ["aggregating", "completed", "failed", "cancelled"],
    "aggregating": ["completed", "failed", "cancelled"],
}

TERMINAL_SHADOW_CLONE_STATUSES = frozenset(
    {"completed", "failed", "cancelled", "timeout", "denied"},
)


def _normalize_optional_metadata_value(value: Any) -> Optional[str]:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _preserve_current_terminal_state(
    current_state: Optional[Dict[str, Any]],
    incoming_state: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    if not isinstance(current_state, dict):
        return None

    current_status = str(current_state.get("status") or "").strip().lower()
    incoming_status = str(incoming_state.get("status") or "").strip().lower()
    current_epoch = int(current_state.get("execution_epoch") or 0)
    incoming_epoch = int(incoming_state.get("execution_epoch") or 0)

    if current_status not in TERMINAL_SHADOW_CLONE_STATUSES:
        return None
    if incoming_status not in TERMINAL_SHADOW_CLONE_STATUSES:
        return current_state
    if incoming_epoch < current_epoch:
        return current_state
    if incoming_epoch == current_epoch and incoming_status != current_status:
        return current_state
    return None


_TRANSITION_LUA = """
local key = KEYS[1]
local expected = ARGV[1]
local target = ARGV[2]
local ttl = tonumber(ARGV[3])
local now_iso = ARGV[4]

local raw = redis.call('GET', key)
if not raw then
  return 0
end

local state = cjson.decode(raw)
if state["status"] ~= expected then
  return 0
end

state["status"] = target
state["updated_at"] = now_iso

redis.call('SET', key, cjson.encode(state), 'EX', ttl)
return 1
"""

_UPDATE_SUBAGENT_LUA = """
local key = KEYS[1]
local subtask_id = ARGV[1]
local status = ARGV[2]
local role = ARGV[3]
local result_summary = ARGV[4]
local ttl = tonumber(ARGV[5])
local now_iso = ARGV[6]
local expected_epoch = ARGV[7]
local reset = ARGV[8]

local raw = redis.call('GET', key)
if not raw then
  return 0
end

local state = cjson.decode(raw)
local current_epoch = tonumber(state["execution_epoch"] or 0)
if expected_epoch and expected_epoch ~= "" then
  local parsed_expected_epoch = tonumber(expected_epoch)
  if parsed_expected_epoch ~= current_epoch then
    return -1
  end
end
if state["subagents"] == cjson.null or state["subagents"] == nil then
  state["subagents"] = {}
end

local sub = state["subagents"][subtask_id]
if sub == cjson.null or sub == nil then
  sub = {}
  sub["created_at"] = now_iso
end

sub["status"] = status
if reset == "1" then
  sub["result_summary"] = cjson.null
  sub["started_at"] = cjson.null
  sub["finished_at"] = cjson.null
  sub["owner_token"] = cjson.null
end
if role and role ~= "" then
  sub["role"] = role
end
if result_summary and result_summary ~= "" then
  sub["result_summary"] = result_summary
end
if status == "running" and (sub["started_at"] == nil or sub["started_at"] == cjson.null) then
  sub["started_at"] = now_iso
end
if status == "completed" or status == "failed" then
  sub["finished_at"] = now_iso
  sub["owner_token"] = cjson.null
elseif status == "pending" then
  sub["owner_token"] = cjson.null
end

state["subagents"][subtask_id] = sub

local total = 0
local completed = 0
local failed = 0
local running = 0

for _, item in pairs(state["subagents"]) do
  total = total + 1
  local item_status = item["status"]
  if item_status == "completed" then
    completed = completed + 1
  elseif item_status == "failed" then
    failed = failed + 1
  elseif item_status == "running" then
    running = running + 1
  end
end

if state["total"] == nil or state["total"] < total then
  state["total"] = total
end
state["completed"] = completed
state["failed"] = failed
state["running"] = running
state["updated_at"] = now_iso

redis.call('SET', key, cjson.encode(state), 'EX', ttl)
return 1
"""

_CLAIM_SUBAGENT_LUA = """
local key = KEYS[1]
local subtask_id = ARGV[1]
local role = ARGV[2]
local ttl = tonumber(ARGV[3])
local now_iso = ARGV[4]
local expected_epoch = ARGV[5]
local owner_token = ARGV[6]

local raw = redis.call('GET', key)
if not raw then
  return 0
end

local state = cjson.decode(raw)
local current_epoch = tonumber(state["execution_epoch"] or 0)
if expected_epoch and expected_epoch ~= "" then
  local parsed_expected_epoch = tonumber(expected_epoch)
  if parsed_expected_epoch ~= current_epoch then
    return -1
  end
end
if state["subagents"] == cjson.null or state["subagents"] == nil then
  state["subagents"] = {}
end

local sub = state["subagents"][subtask_id]
if sub == cjson.null or sub == nil then
  sub = {}
  sub["created_at"] = now_iso
end

local current_status = tostring(sub["status"] or "")
local current_owner = tostring(sub["owner_token"] or "")
if current_status == "running" then
  if current_owner == owner_token then
    return 1
  end
  return -2
end
if current_status ~= "" and current_status ~= "pending" then
  return -2
end

sub["status"] = "running"
sub["owner_token"] = owner_token
sub["finished_at"] = cjson.null
if role and role ~= "" then
  sub["role"] = role
end
if sub["started_at"] == nil or sub["started_at"] == cjson.null then
  sub["started_at"] = now_iso
end

state["subagents"][subtask_id] = sub

local total = 0
local completed = 0
local failed = 0
local running = 0

for _, item in pairs(state["subagents"]) do
  total = total + 1
  local item_status = item["status"]
  if item_status == "completed" then
    completed = completed + 1
  elseif item_status == "failed" then
    failed = failed + 1
  elseif item_status == "running" then
    running = running + 1
  end
end

if state["total"] == nil or state["total"] < total then
  state["total"] = total
end
state["completed"] = completed
state["failed"] = failed
state["running"] = running
state["updated_at"] = now_iso

redis.call('SET', key, cjson.encode(state), 'EX', ttl)
return 1
"""

_FINALIZE_SUBAGENT_LUA = """
local key = KEYS[1]
local subtask_id = ARGV[1]
local status = ARGV[2]
local role = ARGV[3]
local result_summary = ARGV[4]
local ttl = tonumber(ARGV[5])
local now_iso = ARGV[6]
local expected_epoch = ARGV[7]
local owner_token = ARGV[8]

local raw = redis.call('GET', key)
if not raw then
  return 0
end

local state = cjson.decode(raw)
local current_epoch = tonumber(state["execution_epoch"] or 0)
if expected_epoch and expected_epoch ~= "" then
  local parsed_expected_epoch = tonumber(expected_epoch)
  if parsed_expected_epoch ~= current_epoch then
    return -1
  end
end
if state["subagents"] == cjson.null or state["subagents"] == nil then
  state["subagents"] = {}
end

local sub = state["subagents"][subtask_id]
if sub == cjson.null or sub == nil then
  return -2
end

local current_owner = tostring(sub["owner_token"] or "")
if current_owner == "" or current_owner ~= owner_token then
  return -2
end

sub["status"] = status
sub["owner_token"] = cjson.null
if role and role ~= "" then
  sub["role"] = role
end
if result_summary and result_summary ~= "" then
  sub["result_summary"] = result_summary
end
if status == "completed" or status == "failed" then
  sub["finished_at"] = now_iso
end

state["subagents"][subtask_id] = sub

local total = 0
local completed = 0
local failed = 0
local running = 0

for _, item in pairs(state["subagents"]) do
  total = total + 1
  local item_status = item["status"]
  if item_status == "completed" then
    completed = completed + 1
  elseif item_status == "failed" then
    failed = failed + 1
  elseif item_status == "running" then
    running = running + 1
  end
end

if state["total"] == nil or state["total"] < total then
  state["total"] = total
end
state["completed"] = completed
state["failed"] = failed
state["running"] = running
state["updated_at"] = now_iso

redis.call('SET', key, cjson.encode(state), 'EX', ttl)
return 1
"""

_STAGE_PROPOSAL_LUA = """
-- shadow_clone_stage_proposal_state
local key = KEYS[1]
local total = tonumber(ARGV[1]) or 0
local subtasks_json = ARGV[2]
local dependencies_json = ARGV[3]
local ttl = tonumber(ARGV[4])
local now_iso = ARGV[5]

local raw = redis.call('GET', key)
if not raw then
  return ''
end

local state = cjson.decode(raw)
local proposal = state["proposal"]
if proposal == cjson.null or proposal == nil then
  proposal = {}
end

proposal["subtasks"] = cjson.decode(subtasks_json or "[]")
proposal["dependencies"] = cjson.decode(dependencies_json or "[]")

state["proposal"] = proposal
state["total"] = total
state["updated_at"] = now_iso

local encoded = cjson.encode(state)
redis.call('SET', key, encoded, 'EX', ttl)
return encoded
"""

_UPDATE_ENVIRONMENT_LUA = """
-- shadow_clone_update_environment
local key = KEYS[1]
local status_mode = ARGV[1]
local status_value = ARGV[2]
local ready_mode = ARGV[3]
local ready_value = ARGV[4]
local last_error_mode = ARGV[5]
local last_error_value = ARGV[6]
local prepared_at_mode = ARGV[7]
local prepared_at_value = ARGV[8]
local manifest_mode = ARGV[9]
local manifest_json = ARGV[10]
local ttl = tonumber(ARGV[11])
local now_iso = ARGV[12]

local raw = redis.call('GET', key)
if not raw then
  return ''
end

local state = cjson.decode(raw)
local environment = state["environment"]
if environment == cjson.null or environment == nil then
  environment = {}
end

local merged_manifest = environment["manifest"]
if merged_manifest == cjson.null or merged_manifest == nil then
  merged_manifest = {}
end

if manifest_mode == "SET" then
  local next_manifest = cjson.decode(manifest_json or "{}")
  for manifest_key, manifest_value in pairs(next_manifest) do
    merged_manifest[manifest_key] = manifest_value
  end
end

if status_mode == "SET" then
  environment["status"] = status_value
end
if ready_mode == "SET" then
  environment["ready"] = (ready_value == "1")
end
if prepared_at_mode == "SET" then
  if prepared_at_value == "" then
    environment["prepared_at"] = cjson.null
  else
    environment["prepared_at"] = prepared_at_value
  end
end
if last_error_mode == "SET" then
  if last_error_value == "" then
    environment["last_error"] = cjson.null
  else
    environment["last_error"] = last_error_value
  end
end
environment["manifest"] = merged_manifest

state["environment"] = environment
state["updated_at"] = now_iso

local encoded = cjson.encode(state)
redis.call('SET', key, encoded, 'EX', ttl)
return encoded
"""

_UPDATE_LIVE_ACTIVITY_LUA = """
-- shadow_clone_update_live_activity
local key = KEYS[1]
local scope = ARGV[1]
local phase = ARGV[2]
local reason = ARGV[3]
local subtask_id = ARGV[4]
local epoch_mode = ARGV[5]
local epoch_value = ARGV[6]
local ttl = tonumber(ARGV[7])
local now_iso = ARGV[8]

local raw = redis.call('GET', key)
if not raw then
  return ''
end

local state = cjson.decode(raw)
local resolved_epoch = tonumber(state["execution_epoch"] or 0)
if epoch_mode == "SET" then
  resolved_epoch = tonumber(epoch_value or 0) or 0
end

local live_activity = {
  ["scope"] = scope,
  ["phase"] = phase,
  ["reason"] = cjson.null,
  ["subtask_id"] = cjson.null,
  ["epoch"] = resolved_epoch,
  ["updated_at"] = now_iso,
}
if reason ~= "" then
  live_activity["reason"] = reason
end
if subtask_id ~= "" then
  live_activity["subtask_id"] = subtask_id
end

state["live_activity"] = live_activity
state["updated_at"] = now_iso

local encoded = cjson.encode(state)
redis.call('SET', key, encoded, 'EX', ttl)
return encoded
"""

_WRITE_STATE_LUA = """
local key = KEYS[1]
local incoming_raw = ARGV[1]
local ttl = tonumber(ARGV[2])
local now_iso = ARGV[3]

local incoming = cjson.decode(incoming_raw)
incoming["updated_at"] = now_iso

local current_raw = redis.call('GET', key)
if current_raw then
  local current = cjson.decode(current_raw)
  local current_status = tostring(current["status"] or "")
  local incoming_status = tostring(incoming["status"] or "")
  local current_epoch = tonumber(current["execution_epoch"] or 0) or 0
  local incoming_epoch = tonumber(incoming["execution_epoch"] or 0) or 0

  local function is_terminal(status)
    return status == "completed"
      or status == "failed"
      or status == "cancelled"
      or status == "timeout"
      or status == "denied"
  end

  if is_terminal(current_status) then
    if not is_terminal(incoming_status) then
      return current_raw
    end
    if incoming_epoch < current_epoch then
      return current_raw
    end
    if incoming_epoch == current_epoch and incoming_status ~= current_status then
      return current_raw
    end
  end
end

local encoded = cjson.encode(incoming)
redis.call('SET', key, encoded, 'EX', ttl)
return encoded
"""


def _state_key(run_id: str) -> str:
    return STATE_KEY_TEMPLATE.format(run_id=run_id)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _empty_subagent_recovery() -> Dict[str, Any]:
    return {
        "mode": None,
        "phase": None,
        "reason": None,
        "wake_attempts": 0,
        "replacement_attempts": 0,
        "replacement_context_id": None,
        "handoff_summary": None,
        "last_command_id": None,
        "updated_at": None,
    }


def _coerce_subagent_recovery(payload: Any) -> Dict[str, Any]:
    recovery = payload if isinstance(payload, dict) else {}
    normalized = _empty_subagent_recovery()
    normalized.update(
        {
            key: recovery.get(key)
            for key in normalized.keys()
            if key in recovery
        }
    )
    normalized["wake_attempts"] = max(0, int(normalized.get("wake_attempts") or 0))
    normalized["replacement_attempts"] = max(
        0,
        int(normalized.get("replacement_attempts") or 0),
    )
    return normalized


def _normalize_mode(mode: Any) -> str:
    value = str(mode or "").strip().lower()
    return value or "auto"


def _is_allowed_transition(from_status: str, to_status: str) -> bool:
    return to_status in STATE_TRANSITIONS.get(from_status, [])


async def init_state(
    run_id: str,
    mode: str,
    total: int = 0,
    *,
    subtasks: Optional[list[Dict[str, Any]]] = None,
    dependencies: Optional[list[Dict[str, Any]]] = None,
) -> None:
    """Initialize the run state in Redis."""
    normalized_subtasks = []
    for item in subtasks or []:
        if not isinstance(item, dict):
            continue
        normalized_subtasks.append(
            {
                "id": str(item.get("id") or "").strip(),
                "role": str(item.get("role") or "").strip(),
                "task_description": str(item.get("task_description") or "").strip(),
            }
        )

    normalized_dependencies = []
    for item in dependencies or []:
        if not isinstance(item, dict):
            continue
        normalized_dependencies.append(
            {
                "from_id": str(item.get("from_id") or "").strip(),
                "to_id": str(item.get("to_id") or "").strip(),
            }
        )

    state = {
        "status": "pending",
        "mode": _normalize_mode(mode),
        "total": max(0, int(total or 0)),
        "completed": 0,
        "failed": 0,
        "running": 0,
        "completion_mode": None,
        "terminal_reason": None,
        "execution_epoch": 0,
        "last_completed_layer": -1,
        "recovery": {
            "pending": False,
            "kind": None,
            "error_code": None,
            "message": None,
            "source": None,
            "scope": None,
            "failed_subtasks": [],
            "retryable_subtasks": [],
            "decision": None,
            "decision_source": None,
            "decision_at": None,
            "requested_at": None,
        },
        "environment": {
            "status": "pending",
            "ready": False,
            "last_error": None,
            "prepared_at": None,
            "manifest": {},
        },
        "live_activity": {
            "scope": "shadow_clone_main",
            "phase": "planning",
            "reason": "planning_started",
            "subtask_id": None,
            "epoch": 0,
            "updated_at": _now_iso(),
        },
        "proposal": {
            "subtasks": normalized_subtasks,
            "dependencies": normalized_dependencies,
        },
        "updated_at": _now_iso(),
        "subagents": {},
    }
    await redis_service.set(
        _state_key(run_id),
        json.dumps(state, ensure_ascii=False),
        ex=STATE_TTL_SECONDS,
    )


async def transition(run_id: str, from_status: str, to_status: str) -> bool:
    """Atomically transition state via CAS semantics."""
    if not _is_allowed_transition(from_status, to_status):
        logger.warning(
            "Rejected illegal shadow clone transition: %s -> %s (run_id=%s)",
            from_status,
            to_status,
            run_id,
        )
        return False

    result = await redis_service.eval_script(
        _TRANSITION_LUA,
        keys=[_state_key(run_id)],
        args=[from_status, to_status, str(STATE_TTL_SECONDS), _now_iso()],
    )
    return bool(int(result or 0))


async def update_subagent(
    run_id: str,
    subtask_id: str,
    status: str,
    result_summary: str = "",
    role: str = "",
    *,
    timeout: float | None = None,
) -> None:
    """Update one subagent entry and roll up counters atomically."""
    kwargs = {}
    if timeout is not None:
        kwargs["timeout"] = timeout
    result = await redis_service.eval_script(
        _UPDATE_SUBAGENT_LUA,
        keys=[_state_key(run_id)],
        args=[
            str(subtask_id),
            str(status),
            str(role or ""),
            str(result_summary or ""),
            str(STATE_TTL_SECONDS),
            _now_iso(),
        ],
        **kwargs,
    )
    return bool(int(result or 0))


async def update_subagent_for_epoch(
    run_id: str,
    subtask_id: str,
    status: str,
    *,
    expected_epoch: int,
    result_summary: str = "",
    role: str = "",
    reset: bool = False,
    timeout: float | None = None,
) -> bool:
    kwargs = {}
    if timeout is not None:
        kwargs["timeout"] = timeout
    result = await redis_service.eval_script(
        _UPDATE_SUBAGENT_LUA,
        keys=[_state_key(run_id)],
        args=[
            str(subtask_id),
            str(status),
            str(role or ""),
            str(result_summary or ""),
            str(STATE_TTL_SECONDS),
            _now_iso(),
            str(int(expected_epoch)),
            "1" if reset else "0",
        ],
        **kwargs,
    )
    return int(result or 0) == 1


async def claim_subagent_for_epoch(
    run_id: str,
    subtask_id: str,
    *,
    expected_epoch: int,
    owner_token: str,
    role: str = "",
    timeout: float | None = None,
) -> bool:
    kwargs = {}
    if timeout is not None:
        kwargs["timeout"] = timeout
    result = await redis_service.eval_script(
        _CLAIM_SUBAGENT_LUA,
        keys=[_state_key(run_id)],
        args=[
            str(subtask_id),
            str(role or ""),
            str(STATE_TTL_SECONDS),
            _now_iso(),
            str(int(expected_epoch)),
            str(owner_token or ""),
        ],
        **kwargs,
    )
    return int(result or 0) == 1


async def finalize_subagent_for_epoch(
    run_id: str,
    subtask_id: str,
    status: str,
    *,
    expected_epoch: int,
    owner_token: str,
    result_summary: str = "",
    role: str = "",
    timeout: float | None = None,
) -> bool:
    kwargs = {}
    if timeout is not None:
        kwargs["timeout"] = timeout
    result = await redis_service.eval_script(
        _FINALIZE_SUBAGENT_LUA,
        keys=[_state_key(run_id)],
        args=[
            str(subtask_id),
            str(status),
            str(role or ""),
            str(result_summary or ""),
            str(STATE_TTL_SECONDS),
            _now_iso(),
            str(int(expected_epoch)),
            str(owner_token or ""),
        ],
        **kwargs,
    )
    return int(result or 0) == 1


async def get_state(
    run_id: str,
    *,
    timeout: float | None = None,
) -> Optional[Dict[str, Any]]:
    """Return the full state payload if it exists."""
    kwargs = {}
    if timeout is not None:
        kwargs["timeout"] = timeout
    raw = await redis_service.get(_state_key(run_id), **kwargs)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception as exc:
        logger.warning(
            "Failed to parse shadow clone state JSON (run_id=%s): %s",
            run_id,
            exc,
        )
        return None


def _decode_state_payload(raw: Any) -> Optional[Dict[str, Any]]:
    if not raw:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    if not isinstance(raw, str):
        return None
    try:
        payload = json.loads(raw)
    except Exception as exc:
        logger.warning("Failed to decode shadow clone state payload: %s", exc)
        return None
    return payload if isinstance(payload, dict) else None


async def _write_state(run_id: str, state: Dict[str, Any]) -> Dict[str, Any]:
    serialized_state = json.dumps(state, ensure_ascii=False)
    now_iso = _now_iso()
    try:
        payload = await redis_service.eval_script(
            _WRITE_STATE_LUA,
            keys=[_state_key(run_id)],
            args=[
                serialized_state,
                str(STATE_TTL_SECONDS),
                now_iso,
            ],
        )
    except Exception:
        payload = None
    decoded = _decode_state_payload(payload)
    if decoded is not None:
        return decoded

    current_state = await get_state(run_id)
    preserved_state = _preserve_current_terminal_state(current_state, state)
    if preserved_state is not None:
        return preserved_state

    state["updated_at"] = now_iso
    await redis_service.set(
        _state_key(run_id),
        json.dumps(state, ensure_ascii=False),
        ex=STATE_TTL_SECONDS,
    )
    return state


async def get_execution_epoch(run_id: str) -> int:
    state = await get_state(run_id)
    if not state:
        return 0
    return int(state.get("execution_epoch") or 0)


async def stage_proposal_state(
    run_id: str,
    *,
    total: int,
    subtasks: Optional[list[Dict[str, Any]]] = None,
    dependencies: Optional[list[Dict[str, Any]]] = None,
) -> Optional[Dict[str, Any]]:
    normalized_subtasks = []
    for item in subtasks or []:
        if not isinstance(item, dict):
            continue
        normalized_subtasks.append(
            {
                "id": str(item.get("id") or "").strip(),
                "role": str(item.get("role") or "").strip(),
                "task_description": str(item.get("task_description") or "").strip(),
            }
        )

    normalized_dependencies = []
    for item in dependencies or []:
        if not isinstance(item, dict):
            continue
        normalized_dependencies.append(
            {
                "from_id": str(item.get("from_id") or "").strip(),
                "to_id": str(item.get("to_id") or "").strip(),
            }
        )

    payload = await redis_service.eval_script(
        _STAGE_PROPOSAL_LUA,
        keys=[_state_key(run_id)],
        args=[
            str(max(0, int(total or 0))),
            json.dumps(normalized_subtasks, ensure_ascii=False),
            json.dumps(normalized_dependencies, ensure_ascii=False),
            str(STATE_TTL_SECONDS),
            _now_iso(),
        ],
    )
    return _decode_state_payload(payload)


async def update_environment(
    run_id: str,
    *,
    status: Optional[str] = None,
    ready: Optional[bool] = None,
    last_error: Any = _UNSET,
    prepared_at: Optional[str] = None,
    manifest: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    payload = await redis_service.eval_script(
        _UPDATE_ENVIRONMENT_LUA,
        keys=[_state_key(run_id)],
        args=[
            "SET" if status is not None else "UNSET",
            str(status or "").strip().lower() or "pending",
            "SET" if ready is not None else "UNSET",
            "1" if bool(ready) else "0",
            "SET" if last_error is not _UNSET else "UNSET",
            str(last_error or "").strip() if last_error is not _UNSET else "",
            "SET" if prepared_at is not None else "UNSET",
            str(prepared_at or "").strip(),
            "SET" if isinstance(manifest, dict) else "UNSET",
            json.dumps(manifest or {}, ensure_ascii=False),
            str(STATE_TTL_SECONDS),
            _now_iso(),
        ],
    )
    return _decode_state_payload(payload)


async def update_live_activity(
    run_id: str,
    *,
    scope: str,
    phase: str,
    reason: Optional[str] = None,
    subtask_id: Optional[str] = None,
    epoch: Optional[int] = None,
) -> Optional[Dict[str, Any]]:
    payload = await redis_service.eval_script(
        _UPDATE_LIVE_ACTIVITY_LUA,
        keys=[_state_key(run_id)],
        args=[
            str(scope or "").strip() or "shadow_clone_main",
            str(phase or "").strip() or "planning",
            str(reason or "").strip(),
            str(subtask_id or "").strip(),
            "SET" if epoch is not None else "UNSET",
            str(int(epoch or 0)),
            str(STATE_TTL_SECONDS),
            _now_iso(),
        ],
    )
    return _decode_state_payload(payload)


async def mark_checkpoint(
    run_id: str,
    *,
    layer_index: int,
) -> Optional[Dict[str, Any]]:
    state = await get_state(run_id)
    if state is None:
        return None
    state["last_completed_layer"] = max(
        int(state.get("last_completed_layer") or -1),
        int(layer_index),
    )
    return await _write_state(run_id, state)


async def request_recovery(
    run_id: str,
    *,
    kind: str,
    error_code: str,
    message: str,
    source: str,
    scope: Optional[str] = None,
    failed_subtasks: Optional[list[Dict[str, Any]]] = None,
    retryable_subtasks: Optional[list[str]] = None,
) -> Optional[Dict[str, Any]]:
    state = await get_state(run_id)
    if state is None:
        return None
    state["recovery"] = {
        "pending": True,
        "kind": str(kind or "").strip() or "clone",
        "error_code": str(error_code or "").strip() or None,
        "message": str(message or "").strip() or None,
        "source": str(source or "").strip() or None,
        "scope": str(scope or "").strip() or None,
        "failed_subtasks": list(failed_subtasks or []),
        "retryable_subtasks": [
            str(item).strip()
            for item in (retryable_subtasks or [])
            if str(item).strip()
        ],
        "decision": None,
        "decision_source": None,
        "decision_at": None,
        "requested_at": _now_iso(),
    }
    return await _write_state(run_id, state)


async def record_recovery_decision(
    run_id: str,
    *,
    decision: str,
    decision_source: str,
    message: Optional[str] = None,
    failed_subtasks: Optional[list[Dict[str, Any]]] = None,
    retryable_subtasks: Optional[list[str]] = None,
    scope: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    state = await get_state(run_id)
    if state is None:
        return None

    recovery = state.get("recovery")
    if not isinstance(recovery, dict):
        recovery = {}

    recovery["pending"] = False
    recovery["decision"] = str(decision or "").strip() or None
    recovery["decision_source"] = str(decision_source or "").strip() or None
    recovery["decision_at"] = _now_iso()
    if message is not None:
        recovery["message"] = str(message or "").strip() or None
    if scope is not None:
        recovery["scope"] = str(scope or "").strip() or None
    if failed_subtasks is not None:
        recovery["failed_subtasks"] = list(failed_subtasks or [])
    if retryable_subtasks is not None:
        recovery["retryable_subtasks"] = [
            str(item).strip()
            for item in (retryable_subtasks or [])
            if str(item).strip()
        ]

    state["recovery"] = recovery
    return await _write_state(run_id, state)


async def clear_recovery(run_id: str) -> Optional[Dict[str, Any]]:
    state = await get_state(run_id)
    if state is None:
        return None
    state["recovery"] = {
        "pending": False,
        "kind": None,
        "error_code": None,
        "message": None,
        "source": None,
        "scope": None,
        "failed_subtasks": [],
        "retryable_subtasks": [],
        "decision": None,
        "decision_source": None,
        "decision_at": None,
        "requested_at": None,
    }
    return await _write_state(run_id, state)


async def bump_execution_epoch(run_id: str) -> int:
    state = await get_state(run_id)
    if state is None:
        return 0
    next_epoch = int(state.get("execution_epoch") or 0) + 1
    state["execution_epoch"] = next_epoch
    recovery = state.get("recovery")
    if not isinstance(recovery, dict):
        recovery = {}
    recovery["pending"] = False
    state["recovery"] = recovery
    await _write_state(run_id, state)
    return next_epoch


async def update_terminal_metadata(
    run_id: str,
    *,
    completion_mode: Any = _UNSET,
    terminal_reason: Any = _UNSET,
) -> Optional[Dict[str, Any]]:
    state = await get_state(run_id)
    if state is None:
        return None

    if completion_mode is not _UNSET:
        state["completion_mode"] = _normalize_optional_metadata_value(completion_mode)
    if terminal_reason is not _UNSET:
        state["terminal_reason"] = _normalize_optional_metadata_value(terminal_reason)
    return await _write_state(run_id, state)


async def force_terminal_state(
    run_id: str,
    *,
    status: str,
    reason: Optional[str] = None,
    error_message: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    normalized_status = str(status or "").strip().lower()
    if normalized_status not in TERMINAL_SHADOW_CLONE_STATUSES:
        raise ValueError(f"Unsupported terminal shadow clone status: {status}")

    state = await get_state(run_id)
    if state is None:
        return None

    next_epoch = max(0, int(state.get("execution_epoch") or 0)) + 1
    state["status"] = normalized_status
    state["execution_epoch"] = next_epoch
    state["completion_mode"] = None
    state["terminal_reason"] = (
        _normalize_optional_metadata_value(reason) or normalized_status
    )

    recovery = state.get("recovery")
    if not isinstance(recovery, dict):
        recovery = {}
    recovery["pending"] = False
    state["recovery"] = recovery

    live_activity = state.get("live_activity")
    if not isinstance(live_activity, dict):
        live_activity = {}
    live_activity.update(
        {
            "scope": str(live_activity.get("scope") or "shadow_clone_main"),
            "phase": normalized_status,
            "reason": str(reason or normalized_status).strip() or normalized_status,
            "subtask_id": None,
            "epoch": next_epoch,
            "updated_at": _now_iso(),
        }
    )
    state["live_activity"] = live_activity

    environment = state.get("environment")
    if not isinstance(environment, dict):
        environment = {}
    environment["ready"] = False
    environment["status"] = (
        "failed" if normalized_status == "failed" else normalized_status
    )
    if error_message is not None:
        environment["last_error"] = str(error_message or "").strip() or None
    state["environment"] = environment

    subagents = state.get("subagents")
    if not isinstance(subagents, dict):
        subagents = {}
    cancelled_count = 0
    completed = 0
    failed = 0
    running = 0
    now_iso = _now_iso()
    for payload in subagents.values():
        if not isinstance(payload, dict):
            continue
        subtask_status = str(payload.get("status") or "").strip().lower()
        if subtask_status in {"pending", "running"}:
            payload["status"] = "cancelled"
            payload["finished_at"] = now_iso
            payload["owner_token"] = None
            cancelled_count += 1
        final_status = str(payload.get("status") or "").strip().lower()
        if final_status == "completed":
            completed += 1
        elif final_status == "failed":
            failed += 1
        elif final_status == "running":
            running += 1
    state["subagents"] = subagents
    state["completed"] = completed
    state["failed"] = failed
    state["running"] = running
    if cancelled_count > 0:
        state["cancelled"] = cancelled_count

    return await _write_state(run_id, state)


async def reset_subagents_for_epoch(
    run_id: str,
    subtask_ids: list[str],
    *,
    expected_epoch: int,
    roles: Optional[Dict[str, str]] = None,
    timeout: float | None = None,
) -> None:
    for subtask_id in subtask_ids:
        await update_subagent_for_epoch(
            run_id,
            subtask_id,
            "pending",
            expected_epoch=expected_epoch,
            role=str((roles or {}).get(subtask_id) or ""),
            reset=True,
            timeout=timeout,
        )


async def initialize_subagent_attempts(
    run_id: str,
    *,
    subtask_ids: Optional[list[str]] = None,
) -> Optional[Dict[str, Any]]:
    state = await get_state(run_id)
    if state is None:
        return None

    targets = {
        str(subtask_id).strip()
        for subtask_id in (subtask_ids or [])
        if str(subtask_id).strip()
    }
    subagents = state.get("subagents")
    if not isinstance(subagents, dict):
        subagents = {}

    changed = False
    for subtask_id, payload in subagents.items():
        if targets and subtask_id not in targets:
            continue
        if not isinstance(payload, dict):
            continue
        if int(payload.get("attempt_index") or 0) < 1:
            payload["attempt_index"] = 1
            changed = True
        if not isinstance(payload.get("attempt_history"), list):
            payload["attempt_history"] = []
            changed = True
        if "failure_class" not in payload:
            payload["failure_class"] = None
            changed = True
        if "last_error" not in payload:
            payload["last_error"] = None
            changed = True
        if not isinstance(payload.get("recovery"), dict):
            payload["recovery"] = _empty_subagent_recovery()
            changed = True

    state["subagents"] = subagents
    if not changed:
        return state
    return await _write_state(run_id, state)


async def record_subagent_failures(
    run_id: str,
    *,
    failed_subtasks: list[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    state = await get_state(run_id)
    if state is None:
        return None

    subagents = state.get("subagents")
    if not isinstance(subagents, dict):
        subagents = {}

    changed = False
    for item in failed_subtasks:
        if not isinstance(item, dict):
            continue
        subtask_id = str(item.get("subtask_id") or "").strip()
        if not subtask_id:
            continue
        payload = subagents.get(subtask_id)
        if not isinstance(payload, dict):
            payload = {}
            subagents[subtask_id] = payload
        if int(payload.get("attempt_index") or 0) < 1:
            payload["attempt_index"] = 1
        if not isinstance(payload.get("attempt_history"), list):
            payload["attempt_history"] = []
        if not isinstance(payload.get("recovery"), dict):
            payload["recovery"] = _empty_subagent_recovery()
        payload["failure_class"] = str(item.get("failure_class") or "").strip() or None
        payload["last_error"] = str(item.get("error") or "").strip() or None
        changed = True

    state["subagents"] = subagents
    if not changed:
        return state
    return await _write_state(run_id, state)


async def prepare_subagents_for_retry(
    run_id: str,
    subtask_ids: list[str],
    *,
    expected_epoch: int,
    roles: Optional[Dict[str, str]] = None,
    timeout: float | None = None,
) -> Optional[Dict[str, Any]]:
    state = await get_state(run_id)
    if state is None:
        return None

    subagents = state.get("subagents")
    if not isinstance(subagents, dict):
        subagents = {}

    changed = False
    for subtask_id in subtask_ids:
        normalized_subtask_id = str(subtask_id or "").strip()
        if not normalized_subtask_id:
            continue
        payload = subagents.get(normalized_subtask_id)
        if not isinstance(payload, dict):
            payload = {}
            subagents[normalized_subtask_id] = payload

        attempt_index = max(1, int(payload.get("attempt_index") or 1))
        attempt_history = payload.get("attempt_history")
        if not isinstance(attempt_history, list):
            attempt_history = []

        snapshot = {
            "attempt_index": attempt_index,
            "status": str(payload.get("status") or "").strip() or None,
            "result_summary": str(payload.get("result_summary") or "").strip() or None,
            "error": str(payload.get("last_error") or "").strip() or None,
            "failure_class": str(payload.get("failure_class") or "").strip() or None,
            "started_at": payload.get("started_at"),
            "finished_at": payload.get("finished_at"),
        }
        if snapshot not in attempt_history:
            attempt_history.append(snapshot)

        payload["attempt_history"] = attempt_history
        payload["attempt_index"] = attempt_index + 1
        payload["failure_class"] = None
        payload["last_error"] = None
        changed = True

    state["subagents"] = subagents
    if changed:
        await _write_state(run_id, state)

    await reset_subagents_for_epoch(
        run_id,
        subtask_ids,
        expected_epoch=expected_epoch,
        roles=roles,
        timeout=timeout,
    )

    return await get_state(run_id)


async def update_subagent_recovery(
    run_id: str,
    subtask_id: str,
    *,
    expected_epoch: Optional[int] = None,
    mode: Any = _UNSET,
    phase: Any = _UNSET,
    reason: Any = _UNSET,
    wake_attempts: Any = _UNSET,
    replacement_attempts: Any = _UNSET,
    replacement_context_id: Any = _UNSET,
    handoff_summary: Any = _UNSET,
    last_command_id: Any = _UNSET,
) -> Optional[Dict[str, Any]]:
    state = await get_state(run_id)
    if state is None:
        return None
    if expected_epoch is not None and int(state.get("execution_epoch") or 0) != int(
        expected_epoch,
    ):
        return None

    subagents = state.get("subagents")
    if not isinstance(subagents, dict):
        subagents = {}

    payload = subagents.get(subtask_id)
    if not isinstance(payload, dict):
        payload = {}
        subagents[subtask_id] = payload

    recovery = _coerce_subagent_recovery(payload.get("recovery"))

    if mode is not _UNSET:
        recovery["mode"] = str(mode or "").strip() or None
    if phase is not _UNSET:
        recovery["phase"] = str(phase or "").strip() or None
    if reason is not _UNSET:
        recovery["reason"] = str(reason or "").strip() or None
    if wake_attempts is not _UNSET:
        recovery["wake_attempts"] = max(0, int(wake_attempts or 0))
    if replacement_attempts is not _UNSET:
        recovery["replacement_attempts"] = max(0, int(replacement_attempts or 0))
    if replacement_context_id is not _UNSET:
        recovery["replacement_context_id"] = (
            str(replacement_context_id or "").strip() or None
        )
    if handoff_summary is not _UNSET:
        recovery["handoff_summary"] = str(handoff_summary or "").strip() or None
    if last_command_id is not _UNSET:
        recovery["last_command_id"] = str(last_command_id or "").strip() or None

    recovery["updated_at"] = _now_iso()
    payload["recovery"] = recovery
    subagents[subtask_id] = payload
    state["subagents"] = subagents
    return await _write_state(run_id, state)


async def cleanup(run_id: str) -> None:
    """Retain the latest state snapshot for the configured TTL window."""
    await redis_service.expire(_state_key(run_id), STATE_TTL_SECONDS)
