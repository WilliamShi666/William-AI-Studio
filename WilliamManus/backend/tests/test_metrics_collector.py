from __future__ import annotations

from services.metrics_collector import (
    aggregate_generation_metrics,
    aggregate_tool_metrics,
    compute_phase1_score_bundle,
    compute_behavior_score,
    compute_o_score,
    compute_phase1_ratio_metrics,
)


def test_aggregate_generation_metrics_sums_tokens_and_costs() -> None:
    generations = [
        {"model": "openai/gpt-4o-mini", "input_tokens": 1000, "output_tokens": 500},
        {"model": "openai/gpt-4o-mini", "input_tokens": 2000, "output_tokens": 300},
        {"model": "openai/gpt-4.1", "input_tokens": 100, "output_tokens": 50},
    ]

    result = aggregate_generation_metrics(generations)

    assert result["total_tokens"] == 3950
    assert result["input_tokens"] == 3100
    assert result["output_tokens"] == 850
    assert result["estimated_cost_usd"] > 0


def test_aggregate_tool_metrics_counts_iterations_and_code_exec_success_rate() -> None:
    tool_events = [
        {"tool_name": "execute_command", "success": True},
        {"tool_name": "execute_command", "success": False},
        {"tool_name": "read_file", "success": True},
        {"tool_name": "write_file", "success": True},
    ]

    result = aggregate_tool_metrics(tool_events)

    assert result["iteration_count"] == 4
    assert result["code_exec_attempts"] == 2
    assert result["code_exec_successes"] == 1
    assert result["code_exec_success_rate"] == 0.5


def test_compute_o_score_applies_penalties_and_floor() -> None:
    result = compute_o_score(
        ttft_seconds=6.1,
        e2e_duration_seconds=360,
        estimated_cost_usd=0.75,
        iteration_count=80,
        total_tokens=700000,
    )

    assert result == 25


def test_compute_o_score_zero_penalties_returns_hundred() -> None:
    result = compute_o_score(
        ttft_seconds=2.0,
        e2e_duration_seconds=120,
        estimated_cost_usd=0.10,
        iteration_count=12,
        total_tokens=12000,
    )

    assert result == 100


def test_compute_behavior_score_handles_normal_case() -> None:
    result = compute_behavior_score(
        first_try_success_rate=0.5,
        waste_ratio=0.2,
        convergence_speed=0.9,
    )

    assert result == 70.5


def test_compute_behavior_score_clamps_and_handles_zero_edges() -> None:
    result = compute_behavior_score(
        first_try_success_rate=-1.0,
        waste_ratio=2.0,
        convergence_speed=None,
    )

    assert result == 0.0


def test_compute_phase1_ratio_metrics_uses_proxy_quality_bundle() -> None:
    result = compute_phase1_ratio_metrics(
        estimated_cost_usd=0.2,
        total_tokens=4000,
        iteration_count=8,
        task_completed=1.0,
        deliverable_quality=4.0,
    )

    assert result == {
        "quality_per_dollar": 20.0,
        "quality_per_ktoken": 1.0,
        "quality_per_iteration": 0.5,
        "q_like_score": 4.0,
    }


def test_compute_phase1_ratio_metrics_returns_none_without_quality_proxy() -> None:
    result = compute_phase1_ratio_metrics(
        estimated_cost_usd=0.2,
        total_tokens=4000,
        iteration_count=8,
        task_completed=None,
        deliverable_quality=None,
    )

    assert result is None


def test_compute_phase1_score_bundle_collects_phase1_runtime_scores() -> None:
    result = compute_phase1_score_bundle(
        generations=[
            {"model": "openai/gpt-4o-mini", "input_tokens": 1000, "output_tokens": 500},
            {"model": "openai/gpt-4o-mini", "input_tokens": 250, "output_tokens": 100},
        ],
        tool_events=[
            {"tool_name": "execute_command", "success": True},
            {"tool_name": "execute_command", "success": False},
            {"tool_name": "read_file", "success": True},
        ],
        safety_events=[
            {
                "tool_name": "execute_command",
                "safety": {"is_violation": True, "reason_code": "DANGEROUS_RM_RF"},
            },
        ],
        e2e_duration_seconds=42.5,
        final_status="completed",
        ttft_seconds=1.25,
    )

    assert result["total_tokens"] == 1850
    assert result["iteration_count"] == 3
    assert result["code_exec_success_rate"] == 0.5
    assert result["task_completed"] == 1.0
    assert result["tool_authorization_violation"] == 1
    assert result["e2e_duration_seconds"] == 42.5
    assert result["ttft_seconds"] == 1.25
    assert result["o_score"] == 100
