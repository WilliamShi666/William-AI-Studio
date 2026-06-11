"""Thread-scoped agent registry helpers for Shadow Clone V2."""

from __future__ import annotations

from datetime import datetime
from typing import Iterable

from .models import AgentIdentity, AgentLifecycleStatus
from .policy import compute_idle_expiration


def _parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    normalized = str(value).strip()
    if normalized.endswith("Z"):
        normalized = normalized[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(normalized)
    except ValueError:
        return None


def mark_agent_idle(agent: AgentIdentity, *, now: datetime) -> AgentIdentity:
    """Return an updated idle copy of an agent with a bounded TTL."""
    idle_expires_at = compute_idle_expiration(now=now)
    return agent.model_copy(
        update={
            "status": AgentLifecycleStatus.IDLE,
            "idle_since": now.isoformat(),
            "idle_expires_at": idle_expires_at.isoformat(),
        }
    )


def find_expired_idle_agents(
    agents: Iterable[AgentIdentity],
    *,
    now: datetime,
) -> list[AgentIdentity]:
    """Return idle agents whose idle TTL has expired."""
    expired: list[AgentIdentity] = []
    for agent in agents:
        if agent.status != AgentLifecycleStatus.IDLE:
            continue
        expires_at = _parse_iso_datetime(agent.idle_expires_at)
        if expires_at is not None and expires_at <= now:
            expired.append(agent)
    return expired
