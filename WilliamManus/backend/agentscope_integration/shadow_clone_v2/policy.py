"""Policy helpers for Shadow Clone V2 runtime limits."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterable

from .constants import (
    SHADOW_CLONE_V2_IDLE_TTL_SECONDS,
    SHADOW_CLONE_V2_MAX_SUBAGENTS_PER_RUN,
)


class ShadowCloneV2CapacityError(ValueError):
    """Raised when a V2 run would exceed configured capacity."""


@dataclass(frozen=True)
class SubagentCapacityDecision:
    """Capacity accounting for reused plus newly created subagents."""

    reused_count: int
    new_count: int
    total_subagents: int
    max_subagents: int = SHADOW_CLONE_V2_MAX_SUBAGENTS_PER_RUN


def _unique_names(names: Iterable[str]) -> set[str]:
    return {str(name or "").strip() for name in names if str(name or "").strip()}


def validate_run_subagent_capacity(
    *,
    reused_agent_names: Iterable[str],
    new_agent_names: Iterable[str],
    max_subagents: int = SHADOW_CLONE_V2_MAX_SUBAGENTS_PER_RUN,
) -> SubagentCapacityDecision:
    """Validate that one run uses at most max_subagents total agents.

    Reused idle agents count toward the same per-run limit as newly spawned
    agents. This prevents thread-scoped persistence from accumulating an
    unbounded team across follow-up runs.
    """
    if max_subagents <= 0:
        raise ShadowCloneV2CapacityError("max_subagents must be positive")

    reused = _unique_names(reused_agent_names)
    new = _unique_names(new_agent_names)
    overlapping = reused & new
    if overlapping:
        new = new - overlapping

    total = len(reused) + len(new)
    if total > max_subagents:
        raise ShadowCloneV2CapacityError(
            f"Shadow Clone V2 run may use at most {max_subagents} subagents; "
            f"requested {total} ({len(reused)} reused + {len(new)} new)."
        )

    return SubagentCapacityDecision(
        reused_count=len(reused),
        new_count=len(new),
        total_subagents=total,
        max_subagents=max_subagents,
    )


def compute_idle_expiration(
    *,
    now: datetime,
    idle_ttl_seconds: int = SHADOW_CLONE_V2_IDLE_TTL_SECONDS,
) -> datetime:
    """Return when a subagent entering idle should be shut down."""
    if idle_ttl_seconds <= 0:
        raise ValueError("idle_ttl_seconds must be positive")
    return now + timedelta(seconds=idle_ttl_seconds)
