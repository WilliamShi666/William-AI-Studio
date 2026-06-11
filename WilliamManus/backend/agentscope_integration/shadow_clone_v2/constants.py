"""Constants for Shadow Clone V2."""

from __future__ import annotations

import os


def _read_int_env(name: str, default: int) -> int:
    raw_value = os.getenv(name)
    if raw_value is None or str(raw_value).strip() == "":
        return default
    try:
        value = int(str(raw_value).strip())
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


SHADOW_CLONE_V2_IDLE_TTL_SECONDS = _read_int_env(
    "SHADOW_CLONE_V2_IDLE_TTL_SECONDS",
    20 * 60,
)
SHADOW_CLONE_V2_MAX_SUBAGENTS_PER_RUN = min(
    _read_int_env(
        "SHADOW_CLONE_V2_MAX_SUBAGENTS_PER_RUN",
        10,
    ),
    10,
)
SHADOW_CLONE_V2_MAX_PARALLEL_SUBAGENTS = min(
    _read_int_env(
        "SHADOW_CLONE_V2_MAX_PARALLEL_SUBAGENTS",
        10,
    ),
    SHADOW_CLONE_V2_MAX_SUBAGENTS_PER_RUN,
    10,
)

SHADOW_CLONE_V2_EVENT_STREAM_MAXLEN = _read_int_env(
    "SHADOW_CLONE_V2_EVENT_STREAM_MAXLEN",
    5000,
)

SHADOW_CLONE_V2_MAILBOX_STREAM_MAXLEN = _read_int_env(
    "SHADOW_CLONE_V2_MAILBOX_STREAM_MAXLEN",
    1000,
)

SHADOW_CLONE_V2_STATE_TTL_SECONDS = _read_int_env(
    "SHADOW_CLONE_V2_STATE_TTL_SECONDS",
    24 * 60 * 60,
)
