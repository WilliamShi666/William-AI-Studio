"""Confirmation wait loop for Shadow Clone proposal execution."""

from __future__ import annotations

import time
from enum import Enum
from typing import Any

from services import redis as redis_service
from utils.logger import logger

from .constants import ConfirmationResult, SHADOW_CLONE_CONFIRMATION_TIMEOUT
from .state_machine import STATE_TTL_SECONDS


CONFIRMATION_DECISION_KEY_TEMPLATE = (
    "agent_run:{agent_run_id}:shadow_clone_confirmation"
)
CONFIRMATION_CHANNEL_TEMPLATE = "agent_run:{agent_run_id}:shadow_clone_confirm"
CONFIRMATION_DECISION_TTL_SECONDS = max(
    STATE_TTL_SECONDS,
    int(SHADOW_CLONE_CONFIRMATION_TIMEOUT or 0),
)

_WRITE_CONFIRMATION_DECISION_LUA = """
local key = KEYS[1]
local decision = string.upper(ARGV[1])
local ttl = tonumber(ARGV[2])

local existing = redis.call('GET', key)
if not existing then
  redis.call('SET', key, decision, 'EX', ttl)
  return 1
end

if string.upper(tostring(existing)) == decision then
  redis.call('EXPIRE', key, ttl)
  return 2
end

return -1
"""


class ConfirmationDecisionWriteStatus(str, Enum):
    RECORDED = "recorded"
    DUPLICATE = "duplicate"
    CONFLICT = "conflict"


def _normalize_message_data(raw_data) -> str:
    if isinstance(raw_data, bytes):
        return raw_data.decode("utf-8", errors="replace").strip().upper()
    return str(raw_data or "").strip().upper()


def confirmation_channel(agent_run_id: str) -> str:
    return CONFIRMATION_CHANNEL_TEMPLATE.format(agent_run_id=agent_run_id)


def _confirmation_decision_key(agent_run_id: str) -> str:
    return CONFIRMATION_DECISION_KEY_TEMPLATE.format(agent_run_id=agent_run_id)


def _coerce_confirmation_result(raw_data: Any) -> ConfirmationResult | None:
    payload = _normalize_message_data(raw_data)
    if payload == "CONFIRMED":
        return ConfirmationResult.CONFIRMED
    if payload == "DENIED":
        return ConfirmationResult.DENIED
    return None


async def get_confirmation_result(
    agent_run_id: str,
    *,
    timeout: float | None = None,
) -> ConfirmationResult | None:
    kwargs = {}
    if timeout is not None:
        kwargs["timeout"] = timeout
    raw_value = await redis_service.get(
        _confirmation_decision_key(agent_run_id),
        **kwargs,
    )
    return _coerce_confirmation_result(raw_value)


async def record_confirmation_result(
    agent_run_id: str,
    result: ConfirmationResult,
    *,
    timeout: float | None = None,
) -> tuple[ConfirmationDecisionWriteStatus, ConfirmationResult | None]:
    if result not in {ConfirmationResult.CONFIRMED, ConfirmationResult.DENIED}:
        raise ValueError(f"Unsupported confirmation result: {result!r}")

    kwargs = {}
    if timeout is not None:
        kwargs["timeout"] = timeout

    outcome = int(
        await redis_service.eval_script(
            _WRITE_CONFIRMATION_DECISION_LUA,
            keys=[_confirmation_decision_key(agent_run_id)],
            args=[result.value, str(CONFIRMATION_DECISION_TTL_SECONDS)],
            **kwargs,
        )
        or 0
    )
    if outcome == 1:
        return ConfirmationDecisionWriteStatus.RECORDED, result
    if outcome == 2:
        return ConfirmationDecisionWriteStatus.DUPLICATE, result
    if outcome == -1:
        return (
            ConfirmationDecisionWriteStatus.CONFLICT,
            await get_confirmation_result(agent_run_id, timeout=timeout),
        )
    raise RuntimeError(
        "Unexpected confirmation decision write outcome "
        f"{outcome} for run_id={agent_run_id}"
    )


async def _safe_get_confirmation_result(
    agent_run_id: str,
    *,
    timeout: float | None = None,
) -> ConfirmationResult | None:
    try:
        return await get_confirmation_result(agent_run_id, timeout=timeout)
    except Exception as exc:
        logger.warning(
            "Failed to read durable shadow clone confirmation result "
            "(run_id=%s): %s",
            agent_run_id,
            exc,
        )
        return None


async def wait_for_confirmation(
    agent_run_id: str,
    timeout: int | None = SHADOW_CLONE_CONFIRMATION_TIMEOUT,
) -> ConfirmationResult:
    """
    Wait for CONFIRMED / DENIED / STOP on Redis pub/sub channels.
    """
    persisted_result = await _safe_get_confirmation_result(agent_run_id)
    if persisted_result is not None:
        return persisted_result

    confirm_channel = confirmation_channel(agent_run_id)
    control_channel = f"agent_run:{agent_run_id}:control"
    deadline = (
        time.monotonic() + max(0, int(timeout))
        if timeout is not None
        else None
    )

    pubsub = await redis_service.create_pubsub()
    try:
        await pubsub.subscribe(confirm_channel, control_channel)

        while True:
            if deadline is None:
                poll_timeout = 1.0
            else:
                remaining = max(0.0, deadline - time.monotonic())
                poll_timeout = min(1.0, remaining)
                if poll_timeout <= 0:
                    break

            try:
                message = await pubsub.get_message(
                    ignore_subscribe_messages=True,
                    timeout=poll_timeout,
                )
            except ConnectionError as exc:
                logger.warning(
                    "Redis pubsub connection error during confirmation wait "
                    "(run_id=%s): %s. Attempting re-subscribe.",
                    agent_run_id,
                    exc,
                )
                persisted_result = await _safe_get_confirmation_result(agent_run_id)
                if persisted_result is not None:
                    return persisted_result
                await pubsub.subscribe(confirm_channel, control_channel)
                continue

            if not message or message.get("type") != "message":
                persisted_result = await _safe_get_confirmation_result(agent_run_id)
                if persisted_result is not None:
                    return persisted_result
                continue

            payload = _normalize_message_data(message.get("data"))
            if payload == "CONFIRMED":
                return ConfirmationResult.CONFIRMED
            if payload == "DENIED":
                return ConfirmationResult.DENIED
            if payload == "STOP":
                return ConfirmationResult.CANCELLED

        persisted_result = await _safe_get_confirmation_result(agent_run_id)
        if persisted_result is not None:
            return persisted_result
        return ConfirmationResult.TIMEOUT
    finally:
        try:
            await pubsub.unsubscribe(confirm_channel, control_channel)
        except Exception:
            pass
        try:
            await pubsub.close()
        except Exception:
            pass
