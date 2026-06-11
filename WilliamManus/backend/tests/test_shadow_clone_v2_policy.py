from __future__ import annotations

from datetime import datetime, timezone

import pytest

from agentscope_integration.shadow_clone_v2.constants import (
    SHADOW_CLONE_V2_IDLE_TTL_SECONDS,
)
from agentscope_integration.shadow_clone_v2.policy import (
    ShadowCloneV2CapacityError,
    compute_idle_expiration,
    validate_run_subagent_capacity,
)


def test_validate_run_capacity_counts_reused_and_new_subagents_together() -> None:
    decision = validate_run_subagent_capacity(
        reused_agent_names=[f"agent-{index}" for index in range(6)],
        new_agent_names=[f"new-{index}" for index in range(4)],
    )

    assert decision.total_subagents == 10
    assert decision.reused_count == 6
    assert decision.new_count == 4


def test_validate_run_capacity_rejects_more_than_ten_total_subagents() -> None:
    with pytest.raises(ShadowCloneV2CapacityError, match="at most 10 subagents"):
        validate_run_subagent_capacity(
            reused_agent_names=[f"agent-{index}" for index in range(7)],
            new_agent_names=[f"new-{index}" for index in range(4)],
        )


def test_compute_idle_expiration_uses_twenty_minute_ttl_without_extending_on_heartbeat() -> None:
    now = datetime(2026, 5, 31, 12, 0, 0, tzinfo=timezone.utc)

    expiration = compute_idle_expiration(now=now)

    assert expiration.isoformat() == "2026-05-31T12:20:00+00:00"
    assert SHADOW_CLONE_V2_IDLE_TTL_SECONDS == 1200
