"""Resolve per-runtime Dramatiq queue names.

Queue isolation prevents older worker pools from consuming messages produced by
newer code when multiple dev runtimes share the same Redis broker.
"""

from __future__ import annotations

import os


def _read_queue_name(name: str, default: str) -> str:
    raw_value = str(os.getenv(name) or "").strip()
    return raw_value or default


RUN_AGENT_BACKGROUND_QUEUE = _read_queue_name(
    "DRAMATIQ_RUN_AGENT_QUEUE",
    "default",
)
SANDBOX_CLEANUP_QUEUE = _read_queue_name(
    "DRAMATIQ_SANDBOX_CLEANUP_QUEUE",
    "sandbox_cleanup",
)
SHADOW_CLONE_SUBAGENT_QUEUE = _read_queue_name(
    "SHADOW_CLONE_SUBAGENT_QUEUE",
    "shadow_clone_subagents",
)
REGULAR_SUPERVISOR_QUEUE = _read_queue_name(
    "REGULAR_SUPERVISOR_QUEUE",
    "regular_supervisor",
)
