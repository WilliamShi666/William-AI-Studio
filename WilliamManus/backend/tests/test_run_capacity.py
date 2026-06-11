from __future__ import annotations

import pytest

from services import run_capacity


def test_get_server_concurrency_budget_prefers_explicit_env(monkeypatch):
    monkeypatch.setenv("AGENTSCOPE_SERVER_CONCURRENCY_BUDGET", "23")
    monkeypatch.setenv("DRAMATIQ_PROCESSES", "2")
    monkeypatch.setenv("DRAMATIQ_THREADS", "8")

    assert run_capacity.get_server_concurrency_budget() == 23


def test_get_server_concurrency_budget_derives_from_dramatiq_env(monkeypatch):
    monkeypatch.delenv("AGENTSCOPE_SERVER_CONCURRENCY_BUDGET", raising=False)
    monkeypatch.setenv("DRAMATIQ_PROCESSES", "2")
    monkeypatch.setenv("DRAMATIQ_THREADS", "8")

    assert run_capacity.get_server_concurrency_budget() == 16


def test_get_run_capacity_budget_caps_regular_mode_when_regular_admission_budget_is_set(
    monkeypatch,
):
    monkeypatch.setenv("AGENTSCOPE_SERVER_CONCURRENCY_BUDGET", "16")
    monkeypatch.setenv("AGENTSCOPE_SERVER_REGULAR_ADMISSION_BUDGET", "12")

    assert run_capacity.get_run_capacity_budget("off") == 12
    assert run_capacity.get_run_capacity_budget("on") == 16
    assert run_capacity.get_run_capacity_budget("auto") == 16
    assert run_capacity.get_run_capacity_budget("v2") == 16


def test_get_run_capacity_budget_clamps_regular_admission_budget_to_total_budget(
    monkeypatch,
):
    monkeypatch.setenv("AGENTSCOPE_SERVER_CONCURRENCY_BUDGET", "10")
    monkeypatch.setenv("AGENTSCOPE_SERVER_REGULAR_ADMISSION_BUDGET", "14")

    assert run_capacity.get_run_capacity_budget("off") == 10


def test_get_run_capacity_cost_treats_shadow_clone_auto_as_weighted(monkeypatch):
    monkeypatch.setenv("AGENTSCOPE_SERVER_REGULAR_RUN_COST", "1")
    monkeypatch.setenv("AGENTSCOPE_SERVER_SHADOW_CLONE_RUN_COST", "4")

    assert run_capacity.get_run_capacity_cost("off") == 1
    assert run_capacity.get_run_capacity_cost("auto") == 4
    assert run_capacity.get_run_capacity_cost("on") == 4
    assert run_capacity.get_run_capacity_cost("v2") == 4


def test_get_run_capacity_cost_defaults_shadow_clone_local_mode_to_two(monkeypatch):
    monkeypatch.delenv("AGENTSCOPE_SERVER_SHADOW_CLONE_RUN_COST", raising=False)
    monkeypatch.setenv("SHADOW_CLONE_SUBAGENT_EXECUTION_MODE", "local")

    assert run_capacity.get_run_capacity_cost("on") == 2
    assert run_capacity.get_run_capacity_cost("auto") == 2
    assert run_capacity.get_run_capacity_cost("v2") == 2


def test_get_run_capacity_cost_keeps_regular_cost_unchanged_under_local_shadow_clone_mode(
    monkeypatch,
):
    monkeypatch.delenv("AGENTSCOPE_SERVER_REGULAR_RUN_COST", raising=False)
    monkeypatch.setenv("SHADOW_CLONE_SUBAGENT_EXECUTION_MODE", "local")

    assert run_capacity.get_run_capacity_cost("off") == 1


def test_get_run_capacity_cost_keeps_remote_shadow_clone_default_when_not_local(monkeypatch):
    monkeypatch.delenv("AGENTSCOPE_SERVER_SHADOW_CLONE_RUN_COST", raising=False)
    monkeypatch.setenv("SHADOW_CLONE_SUBAGENT_EXECUTION_MODE", "dramatiq")

    assert run_capacity.get_run_capacity_cost("on") == 3
    assert run_capacity.get_run_capacity_cost("v2") == 3


def test_get_run_capacity_cost_explicit_shadow_clone_env_overrides_local_mode_default(
    monkeypatch,
):
    monkeypatch.setenv("AGENTSCOPE_SERVER_SHADOW_CLONE_RUN_COST", "5")
    monkeypatch.setenv("SHADOW_CLONE_SUBAGENT_EXECUTION_MODE", "local")

    assert run_capacity.get_run_capacity_cost("on") == 5
    assert run_capacity.get_run_capacity_cost("v2") == 5


def test_prod_regular_supervisor_defaults_allow_48_shadow_clone_v2_runs(
    monkeypatch,
):
    monkeypatch.setenv("AGENTSCOPE_SERVER_CONCURRENCY_BUDGET", "96")
    monkeypatch.setenv("AGENTSCOPE_SERVER_SHADOW_CLONE_RUN_COST", "2")
    monkeypatch.setenv("AGENTSCOPE_SERVER_REGULAR_ADMISSION_BUDGET", "48")

    assert (
        run_capacity.get_run_capacity_budget("auto")
        // run_capacity.get_run_capacity_cost("auto")
        == 48
    )
    assert (
        run_capacity.get_run_capacity_budget("on")
        // run_capacity.get_run_capacity_cost("on")
        == 48
    )
    assert run_capacity.get_run_capacity_budget("off") == 48


def test_get_run_capacity_kind_treats_v2_as_shadow_clone():
    assert run_capacity.get_run_capacity_kind("v2") == "shadow_clone"


@pytest.mark.asyncio
async def test_try_acquire_run_capacity_lease_parses_acquired_eval_result(monkeypatch):
    monkeypatch.setenv("AGENTSCOPE_SERVER_CONCURRENCY_BUDGET", "16")
    monkeypatch.setenv("AGENTSCOPE_SERVER_SHADOW_CLONE_RUN_COST", "3")
    monkeypatch.setattr(run_capacity.time, "time", lambda: 1234)

    captured = {}

    async def _fake_eval_script(*_args, **kwargs):
        captured["keys"] = list(kwargs.get("keys") or [])
        captured["args"] = list(kwargs.get("args") or [])
        return [1, 9, 16, 3, 0]

    monkeypatch.setattr(run_capacity.redis, "eval_script", _fake_eval_script)

    result = await run_capacity.try_acquire_run_capacity_lease(
        agent_run_id="run-1",
        mode="on",
        owner_token="worker-1",
        allow_takeover=True,
    )

    assert result == {
        "enabled": True,
        "budget": 16,
        "in_use": 9,
        "remaining": 7,
        "requested_cost": 3,
        "can_admit": True,
        "capacity_kind": "shadow_clone",
        "acquired": True,
        "refreshed": False,
        "taken_over": False,
        "owner_conflict": False,
        "lease_ttl_seconds": 90,
    }
    assert captured == {
        "keys": [
            "server_run_capacity:leases",
            "server_run_capacity:owners",
            "server_run_capacity:expiries",
        ],
        "args": ["16", "run-1", "worker-1", "3", "90", "1234", "1"],
    }


@pytest.mark.asyncio
async def test_try_acquire_run_capacity_lease_caps_regular_budget_but_preserves_shadow_clone_budget(
    monkeypatch,
):
    monkeypatch.setenv("AGENTSCOPE_SERVER_CONCURRENCY_BUDGET", "16")
    monkeypatch.setenv("AGENTSCOPE_SERVER_REGULAR_ADMISSION_BUDGET", "12")
    monkeypatch.setenv("AGENTSCOPE_SERVER_REGULAR_RUN_COST", "1")
    monkeypatch.setenv("AGENTSCOPE_SERVER_SHADOW_CLONE_RUN_COST", "2")
    monkeypatch.setattr(run_capacity.time, "time", lambda: 1234)

    captured_args = []

    async def _fake_eval_script(*_args, **kwargs):
        captured_args.append(list(kwargs.get("args") or []))
        budget = int(captured_args[-1][0])
        requested_cost = int(captured_args[-1][3])
        return [1, requested_cost, budget, requested_cost, 0]

    monkeypatch.setattr(run_capacity.redis, "eval_script", _fake_eval_script)

    regular_result = await run_capacity.try_acquire_run_capacity_lease(
        agent_run_id="run-regular",
        mode="off",
        owner_token="worker-regular",
    )
    shadow_result = await run_capacity.try_acquire_run_capacity_lease(
        agent_run_id="run-shadow",
        mode="on",
        owner_token="worker-shadow",
    )

    assert captured_args == [
        ["12", "run-regular", "worker-regular", "1", "90", "1234", "0"],
        ["16", "run-shadow", "worker-shadow", "2", "90", "1234", "0"],
    ]
    assert regular_result["budget"] == 12
    assert shadow_result["budget"] == 16


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("eval_result", "expected"),
    [
        (
            [0, 15, 16, 3, 0],
            {
                "enabled": True,
                "budget": 16,
                "in_use": 15,
                "remaining": 1,
                "requested_cost": 3,
                "can_admit": False,
                "capacity_kind": "shadow_clone",
                "acquired": False,
                "refreshed": False,
                "taken_over": False,
                "owner_conflict": False,
                "lease_ttl_seconds": 90,
            },
        ),
        (
            [2, 6, 16, 3, 3],
            {
                "enabled": True,
                "budget": 16,
                "in_use": 6,
                "remaining": 10,
                "requested_cost": 3,
                "can_admit": True,
                "capacity_kind": "shadow_clone",
                "acquired": True,
                "refreshed": True,
                "taken_over": False,
                "owner_conflict": False,
                "lease_ttl_seconds": 90,
            },
        ),
    ],
)
async def test_try_acquire_run_capacity_lease_covers_denied_and_refresh_states(
    monkeypatch,
    eval_result,
    expected,
):
    monkeypatch.setenv("AGENTSCOPE_SERVER_CONCURRENCY_BUDGET", "16")
    monkeypatch.setenv("AGENTSCOPE_SERVER_SHADOW_CLONE_RUN_COST", "3")

    async def _fake_eval_script(*_args, **_kwargs):
        return eval_result

    monkeypatch.setattr(run_capacity.redis, "eval_script", _fake_eval_script)

    result = await run_capacity.try_acquire_run_capacity_lease(
        agent_run_id="run-lease",
        mode="on",
        owner_token="worker-1",
    )

    assert result == expected


@pytest.mark.asyncio
async def test_try_acquire_run_capacity_lease_exposes_owner_conflict(monkeypatch):
    monkeypatch.setenv("AGENTSCOPE_SERVER_CONCURRENCY_BUDGET", "16")
    monkeypatch.setenv("AGENTSCOPE_SERVER_SHADOW_CLONE_RUN_COST", "3")

    async def _fake_eval_script(*_args, **_kwargs):
        return [3, 6, 16, 3, 3]

    monkeypatch.setattr(run_capacity.redis, "eval_script", _fake_eval_script)

    result = await run_capacity.try_acquire_run_capacity_lease(
        agent_run_id="run-owner-conflict",
        mode="on",
        owner_token="worker-2",
    )

    assert result == {
        "enabled": True,
        "budget": 16,
        "in_use": 6,
        "remaining": 10,
        "requested_cost": 3,
        "can_admit": True,
        "capacity_kind": "shadow_clone",
        "acquired": False,
        "refreshed": False,
        "taken_over": False,
        "owner_conflict": True,
        "lease_ttl_seconds": 90,
    }


@pytest.mark.asyncio
async def test_try_acquire_run_capacity_lease_marks_safe_takeover(monkeypatch):
    monkeypatch.setenv("AGENTSCOPE_SERVER_CONCURRENCY_BUDGET", "16")
    monkeypatch.setenv("AGENTSCOPE_SERVER_SHADOW_CLONE_RUN_COST", "3")

    async def _fake_eval_script(*_args, **_kwargs):
        return [4, 6, 16, 3, 3]

    monkeypatch.setattr(run_capacity.redis, "eval_script", _fake_eval_script)

    result = await run_capacity.try_acquire_run_capacity_lease(
        agent_run_id="run-owner-takeover",
        mode="on",
        owner_token="worker-2",
        allow_takeover=True,
    )

    assert result == {
        "enabled": True,
        "budget": 16,
        "in_use": 6,
        "remaining": 10,
        "requested_cost": 3,
        "can_admit": True,
        "capacity_kind": "shadow_clone",
        "acquired": True,
        "refreshed": False,
        "taken_over": True,
        "owner_conflict": False,
        "lease_ttl_seconds": 90,
    }


@pytest.mark.asyncio
async def test_try_acquire_run_capacity_lease_raises_for_blank_run_id(monkeypatch):
    monkeypatch.setenv("AGENTSCOPE_SERVER_CONCURRENCY_BUDGET", "16")

    with pytest.raises(ValueError):
        await run_capacity.try_acquire_run_capacity_lease(
            agent_run_id=" ",
            mode="off",
            owner_token="worker-1",
        )


@pytest.mark.asyncio
async def test_try_acquire_run_capacity_lease_short_circuits_when_budget_disabled(monkeypatch):
    monkeypatch.setenv("AGENTSCOPE_SERVER_CONCURRENCY_BUDGET", "0")
    monkeypatch.setenv("AGENTSCOPE_SERVER_REGULAR_RUN_COST", "1")

    async def _unexpected_eval_script(*_args, **_kwargs):
        raise AssertionError("eval_script should not be called when budget is disabled")

    monkeypatch.setattr(run_capacity.redis, "eval_script", _unexpected_eval_script)

    result = await run_capacity.try_acquire_run_capacity_lease(
        agent_run_id="run-no-budget",
        mode="off",
        owner_token="worker-1",
    )

    assert result == {
        "enabled": False,
        "budget": 0,
        "in_use": 0,
        "remaining": 0,
        "requested_cost": 1,
        "can_admit": False,
        "capacity_kind": "regular",
        "acquired": True,
        "refreshed": False,
        "taken_over": False,
        "owner_conflict": False,
        "lease_ttl_seconds": 0,
    }


@pytest.mark.asyncio
async def test_release_run_capacity_lease_parses_release_eval_result(monkeypatch):
    monkeypatch.setenv("AGENTSCOPE_SERVER_CONCURRENCY_BUDGET", "16")

    async def _fake_eval_script(*_args, **_kwargs):
        return [1, 4, 3]

    monkeypatch.setattr(run_capacity.redis, "eval_script", _fake_eval_script)

    result = await run_capacity.release_run_capacity_lease(
        agent_run_id="run-release",
        owner_token="worker-1",
    )

    assert result == {
        "enabled": True,
        "budget": 16,
        "in_use": 4,
        "remaining": 12,
        "released": True,
        "released_cost": 3,
        "owner_conflict": False,
    }


@pytest.mark.asyncio
async def test_release_run_capacity_lease_rejects_wrong_owner(monkeypatch):
    monkeypatch.setenv("AGENTSCOPE_SERVER_CONCURRENCY_BUDGET", "16")

    async def _fake_eval_script(*_args, **_kwargs):
        return [-1, 4, 3]

    monkeypatch.setattr(run_capacity.redis, "eval_script", _fake_eval_script)

    result = await run_capacity.release_run_capacity_lease(
        agent_run_id="run-release",
        owner_token="worker-2",
    )

    assert result == {
        "enabled": True,
        "budget": 16,
        "in_use": 4,
        "remaining": 12,
        "released": False,
        "released_cost": 0,
        "owner_conflict": True,
    }


@pytest.mark.asyncio
async def test_refresh_run_capacity_lease_reuses_acquire_logic(monkeypatch):
    monkeypatch.setenv("AGENTSCOPE_SERVER_CONCURRENCY_BUDGET", "16")
    monkeypatch.setenv("AGENTSCOPE_SERVER_SHADOW_CLONE_RUN_COST", "3")

    async def _fake_eval_script(*_args, **_kwargs):
        return [2, 9, 16, 3, 3]

    monkeypatch.setattr(run_capacity.redis, "eval_script", _fake_eval_script)

    result = await run_capacity.refresh_run_capacity_lease(
        agent_run_id="run-refresh",
        mode="on",
        owner_token="worker-1",
    )

    assert result == {
        "enabled": True,
        "budget": 16,
        "in_use": 9,
        "remaining": 7,
        "requested_cost": 3,
        "can_admit": True,
        "capacity_kind": "shadow_clone",
        "acquired": True,
        "refreshed": True,
        "taken_over": False,
        "owner_conflict": False,
        "lease_ttl_seconds": 90,
    }


@pytest.mark.asyncio
async def test_get_safe_run_capacity_snapshot_falls_back_on_eval_error(monkeypatch):
    monkeypatch.setenv("AGENTSCOPE_SERVER_CONCURRENCY_BUDGET", "12")

    async def _broken_eval_script(*_args, **_kwargs):
        raise RuntimeError("redis unavailable")

    monkeypatch.setattr(run_capacity.redis, "eval_script", _broken_eval_script)

    snapshot = await run_capacity.get_safe_run_capacity_snapshot(requested_cost=2)

    assert snapshot == {
        "enabled": True,
        "budget": 12,
        "in_use": 12,
        "remaining": 0,
        "requested_cost": 2,
        "can_admit": False,
        "unavailable": True,
    }


@pytest.mark.asyncio
async def test_get_run_capacity_snapshot_uses_mode_aware_budget(monkeypatch):
    monkeypatch.setenv("AGENTSCOPE_SERVER_CONCURRENCY_BUDGET", "16")
    monkeypatch.setenv("AGENTSCOPE_SERVER_REGULAR_ADMISSION_BUDGET", "12")

    async def _fake_eval_script(*_args, **_kwargs):
        return 9

    monkeypatch.setattr(run_capacity.redis, "eval_script", _fake_eval_script)

    regular_snapshot = await run_capacity.get_run_capacity_snapshot(
        mode="off",
        requested_cost=1,
    )
    shadow_snapshot = await run_capacity.get_run_capacity_snapshot(
        mode="on",
        requested_cost=2,
    )

    assert regular_snapshot == {
        "enabled": True,
        "budget": 12,
        "in_use": 9,
        "remaining": 3,
        "requested_cost": 1,
        "can_admit": True,
    }
    assert shadow_snapshot == {
        "enabled": True,
        "budget": 16,
        "in_use": 9,
        "remaining": 7,
        "requested_cost": 2,
        "can_admit": True,
    }


@pytest.mark.asyncio
async def test_run_capacity_sequence_handles_mixed_regular_and_shadow_clone_load(monkeypatch):
    monkeypatch.setenv("AGENTSCOPE_SERVER_CONCURRENCY_BUDGET", "6")
    monkeypatch.setenv("AGENTSCOPE_SERVER_REGULAR_RUN_COST", "1")
    monkeypatch.setenv("AGENTSCOPE_SERVER_SHADOW_CLONE_RUN_COST", "3")

    leases: dict[str, dict[str, object]] = {}

    async def _stateful_eval(script, *, keys, args):
        assert keys == [
            "server_run_capacity:leases",
            "server_run_capacity:owners",
            "server_run_capacity:expiries",
        ]
        if script == run_capacity._PRUNE_EXPIRED_LEASES_LUA:
            return sum(int(payload["cost"]) for payload in leases.values())
        if script == run_capacity._ACQUIRE_RUN_CAPACITY_LUA:
            budget = int(args[0])
            run_id = args[1]
            owner_token = args[2]
            requested_cost = int(args[3])
            allow_takeover = args[6] == "1"
            existing = leases.get(run_id)
            in_use = sum(int(payload["cost"]) for payload in leases.values())
            existing_cost = int(existing["cost"]) if existing else 0
            if existing and existing["owner"] != owner_token and not allow_takeover:
                return [3, in_use, budget, requested_cost, existing_cost]
            next_total = in_use + requested_cost
            if existing:
                next_total = in_use - existing_cost + requested_cost
            if next_total > budget:
                return [0, in_use, budget, requested_cost, existing_cost]
            if existing:
                status_code = 4 if existing["owner"] != owner_token else 2
            else:
                status_code = 1
            leases[run_id] = {"cost": requested_cost, "owner": owner_token}
            return [
                status_code,
                sum(int(payload["cost"]) for payload in leases.values()),
                budget,
                requested_cost,
                existing_cost,
            ]
        if script == run_capacity._RELEASE_RUN_CAPACITY_LUA:
            run_id = args[0]
            owner_token = args[1]
            existing = leases.get(run_id)
            in_use = sum(int(payload["cost"]) for payload in leases.values())
            if not existing:
                return [0, in_use, 0]
            if existing["owner"] != owner_token:
                return [-1, in_use, int(existing["cost"])]
            released_cost = int(existing["cost"])
            del leases[run_id]
            return [1, sum(int(payload["cost"]) for payload in leases.values()), released_cost]
        raise AssertionError(f"Unexpected script: {script[:40]}")

    monkeypatch.setattr(run_capacity.redis, "eval_script", _stateful_eval)

    regular = await run_capacity.try_acquire_run_capacity_lease(
        agent_run_id="run-regular",
        mode="off",
        owner_token="worker-regular",
    )
    first_shadow = await run_capacity.try_acquire_run_capacity_lease(
        agent_run_id="run-shadow-1",
        mode="on",
        owner_token="worker-shadow-1",
    )
    denied_shadow = await run_capacity.try_acquire_run_capacity_lease(
        agent_run_id="run-shadow-2",
        mode="on",
        owner_token="worker-shadow-2",
    )
    released_regular = await run_capacity.release_run_capacity_lease(
        agent_run_id="run-regular",
        owner_token="worker-regular",
    )
    admitted_shadow = await run_capacity.try_acquire_run_capacity_lease(
        agent_run_id="run-shadow-2",
        mode="on",
        owner_token="worker-shadow-2",
    )

    assert regular["acquired"] is True
    assert regular["remaining"] == 5
    assert first_shadow["acquired"] is True
    assert first_shadow["remaining"] == 2
    assert denied_shadow["acquired"] is False
    assert denied_shadow["remaining"] == 2
    assert denied_shadow["requested_cost"] == 3
    assert released_regular == {
        "enabled": True,
        "budget": 6,
        "in_use": 3,
        "remaining": 3,
        "released": True,
        "released_cost": 1,
        "owner_conflict": False,
    }
    assert admitted_shadow["acquired"] is True
    assert admitted_shadow["remaining"] == 0


@pytest.mark.asyncio
async def test_run_capacity_sequence_preserves_shadow_clone_headroom_with_regular_budget_cap(
    monkeypatch,
):
    monkeypatch.setenv("AGENTSCOPE_SERVER_CONCURRENCY_BUDGET", "16")
    monkeypatch.setenv("AGENTSCOPE_SERVER_REGULAR_ADMISSION_BUDGET", "12")
    monkeypatch.setenv("AGENTSCOPE_SERVER_REGULAR_RUN_COST", "1")
    monkeypatch.setenv("AGENTSCOPE_SERVER_SHADOW_CLONE_RUN_COST", "2")

    leases: dict[str, dict[str, object]] = {}

    async def _stateful_eval(script, *, keys, args):
        assert keys == [
            "server_run_capacity:leases",
            "server_run_capacity:owners",
            "server_run_capacity:expiries",
        ]
        if script == run_capacity._PRUNE_EXPIRED_LEASES_LUA:
            return sum(int(payload["cost"]) for payload in leases.values())
        if script == run_capacity._ACQUIRE_RUN_CAPACITY_LUA:
            budget = int(args[0])
            run_id = args[1]
            owner_token = args[2]
            requested_cost = int(args[3])
            allow_takeover = args[6] == "1"
            existing = leases.get(run_id)
            in_use = sum(int(payload["cost"]) for payload in leases.values())
            existing_cost = int(existing["cost"]) if existing else 0
            if existing and existing["owner"] != owner_token and not allow_takeover:
                return [3, in_use, budget, requested_cost, existing_cost]
            next_total = in_use + requested_cost
            if existing:
                next_total = in_use - existing_cost + requested_cost
            if next_total > budget:
                return [0, in_use, budget, requested_cost, existing_cost]
            if existing:
                status_code = 4 if existing["owner"] != owner_token else 2
            else:
                status_code = 1
            leases[run_id] = {"cost": requested_cost, "owner": owner_token}
            return [
                status_code,
                sum(int(payload["cost"]) for payload in leases.values()),
                budget,
                requested_cost,
                existing_cost,
            ]
        if script == run_capacity._RELEASE_RUN_CAPACITY_LUA:
            run_id = args[0]
            owner_token = args[1]
            existing = leases.get(run_id)
            in_use = sum(int(payload["cost"]) for payload in leases.values())
            if not existing:
                return [0, in_use, 0]
            if existing["owner"] != owner_token:
                return [-1, in_use, int(existing["cost"])]
            released_cost = int(existing["cost"])
            del leases[run_id]
            return [1, sum(int(payload["cost"]) for payload in leases.values()), released_cost]
        raise AssertionError(f"Unexpected script: {script[:40]}")

    monkeypatch.setattr(run_capacity.redis, "eval_script", _stateful_eval)

    regular_results = []
    for index in range(12):
        regular_results.append(
            await run_capacity.try_acquire_run_capacity_lease(
                agent_run_id=f"run-regular-{index}",
                mode="off",
                owner_token=f"worker-regular-{index}",
            )
        )
    denied_regular = await run_capacity.try_acquire_run_capacity_lease(
        agent_run_id="run-regular-denied",
        mode="off",
        owner_token="worker-regular-denied",
    )
    first_shadow = await run_capacity.try_acquire_run_capacity_lease(
        agent_run_id="run-shadow-1",
        mode="on",
        owner_token="worker-shadow-1",
    )
    second_shadow = await run_capacity.try_acquire_run_capacity_lease(
        agent_run_id="run-shadow-2",
        mode="on",
        owner_token="worker-shadow-2",
    )
    denied_shadow = await run_capacity.try_acquire_run_capacity_lease(
        agent_run_id="run-shadow-3",
        mode="on",
        owner_token="worker-shadow-3",
    )

    assert all(result["acquired"] is True for result in regular_results)
    assert regular_results[-1]["remaining"] == 0
    assert denied_regular["acquired"] is False
    assert denied_regular["budget"] == 12
    assert first_shadow["acquired"] is True
    assert first_shadow["remaining"] == 2
    assert second_shadow["acquired"] is True
    assert second_shadow["remaining"] == 0
    assert denied_shadow["acquired"] is False
    assert denied_shadow["budget"] == 16
    assert denied_shadow["requested_cost"] == 2


def test_build_run_capacity_limit_detail_uses_shadow_clone_limit_code():
    detail = run_capacity.build_run_capacity_limit_detail(
        mode="on",
        snapshot={
            "budget": 16,
            "in_use": 15,
            "remaining": 1,
            "requested_cost": 3,
        },
    )

    assert detail == {
        "code": "shadow_clone_limit",
        "message": "Server concurrency budget is exhausted. Please retry shortly.",
        "capacity_kind": "shadow_clone",
        "budget": 16,
        "in_use": 15,
        "remaining": 1,
        "requested_cost": 3,
    }
