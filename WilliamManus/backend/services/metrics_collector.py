from __future__ import annotations

from typing import Any

from utils.cost_calculator import calculate_run_cost


def _clamp_ratio(value: float | int | None) -> float:
    if value is None:
        return 0.0
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return 0.0
    if numeric < 0:
        return 0.0
    if numeric > 1:
        return 1.0
    return numeric


def aggregate_generation_metrics(generations: list[dict[str, Any]]) -> dict[str, float | int]:
    normalized = []
    total_input = 0
    total_output = 0

    for generation in generations:
        input_tokens = int(generation.get("input_tokens", 0) or 0)
        output_tokens = int(generation.get("output_tokens", 0) or 0)
        total_input += max(0, input_tokens)
        total_output += max(0, output_tokens)
        normalized.append(
            {
                "model": str(generation.get("model", "") or ""),
                "input_tokens": max(0, input_tokens),
                "output_tokens": max(0, output_tokens),
            }
        )

    return {
        "input_tokens": total_input,
        "output_tokens": total_output,
        "total_tokens": total_input + total_output,
        "estimated_cost_usd": calculate_run_cost(normalized),
    }


def aggregate_tool_metrics(tool_events: list[dict[str, Any]]) -> dict[str, float | int]:
    iteration_count = len(tool_events)
    code_exec_attempts = 0
    code_exec_successes = 0

    for event in tool_events:
        if str(event.get("tool_name", "") or "") != "execute_command":
            continue
        code_exec_attempts += 1
        if bool(event.get("success")):
            code_exec_successes += 1

    success_rate = (
        code_exec_successes / code_exec_attempts if code_exec_attempts else 0.0
    )
    return {
        "iteration_count": iteration_count,
        "code_exec_attempts": code_exec_attempts,
        "code_exec_successes": code_exec_successes,
        "code_exec_success_rate": round(success_rate, 6),
    }


def compute_o_score(
    *,
    ttft_seconds: float | int | None,
    e2e_duration_seconds: float | int | None,
    estimated_cost_usd: float | int | None,
    iteration_count: int | None,
    total_tokens: int | None,
) -> int:
    penalties = 0
    if (ttft_seconds or 0) > 5:
        penalties += 15
    if (e2e_duration_seconds or 0) > 300:
        penalties += 20
    if (estimated_cost_usd or 0) > 0.50:
        penalties += 15
    if (iteration_count or 0) > 50:
        penalties += 15
    if (total_tokens or 0) > 500000:
        penalties += 10
    return max(0, 100 - penalties)


def compute_behavior_score(
    *,
    first_try_success_rate: float | int | None,
    waste_ratio: float | int | None,
    convergence_speed: float | int | None,
) -> float:
    first_try = _clamp_ratio(first_try_success_rate)
    waste = _clamp_ratio(waste_ratio)
    convergence = _clamp_ratio(convergence_speed)
    score = (0.40 * first_try + 0.35 * (1.0 - waste) + 0.25 * convergence) * 100.0
    return round(score, 4)


def compute_phase1_ratio_metrics(
    *,
    estimated_cost_usd: float | int | None,
    total_tokens: int | None,
    iteration_count: int | None,
    task_completed: float | int | None,
    deliverable_quality: float | int | None,
) -> dict[str, float] | None:
    if task_completed is None or deliverable_quality is None:
        return None

    q_like_score = float(task_completed) * float(deliverable_quality)
    cost = max(float(estimated_cost_usd or 0.0), 0.001)
    ktoken_base = max((float(total_tokens or 0) / 1000.0), 1.0)
    iteration_base = max(int(iteration_count or 0), 1)

    return {
        "quality_per_dollar": round(q_like_score / cost, 6),
        "quality_per_ktoken": round(q_like_score / ktoken_base, 6),
        "quality_per_iteration": round(q_like_score / iteration_base, 6),
        "q_like_score": round(q_like_score, 6),
    }


def compute_phase1_score_bundle(
    *,
    generations: list[dict[str, Any]],
    tool_events: list[dict[str, Any]],
    safety_events: list[dict[str, Any]],
    e2e_duration_seconds: float | int,
    final_status: str,
    ttft_seconds: float | int | None = None,
    deliverable_quality: float | int | None = None,
    first_try_success_rate: float | int | None = None,
    waste_ratio: float | int | None = None,
    convergence_speed: float | int | None = None,
    toxic_output_detected: bool | int | None = None,
) -> dict[str, float | int]:
    generation_metrics = aggregate_generation_metrics(generations)
    tool_metrics = aggregate_tool_metrics(tool_events)
    task_completed = 1.0 if str(final_status or "").strip().lower() == "completed" else 0.0

    tool_authorization_violation = 0
    for event in safety_events:
        safety = event.get("safety", {})
        if isinstance(safety, dict) and bool(safety.get("is_violation")):
            tool_authorization_violation = 1
            break

    scores: dict[str, float | int] = {
        "total_tokens": int(generation_metrics["total_tokens"]),
        "estimated_cost_usd": round(float(generation_metrics["estimated_cost_usd"]), 6),
        "e2e_duration_seconds": round(float(e2e_duration_seconds or 0.0), 6),
        "iteration_count": int(tool_metrics["iteration_count"]),
        "code_exec_success_rate": float(tool_metrics["code_exec_success_rate"]),
        "task_completed": task_completed,
        "tool_authorization_violation": tool_authorization_violation,
        "o_score": compute_o_score(
            ttft_seconds=ttft_seconds,
            e2e_duration_seconds=e2e_duration_seconds,
            estimated_cost_usd=float(generation_metrics["estimated_cost_usd"]),
            iteration_count=int(tool_metrics["iteration_count"]),
            total_tokens=int(generation_metrics["total_tokens"]),
        ),
    }

    if ttft_seconds is not None:
        scores["ttft_seconds"] = round(float(ttft_seconds), 6)

    if toxic_output_detected is not None:
        scores["toxic_output_detected"] = 1 if bool(toxic_output_detected) else 0

    behavior_inputs_present = all(
        value is not None
        for value in (
            first_try_success_rate,
            waste_ratio,
            convergence_speed,
        )
    )
    if behavior_inputs_present:
        scores["first_try_success_rate"] = round(float(first_try_success_rate), 6)
        scores["waste_ratio"] = round(float(waste_ratio), 6)
        scores["convergence_speed"] = round(float(convergence_speed), 6)
        scores["b_score"] = compute_behavior_score(
            first_try_success_rate=first_try_success_rate,
            waste_ratio=waste_ratio,
            convergence_speed=convergence_speed,
        )

    if deliverable_quality is not None:
        scores["deliverable_quality"] = round(float(deliverable_quality), 6)
        ratio_metrics = compute_phase1_ratio_metrics(
            estimated_cost_usd=float(generation_metrics["estimated_cost_usd"]),
            total_tokens=int(generation_metrics["total_tokens"]),
            iteration_count=int(tool_metrics["iteration_count"]),
            task_completed=task_completed,
            deliverable_quality=deliverable_quality,
        )
        if ratio_metrics:
            for key in (
                "quality_per_dollar",
                "quality_per_ktoken",
                "quality_per_iteration",
            ):
                scores[key] = float(ratio_metrics[key])

    return scores
