from __future__ import annotations

import importlib


def test_max_subagents_env_cannot_raise_the_hard_cap_above_ten(monkeypatch) -> None:
    import agentscope_integration.shadow_clone_v2.constants as constants

    monkeypatch.setenv("SHADOW_CLONE_V2_MAX_SUBAGENTS_PER_RUN", "50")
    reloaded = importlib.reload(constants)

    assert reloaded.SHADOW_CLONE_V2_MAX_SUBAGENTS_PER_RUN == 10
    monkeypatch.delenv("SHADOW_CLONE_V2_MAX_SUBAGENTS_PER_RUN", raising=False)
    importlib.reload(constants)


def test_max_subagents_env_can_lower_the_cap(monkeypatch) -> None:
    import agentscope_integration.shadow_clone_v2.constants as constants

    monkeypatch.setenv("SHADOW_CLONE_V2_MAX_SUBAGENTS_PER_RUN", "4")
    reloaded = importlib.reload(constants)

    assert reloaded.SHADOW_CLONE_V2_MAX_SUBAGENTS_PER_RUN == 4
    monkeypatch.delenv("SHADOW_CLONE_V2_MAX_SUBAGENTS_PER_RUN", raising=False)
    importlib.reload(constants)


def test_max_parallel_subagents_defaults_to_ten(monkeypatch) -> None:
    import agentscope_integration.shadow_clone_v2.constants as constants

    monkeypatch.delenv("SHADOW_CLONE_V2_MAX_SUBAGENTS_PER_RUN", raising=False)
    monkeypatch.delenv("SHADOW_CLONE_V2_MAX_PARALLEL_SUBAGENTS", raising=False)
    reloaded = importlib.reload(constants)

    assert reloaded.SHADOW_CLONE_V2_MAX_PARALLEL_SUBAGENTS == 10


def test_max_parallel_subagents_env_can_lower_the_cap(monkeypatch) -> None:
    import agentscope_integration.shadow_clone_v2.constants as constants

    monkeypatch.setenv("SHADOW_CLONE_V2_MAX_PARALLEL_SUBAGENTS", "4")
    reloaded = importlib.reload(constants)

    assert reloaded.SHADOW_CLONE_V2_MAX_PARALLEL_SUBAGENTS == 4
    monkeypatch.delenv("SHADOW_CLONE_V2_MAX_PARALLEL_SUBAGENTS", raising=False)
    importlib.reload(constants)


def test_max_parallel_subagents_cannot_exceed_per_run_subagent_cap(monkeypatch) -> None:
    import agentscope_integration.shadow_clone_v2.constants as constants

    monkeypatch.setenv("SHADOW_CLONE_V2_MAX_SUBAGENTS_PER_RUN", "6")
    monkeypatch.setenv("SHADOW_CLONE_V2_MAX_PARALLEL_SUBAGENTS", "50")
    reloaded = importlib.reload(constants)

    assert reloaded.SHADOW_CLONE_V2_MAX_SUBAGENTS_PER_RUN == 6
    assert reloaded.SHADOW_CLONE_V2_MAX_PARALLEL_SUBAGENTS == 6
    monkeypatch.delenv("SHADOW_CLONE_V2_MAX_SUBAGENTS_PER_RUN", raising=False)
    monkeypatch.delenv("SHADOW_CLONE_V2_MAX_PARALLEL_SUBAGENTS", raising=False)
    importlib.reload(constants)
