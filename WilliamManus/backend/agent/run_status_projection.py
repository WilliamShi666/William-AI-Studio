from __future__ import annotations

import json
from typing import Any, Optional

from services import redis
from utils.logger import logger


_STREAM_TERMINAL_STATUSES = frozenset({"completed", "failed", "stopped"})
_STREAM_TAIL_SCAN_LIMIT = 200


def build_attempt_response_list_key(*, agent_run_id: str, execution_epoch: int) -> str:
    normalized_agent_run_id = str(agent_run_id or "").strip()
    normalized_execution_epoch = max(0, int(execution_epoch))
    if not normalized_agent_run_id:
        raise ValueError("agent_run_id is required")
    if normalized_execution_epoch < 1:
        raise ValueError("execution_epoch must be positive")
    return f"agent_run:{normalized_agent_run_id}:epoch:{normalized_execution_epoch}:responses"


def build_response_list_key(
    agent_run_id: str,
    *,
    execution_epoch: Optional[int] = None,
) -> str:
    normalized_execution_epoch = (
        None if execution_epoch is None else max(0, int(execution_epoch))
    )
    if normalized_execution_epoch:
        return build_attempt_response_list_key(
            agent_run_id=agent_run_id,
            execution_epoch=normalized_execution_epoch,
        )
    normalized_agent_run_id = str(agent_run_id or "").strip()
    if not normalized_agent_run_id:
        raise ValueError("agent_run_id is required")
    return f"agent_run:{normalized_agent_run_id}:responses"


def _normalize_stream_terminal_status(status_value: Any) -> Optional[str]:
    normalized = str(status_value or "").strip().lower()
    if normalized == "error":
        return "failed"
    if normalized in _STREAM_TERMINAL_STATUSES:
        return normalized
    return None


def _parse_response_item(raw_value: Any) -> Optional[dict[str, Any]]:
    if isinstance(raw_value, dict):
        return raw_value
    if not isinstance(raw_value, str):
        return None
    try:
        parsed = json.loads(raw_value)
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def _extract_terminal_status_from_items(
    raw_items: list[Any],
) -> tuple[Optional[str], Optional[str]]:
    for raw_item in reversed(raw_items):
        payload = _parse_response_item(raw_item)
        if not isinstance(payload, dict):
            continue
        if payload.get("type") != "status":
            continue

        terminal_status = _normalize_stream_terminal_status(payload.get("status"))
        if terminal_status is None:
            continue
        terminal_message = str(payload.get("message") or "").strip() or None
        return terminal_status, terminal_message

    return None, None


def read_terminal_status_from_attempt_tails(
    *,
    current_execution_epoch: int,
    tails_by_epoch: dict[int, list[Any]],
) -> tuple[Optional[str], Optional[str]]:
    normalized_epoch = max(0, int(current_execution_epoch or 0))
    if normalized_epoch > 0:
        current_tail = list(tails_by_epoch.get(normalized_epoch) or [])
        status, message = _extract_terminal_status_from_items(current_tail)
        if status is not None:
            return status, message

    for epoch in sorted(tails_by_epoch.keys(), reverse=True):
        if epoch == normalized_epoch:
            continue
        status, message = _extract_terminal_status_from_items(
            list(tails_by_epoch.get(epoch) or [])
        )
        if status is not None:
            return status, message

    return None, None


async def read_terminal_status_from_response_tail(
    agent_run_id: str,
    *,
    current_execution_epoch: Optional[int] = None,
    include_legacy_run_wide_fallback: bool = True,
    limit: int = _STREAM_TAIL_SCAN_LIMIT,
) -> tuple[Optional[str], Optional[str]]:
    normalized_agent_run_id = str(agent_run_id or "").strip()
    if not normalized_agent_run_id:
        return None, None

    try:
        tails_by_epoch: dict[int, list[Any]] = {}
        normalized_epoch = (
            None
            if current_execution_epoch is None
            else max(0, int(current_execution_epoch or 0))
        )
        if normalized_epoch:
            epoch_response_key = build_response_list_key(
                normalized_agent_run_id,
                execution_epoch=normalized_epoch,
            )
            tails_by_epoch[normalized_epoch] = list(
                await redis.lrange(epoch_response_key, -max(1, int(limit)), -1)
            )
        raw_items: list[Any] = []
        if current_execution_epoch is None or include_legacy_run_wide_fallback:
            response_list_key = build_response_list_key(normalized_agent_run_id)
            raw_items = await redis.lrange(response_list_key, -max(1, int(limit)), -1)
    except Exception as fetch_error:
        logger.warning(
            "Failed to inspect response tail for terminal status projection "
            "(agent_run_id=%s): %s",
            normalized_agent_run_id,
            fetch_error,
        )
        return None, None

    if current_execution_epoch is not None:
        if include_legacy_run_wide_fallback:
            tails_by_epoch[0] = list(raw_items)
        return read_terminal_status_from_attempt_tails(
            current_execution_epoch=max(0, int(current_execution_epoch or 0)),
            tails_by_epoch=tails_by_epoch,
        )

    return _extract_terminal_status_from_items(list(raw_items))


async def read_terminal_status_from_current_attempt_tail(
    agent_run_id: str,
    *,
    current_execution_epoch: Optional[int],
    limit: int = _STREAM_TAIL_SCAN_LIMIT,
) -> tuple[Optional[str], Optional[str]]:
    return await read_terminal_status_from_response_tail(
        agent_run_id,
        current_execution_epoch=current_execution_epoch,
        include_legacy_run_wide_fallback=True,
        limit=limit,
    )


async def read_terminal_status_from_current_attempt_tail(
    agent_run_id: str,
    *,
    current_execution_epoch: int,
    limit: int = _STREAM_TAIL_SCAN_LIMIT,
) -> tuple[Optional[str], Optional[str]]:
    return await read_terminal_status_from_response_tail(
        agent_run_id,
        current_execution_epoch=current_execution_epoch,
        include_legacy_run_wide_fallback=True,
        limit=limit,
    )
