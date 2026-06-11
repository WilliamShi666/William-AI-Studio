"""Redis-based ephemeral result storage for Shadow Clone subagent outputs."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from services import redis as redis_service
from utils.logger import logger

from .constants import (
    SHADOW_CLONE_FULL_RESULT_MAX_CHARS,
    SHADOW_CLONE_RESULT_TTL,
    SHADOW_CLONE_SUMMARY_MAX_CHARS,
)


RESULT_SPREADSHEET_KEY = "shadow_clone:{run_id}:results"
RESULT_SUMMARY_CACHE_KEY = "shadow_clone:{run_id}:results:summary_cache"
RESULT_FULL_KEY = "shadow_clone:{run_id}:full:{subtask_id}"
STATE_KEY = "shadow_clone:{run_id}:state"
_TERMINAL_RESULT_VISIBILITY_SENTINELS = {
    "completed": "Subagent completed without a visible textual result.",
    "failed": "Subagent failed before producing a visible textual result.",
    "cancelled": "Subagent stopped before producing a visible textual result.",
    "timeout": "Subagent timed out before producing a visible textual result.",
    "denied": "Subagent was denied before producing a visible textual result.",
}

_SUBMIT_RESULT_LUA = """
local spreadsheet_key = KEYS[1]
local full_key = KEYS[2]
local summary_cache_key = KEYS[3]
local state_key = KEYS[4]
local subtask_id = ARGV[1]
local row_json = ARGV[2]
local cache_row_json = ARGV[3]
local full_result = ARGV[4]
local ttl = tonumber(ARGV[5])
local expected_epoch = ARGV[6]
local expected_owner_token = ARGV[7]

if expected_epoch and expected_epoch ~= "" then
  local raw_state = redis.call('GET', state_key)
  if not raw_state then
    return 0
  end
  local state = cjson.decode(raw_state)
  local current_epoch = tonumber(state["execution_epoch"] or 0)
  if tonumber(expected_epoch) ~= current_epoch then
    return -1
  end
  if expected_owner_token and expected_owner_token ~= "" then
    local subagents = state["subagents"] or {}
    local sub = subagents[subtask_id]
    if sub == cjson.null or sub == nil then
      return -2
    end
    local current_owner_token = tostring(sub["owner_token"] or "")
    if current_owner_token == "" or current_owner_token ~= expected_owner_token then
      return -2
    end
  end
end

redis.call('HSET', spreadsheet_key, subtask_id, row_json)
redis.call('EXPIRE', spreadsheet_key, ttl)
redis.call('SET', full_key, full_result, 'EX', ttl)

local raw_cache = redis.call('GET', summary_cache_key)
if raw_cache then
  local decoded_ok, cache_rows = pcall(cjson.decode, raw_cache)
  if not decoded_ok or type(cache_rows) ~= 'table' then
    redis.call('DEL', summary_cache_key)
  else
    local row_ok, cache_row = pcall(cjson.decode, cache_row_json)
    if not row_ok or type(cache_row) ~= 'table' then
      redis.call('DEL', summary_cache_key)
    else
      local replaced = false
      for index, item in ipairs(cache_rows) do
        if type(item) == 'table' and tostring(item["subtask_id"] or "") == subtask_id then
          cache_rows[index] = cache_row
          replaced = true
          break
        end
      end
      if not replaced then
        table.insert(cache_rows, cache_row)
      end
      table.sort(
        cache_rows,
        function(a, b)
          local a_submitted_at = tostring(a["submitted_at"] or "")
          local b_submitted_at = tostring(b["submitted_at"] or "")
          if a_submitted_at == b_submitted_at then
            return tostring(a["subtask_id"] or "") < tostring(b["subtask_id"] or "")
          end
          return a_submitted_at < b_submitted_at
        end
      )
      redis.call('SET', summary_cache_key, cjson.encode(cache_rows), 'EX', ttl)
    end
  end
end
return 1
"""


class ShadowCloneResultStoreError(RuntimeError):
    """Base error for Shadow Clone result persistence failures."""


class ShadowCloneResultStateUnavailableError(ShadowCloneResultStoreError):
    """Raised when a result cannot be persisted because run state no longer exists."""


class ShadowCloneResultClaimLostError(ShadowCloneResultStoreError):
    """Raised when a result is persisted by an actor that no longer owns the subtask claim."""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _truncate_to_summary(text: str, max_chars: int = SHADOW_CLONE_SUMMARY_MAX_CHARS) -> str:
    """Truncate text for summary display, preferring sentence boundaries."""
    if not text:
        return ""
    if len(text) <= max_chars:
        return text

    truncated = text[:max_chars]
    for sep in (". ", ".\n", "! ", "? ", "。", "！", "？"):
        idx = truncated.rfind(sep)
        if idx > max_chars // 2:
            # Keep punctuation if separator is a single punctuation marker.
            if sep in {"。", "！", "？"}:
                return truncated[: idx + 1]
            return truncated[:idx].rstrip() + "."

    return truncated.rstrip() + "..."


def _sanitize_summary_row(row: Dict[str, Any]) -> Dict[str, Any]:
    allowed_keys = (
        "subtask_id",
        "role",
        "status",
        "summary",
        "submitted_at",
        "late_peer_notes_count",
        "attempt_index",
        "failure_class",
    )
    return {key: row[key] for key in allowed_keys if key in row}


def _summary_cache_key(run_id: str) -> str:
    return RESULT_SUMMARY_CACHE_KEY.format(run_id=run_id)


def _decode_summary_row(subtask_id: Any, value: Any) -> Optional[Dict[str, Any]]:
    try:
        parsed = json.loads(value) if isinstance(value, str) else {}
    except Exception:
        return None
    if not isinstance(parsed, dict):
        return None
    parsed["subtask_id"] = str(subtask_id)
    return _sanitize_summary_row(parsed)


def _sort_summary_rows(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(
        rows,
        key=lambda item: (
            str(item.get("submitted_at") or ""),
            str(item.get("subtask_id") or ""),
        ),
    )


async def _read_summary_cache(
    run_id: str,
    *,
    timeout: float | None = None,
) -> Optional[List[Dict[str, Any]]]:
    kwargs = {}
    if timeout is not None:
        kwargs["timeout"] = timeout
    raw_cache = await redis_service.get(_summary_cache_key(run_id), **kwargs)
    if not raw_cache:
        return None

    try:
        decoded = json.loads(raw_cache)
    except Exception:
        logger.warning(
            "Failed to parse shadow clone summary cache (run_id=%s); falling back to hash rows.",
            run_id,
        )
        return None

    if not isinstance(decoded, list):
        logger.warning(
            "Ignoring malformed shadow clone summary cache payload (run_id=%s).",
            run_id,
        )
        return None

    rows: List[Dict[str, Any]] = []
    for item in decoded:
        if not isinstance(item, dict):
            continue
        rows.append(_sanitize_summary_row(item))
    return rows


async def _write_summary_cache(
    run_id: str,
    rows: List[Dict[str, Any]],
    *,
    timeout: float | None = None,
) -> None:
    kwargs = {}
    if timeout is not None:
        kwargs["timeout"] = timeout
    await redis_service.set(
        _summary_cache_key(run_id),
        json.dumps(rows, ensure_ascii=False),
        ex=SHADOW_CLONE_RESULT_TTL,
        **kwargs,
    )


async def _invalidate_summary_cache(
    run_id: str,
    *,
    timeout: float | None = None,
) -> int:
    kwargs = {}
    if timeout is not None:
        kwargs["timeout"] = timeout
    return int(await redis_service.delete(_summary_cache_key(run_id), **kwargs) or 0)


async def submit_result(
    run_id: str,
    subtask_id: str,
    role: str,
    status: str,
    full_result: str,
    *,
    summary_override: str | None = None,
    late_peer_notes_count: int = 0,
    attempt_index: int | None = None,
    failure_class: str | None = None,
    expected_epoch: int | None = None,
    owner_token: str | None = None,
    timeout: float | None = None,
) -> Dict[str, Any]:
    """Store one subagent result into Redis spreadsheet + full text keys."""
    text = str(full_result or "")
    if len(text) > SHADOW_CLONE_FULL_RESULT_MAX_CHARS:
        logger.warning(
            "Truncating subagent result from %d to %d chars (run_id=%s subtask_id=%s)",
            len(text),
            SHADOW_CLONE_FULL_RESULT_MAX_CHARS,
            run_id,
            subtask_id,
        )
        text = text[:SHADOW_CLONE_FULL_RESULT_MAX_CHARS] + "\n\n[result truncated]"

    full_key = RESULT_FULL_KEY.format(run_id=run_id, subtask_id=subtask_id)
    spreadsheet_key = RESULT_SPREADSHEET_KEY.format(run_id=run_id)
    summary_source = str(summary_override if summary_override is not None else text)
    row = {
        "role": str(role or ""),
        "status": str(status or ""),
        "summary": _truncate_to_summary(summary_source),
        "submitted_at": _now_iso(),
    }
    if late_peer_notes_count > 0:
        row["late_peer_notes_count"] = int(late_peer_notes_count)
    if attempt_index is not None:
        row["attempt_index"] = max(1, int(attempt_index))
    if failure_class:
        row["failure_class"] = str(failure_class).strip()
    cache_row = {
        "subtask_id": str(subtask_id),
        **_sanitize_summary_row(row),
    }

    try:
        kwargs = {}
        if timeout is not None:
            kwargs["timeout"] = timeout
        keys = [spreadsheet_key, full_key, _summary_cache_key(run_id)]
        args = [
            str(subtask_id),
            json.dumps(row, ensure_ascii=False),
            json.dumps(cache_row, ensure_ascii=False),
            text,
            str(SHADOW_CLONE_RESULT_TTL),
        ]
        if expected_epoch is not None:
            keys.append(STATE_KEY.format(run_id=run_id))
            args.append(str(int(expected_epoch)))
            args.append(str(owner_token or ""))
        result = await redis_service.eval_script(
            _SUBMIT_RESULT_LUA,
            keys=keys,
            args=args,
            **kwargs,
        )
    except Exception:
        logger.error(
            "Failed to submit shadow clone result (run_id=%s subtask_id=%s)",
            run_id,
            subtask_id,
            exc_info=True,
        )
        raise

    result_code = int(result or 0)

    if result_code == -1:
        logger.info(
            "Ignoring stale shadow clone result for prior epoch (run_id=%s subtask_id=%s expected_epoch=%s)",
            run_id,
            subtask_id,
            expected_epoch,
        )
        return {"status": "stale_ignored", "subtask_id": str(subtask_id)}

    if result_code == 0 and expected_epoch is not None:
        raise ShadowCloneResultStateUnavailableError(
            "Shadow Clone result persistence aborted because run state is no longer available "
            f"(run_id={run_id} subtask_id={subtask_id} expected_epoch={expected_epoch})."
        )

    if result_code == -2 and expected_epoch is not None:
        raise ShadowCloneResultClaimLostError(
            "Shadow Clone result persistence aborted because subtask ownership changed "
            f"(run_id={run_id} subtask_id={subtask_id} expected_epoch={expected_epoch})."
        )

    if result_code != 1:
        raise ShadowCloneResultStoreError(
            f"Shadow Clone result persistence returned unexpected code {result_code} "
            f"(run_id={run_id} subtask_id={subtask_id})."
        )

    return {"status": "submitted", "subtask_id": str(subtask_id)}


async def read_summaries(run_id: str) -> List[Dict[str, Any]]:
    """Read all subagent result summaries for one run."""
    cached_rows = await _read_summary_cache(run_id)
    if cached_rows is not None:
        return cached_rows

    spreadsheet_key = RESULT_SPREADSHEET_KEY.format(run_id=run_id)
    try:
        raw = await redis_service.hgetall(spreadsheet_key)
    except Exception:
        logger.error(
            "Failed to read shadow clone summaries (run_id=%s)",
            run_id,
            exc_info=True,
        )
        return []

    if not raw:
        return []

    rows: List[Dict[str, Any]] = []
    for subtask_id, value in raw.items():
        parsed_row = _decode_summary_row(subtask_id, value)
        if parsed_row is None:
            logger.warning(
                "Skipping malformed summary row (run_id=%s subtask_id=%s)",
                run_id,
                subtask_id,
            )
            continue
        rows.append(parsed_row)

    rows = _sort_summary_rows(rows)
    if rows:
        try:
            await _write_summary_cache(run_id, rows)
        except Exception:
            logger.debug(
                "Failed to populate shadow clone summary cache (run_id=%s)",
                run_id,
                exc_info=True,
            )
    return rows


def _terminal_result_visibility_summary(status: Any) -> str:
    normalized_status = str(status or "").strip().lower()
    return _TERMINAL_RESULT_VISIBILITY_SENTINELS.get(
        normalized_status,
        "Subagent finished without a visible textual result.",
    )


async def ensure_terminal_result_summaries_visible(
    run_id: str,
    expected_results: List[Dict[str, Any]],
    *,
    timeout: float | None = None,
) -> List[str]:
    visible_rows = {
        str(row.get("subtask_id") or "").strip(): row
        for row in await read_summaries(run_id)
        if isinstance(row, dict) and str(row.get("subtask_id") or "").strip()
    }
    updated_subtask_ids: List[str] = []

    for item in expected_results:
        if not isinstance(item, dict):
            continue
        subtask_id = str(item.get("subtask_id") or "").strip()
        if not subtask_id:
            continue

        status = str(item.get("status") or "").strip().lower() or "completed"
        existing_row = visible_rows.get(subtask_id) or {}
        existing_status = str(existing_row.get("status") or "").strip().lower()
        if existing_status == status:
            continue

        summary_text = (
            str(item.get("summary") or item.get("result_summary") or item.get("error") or "").strip()
            or _terminal_result_visibility_summary(status)
        )
        await submit_result(
            run_id=run_id,
            subtask_id=subtask_id,
            role=str(item.get("role") or "").strip(),
            status=status,
            full_result=summary_text,
            summary_override=summary_text,
            attempt_index=(
                int(item.get("attempt_index"))
                if item.get("attempt_index") is not None
                else None
            ),
            failure_class=(
                str(item.get("failure_class") or "").strip() or None
            ),
            timeout=timeout,
        )
        updated_subtask_ids.append(subtask_id)
        visible_rows[subtask_id] = {
            "subtask_id": subtask_id,
            "status": status,
            "summary": summary_text,
        }

    return updated_subtask_ids


async def read_full_result(run_id: str, subtask_id: str) -> Optional[str]:
    """Read one full subagent result by subtask id."""
    full_key = RESULT_FULL_KEY.format(run_id=run_id, subtask_id=subtask_id)
    try:
        return await redis_service.get(full_key)
    except Exception:
        logger.error(
            "Failed to read full shadow clone result (run_id=%s subtask_id=%s)",
            run_id,
            subtask_id,
            exc_info=True,
        )
        return None


async def cleanup_results(run_id: str) -> int:
    """Eagerly delete spreadsheet and full result keys for one run."""
    spreadsheet_key = RESULT_SPREADSHEET_KEY.format(run_id=run_id)
    deleted = 0
    try:
        raw = await redis_service.hgetall(spreadsheet_key)
        full_keys = [
            RESULT_FULL_KEY.format(run_id=run_id, subtask_id=subtask_id)
            for subtask_id in (raw or {}).keys()
        ]
        for key in [spreadsheet_key, _summary_cache_key(run_id), *full_keys]:
            deleted += int(await redis_service.delete(key) or 0)
    except Exception:
        logger.warning(
            "Failed eager cleanup of shadow clone results (run_id=%s)",
            run_id,
            exc_info=True,
        )
    return deleted


async def delete_results(run_id: str, subtask_ids: List[str]) -> int:
    deleted = 0
    spreadsheet_key = RESULT_SPREADSHEET_KEY.format(run_id=run_id)
    for subtask_id in subtask_ids:
        if not subtask_id:
            continue
        full_key = RESULT_FULL_KEY.format(run_id=run_id, subtask_id=subtask_id)
        try:
            deleted += int(await redis_service.hdel(spreadsheet_key, str(subtask_id)) or 0)
        except Exception:
            logger.warning(
                "Failed to delete shadow clone summary row (run_id=%s subtask_id=%s)",
                run_id,
                subtask_id,
                exc_info=True,
            )
        try:
            deleted += int(await redis_service.delete(full_key) or 0)
        except Exception:
            logger.warning(
                "Failed to delete shadow clone full result (run_id=%s subtask_id=%s)",
                run_id,
                subtask_id,
                exc_info=True,
            )
    try:
        deleted += await _invalidate_summary_cache(run_id)
    except Exception:
        logger.warning(
            "Failed to invalidate shadow clone summary cache (run_id=%s)",
            run_id,
            exc_info=True,
        )
    return deleted
