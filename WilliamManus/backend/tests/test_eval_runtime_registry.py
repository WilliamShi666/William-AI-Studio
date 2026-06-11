from __future__ import annotations

from services.eval_runtime_registry import (
    pop_eval_run,
    record_model_generation,
    record_safety_event,
    record_tool_event,
    record_ttft_seconds,
    start_eval_run,
    update_eval_run_metadata,
)


def test_eval_runtime_registry_tracks_and_pops_run_state() -> None:
    start_eval_run(
        run_id="run-1",
        trace_id="trace-1",
        metadata={"project_id": "project-1"},
    )
    update_eval_run_metadata("run-1", model_name="openrouter/gpt", resume_strategy="auto")
    record_model_generation(
        "run-1",
        model="openrouter/gpt",
        input_tokens=100,
        output_tokens=50,
    )
    record_tool_event("run-1", tool_name="execute_command", success=True)
    record_safety_event(
        "run-1",
        tool_name="execute_command",
        safety={"is_violation": True, "reason_code": "DANGEROUS_RM_RF"},
    )
    record_ttft_seconds("run-1", 2.5)
    record_ttft_seconds("run-1", 9.9)

    record = pop_eval_run("run-1")

    assert record is not None
    assert record.trace_id == "trace-1"
    assert record.metadata["project_id"] == "project-1"
    assert record.metadata["model_name"] == "openrouter/gpt"
    assert record.metadata["resume_strategy"] == "auto"
    assert record.model_generations == [
        {
            "model": "openrouter/gpt",
            "input_tokens": 100,
            "output_tokens": 50,
        },
    ]
    assert record.tool_events == [
        {
            "tool_name": "execute_command",
            "success": True,
        },
    ]
    assert record.safety_events == [
        {
            "tool_name": "execute_command",
            "safety": {
                "is_violation": True,
                "reason_code": "DANGEROUS_RM_RF",
            },
        },
    ]
    assert record.ttft_seconds == 2.5
    assert pop_eval_run("run-1") is None
