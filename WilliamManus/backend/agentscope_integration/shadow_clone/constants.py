"""Constants and mode resolution helpers for Shadow Clone."""

import logging
import os
from enum import Enum
from typing import Optional, Union

logger = logging.getLogger(__name__)


class ShadowCloneMode(str, Enum):
    OFF = "off"
    ON = "on"
    AUTO = "auto"
    V2 = "v2"


class ConfirmationResult(str, Enum):
    CONFIRMED = "confirmed"
    DENIED = "denied"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"


def _read_int_env(name: str, default: int) -> int:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default

    try:
        return int(raw_value)
    except (TypeError, ValueError):
        logger.warning(
            "Invalid integer for %s=%r, using default=%s",
            name,
            raw_value,
            default,
        )
        return default


def _read_optional_int_env(name: str, default: Optional[int]) -> Optional[int]:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default

    normalized = raw_value.strip().lower()
    if normalized in {"", "none", "null", "off", "disabled", "infinite", "infinity"}:
        return None

    try:
        return int(raw_value)
    except (TypeError, ValueError):
        logger.warning(
            "Invalid optional integer for %s=%r, using default=%r",
            name,
            raw_value,
            default,
        )
        return default


def _read_float_env(name: str, default: float) -> float:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default

    try:
        return float(raw_value)
    except (TypeError, ValueError):
        logger.warning(
            "Invalid float for %s=%r, using default=%s",
            name,
            raw_value,
            default,
        )
        return default


def _read_bool_env(name: str, default: bool) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default

    normalized = raw_value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False

    logger.warning(
        "Invalid boolean for %s=%r, using default=%s",
        name,
        raw_value,
        default,
    )
    return default


def _resolve_default_mode() -> ShadowCloneMode:
    raw_value = (os.getenv("SHADOW_CLONE_DEFAULT_MODE") or ShadowCloneMode.V2.value).strip().lower()
    try:
        return ShadowCloneMode(raw_value)
    except ValueError:
        logger.warning(
            "Invalid SHADOW_CLONE_DEFAULT_MODE=%r, falling back to %s",
            raw_value,
            ShadowCloneMode.V2.value,
        )
        return ShadowCloneMode.V2


def _coerce_optional_bool(value: Union[bool, str, int, None]) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return bool(value)

    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return None


SHADOW_CLONE_DEFAULT_MODE = _resolve_default_mode()
SHADOW_CLONE_CONFIRMATION_TIMEOUT = _read_optional_int_env(
    "SHADOW_CLONE_CONFIRMATION_TIMEOUT",
    None,
)
SHADOW_CLONE_SUBAGENT_TIMEOUT = _read_int_env("SHADOW_CLONE_SUBAGENT_TIMEOUT", 1200)
SHADOW_CLONE_SANDBOX_CMD_TIMEOUT = _read_int_env("SHADOW_CLONE_SANDBOX_CMD_TIMEOUT", 600)
SHADOW_CLONE_COORDINATOR_TIME_LIMIT = _read_int_env("SHADOW_CLONE_COORDINATOR_TIME_LIMIT", 3600)
SHADOW_CLONE_SUBAGENT_ACTOR_TIME_LIMIT = _read_int_env(
    "SHADOW_CLONE_SUBAGENT_ACTOR_TIME_LIMIT",
    1500,
)
SHADOW_CLONE_MAX_SUBAGENTS = _read_int_env("SHADOW_CLONE_MAX_SUBAGENTS", 50)
SHADOW_CLONE_MAX_CONCURRENT = _read_int_env("SHADOW_CLONE_MAX_CONCURRENT", 10)
SHADOW_CLONE_PLAN_BATCH_TARGET = max(
    1,
    min(
        SHADOW_CLONE_MAX_CONCURRENT,
        _read_int_env("SHADOW_CLONE_PLAN_BATCH_TARGET", SHADOW_CLONE_MAX_CONCURRENT),
    ),
)
SHADOW_CLONE_RUNTIME_DISPATCH_WINDOW = max(
    1,
    min(
        SHADOW_CLONE_MAX_CONCURRENT,
        _read_int_env(
            "SHADOW_CLONE_RUNTIME_DISPATCH_WINDOW",
            SHADOW_CLONE_MAX_CONCURRENT,
        ),
    ),
)
SHADOW_CLONE_MAIN_MODEL = os.getenv("SHADOW_CLONE_MAIN_MODEL", "openrouter-kimi-k2.5")
SHADOW_CLONE_SUBAGENT_MODEL = os.getenv(
    "SHADOW_CLONE_SUBAGENT_MODEL",
    "openrouter-minimax-m2.5",
)
SHADOW_CLONE_RESULT_TTL = _read_int_env("SHADOW_CLONE_RESULT_TTL", 86400)
SHADOW_CLONE_SUMMARY_MAX_CHARS = _read_int_env("SHADOW_CLONE_SUMMARY_MAX_CHARS", 200)
SHADOW_CLONE_FULL_RESULT_MAX_CHARS = _read_int_env(
    "SHADOW_CLONE_FULL_RESULT_MAX_CHARS",
    12000,
)
SHADOW_CLONE_PEER_NOTE_TTL = _read_int_env(
    "SHADOW_CLONE_PEER_NOTE_TTL",
    SHADOW_CLONE_RESULT_TTL,
)
SHADOW_CLONE_PEER_NOTE_MAX_SUMMARY_CHARS = _read_int_env(
    "SHADOW_CLONE_PEER_NOTE_MAX_SUMMARY_CHARS",
    240,
)
SHADOW_CLONE_PEER_NOTE_MAX_DETAILS_CHARS = _read_int_env(
    "SHADOW_CLONE_PEER_NOTE_MAX_DETAILS_CHARS",
    400,
)
SHADOW_CLONE_PEER_NOTE_MAX_INJECTED_NOTES_PER_DRAIN = _read_int_env(
    "SHADOW_CLONE_PEER_NOTE_MAX_INJECTED_NOTES_PER_DRAIN",
    4,
)
SHADOW_CLONE_PEER_NOTE_MAX_INJECTION_CHARS = _read_int_env(
    "SHADOW_CLONE_PEER_NOTE_MAX_INJECTION_CHARS",
    2400,
)
SHADOW_CLONE_PEER_NOTE_MAX_APPENDIX_CHARS = _read_int_env(
    "SHADOW_CLONE_PEER_NOTE_MAX_APPENDIX_CHARS",
    2400,
)
SHADOW_CLONE_REDIS_WRITE_TIMEOUT_SECONDS = _read_float_env(
    "SHADOW_CLONE_REDIS_WRITE_TIMEOUT_SECONDS",
    _read_float_env("AGENTSCOPE_REDIS_WRITE_TIMEOUT_SECONDS", 10.0),
)
SHADOW_CLONE_REDIS_WRITE_RETRY_ATTEMPTS = _read_int_env(
    "SHADOW_CLONE_REDIS_WRITE_RETRY_ATTEMPTS",
    _read_int_env("AGENTSCOPE_REDIS_WRITE_RETRY_ATTEMPTS", 3),
)
SHADOW_CLONE_COORDINATOR_RECONCILE_INTERVAL_SECONDS = _read_float_env(
    "SHADOW_CLONE_COORDINATOR_RECONCILE_INTERVAL_SECONDS",
    1.0,
)
SHADOW_CLONE_ENABLE_PROACTIVE_STANDBY = _read_bool_env(
    "SHADOW_CLONE_ENABLE_PROACTIVE_STANDBY",
    False,
)
SHADOW_CLONE_RUNTIME_WAKE_MAX_ATTEMPTS = _read_int_env(
    "SHADOW_CLONE_RUNTIME_WAKE_MAX_ATTEMPTS",
    1,
)
SHADOW_CLONE_RUNTIME_REPLACEMENT_MAX_ATTEMPTS = _read_int_env(
    "SHADOW_CLONE_RUNTIME_REPLACEMENT_MAX_ATTEMPTS",
    1,
)
SHADOW_CLONE_RUNTIME_HANDOFF_MAX_CHARS = _read_int_env(
    "SHADOW_CLONE_RUNTIME_HANDOFF_MAX_CHARS",
    1200,
)


def is_shadow_clone_proactive_standby_enabled() -> bool:
    return _read_bool_env(
        "SHADOW_CLONE_ENABLE_PROACTIVE_STANDBY",
        SHADOW_CLONE_ENABLE_PROACTIVE_STANDBY,
    )


def resolve_shadow_clone_mode(
    shadow_clone_mode: Optional[str] = None,
    shadow_clone_enabled: Union[bool, str, int, None] = None,
    execution_origin: Optional[str] = None,
) -> ShadowCloneMode:
    """
    Resolve the effective shadow clone mode.

    Resolution order:
    1. execution_origin is automation/trigger/workflow -> OFF
    2. valid explicit shadow_clone_mode -> that mode
    3. legacy shadow_clone_enabled (bool-like) -> ON/OFF
    4. default -> SHADOW_CLONE_DEFAULT_MODE
    """
    origin = (execution_origin or "").strip().lower()
    if origin in {"automation", "trigger", "workflow"}:
        return ShadowCloneMode.OFF

    if shadow_clone_mode is not None:
        normalized_mode = (
            shadow_clone_mode.value
            if isinstance(shadow_clone_mode, ShadowCloneMode)
            else str(shadow_clone_mode)
        ).strip().lower()
        if normalized_mode:
            try:
                return ShadowCloneMode(normalized_mode)
            except ValueError:
                logger.warning(
                    "Invalid shadow_clone_mode=%r. Falling back to compatibility/default resolution.",
                    shadow_clone_mode,
                )

    legacy_enabled = _coerce_optional_bool(shadow_clone_enabled)
    if legacy_enabled is True:
        return ShadowCloneMode.ON
    if legacy_enabled is False:
        return ShadowCloneMode.OFF

    return SHADOW_CLONE_DEFAULT_MODE
