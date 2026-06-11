from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Awaitable, Callable


async def admit_queued_regular_run(
    *,
    client: Any,
    thread_id: str,
    project_id: str,
    agent_run_id: str,
    agent_config_snapshot: dict[str, Any] | None,
    agent_run_metadata: dict[str, Any],
    execution_mode: str,
    ensure_phase2_attempt_queue_capacity: Callable[[Any], Awaitable[None]],
    create_initial_attempt: Callable[..., Awaitable[None]],
    dispatch_queue_handoff: Callable[..., None],
    mark_phase2_admission_attempt_failed: Callable[..., Awaitable[None]],
    update_agent_run_status: Callable[..., Awaitable[Any]],
) -> dict[str, str]:
    del project_id

    metadata_payload = dict(agent_run_metadata)
    metadata_payload["regular_execution_mode"] = execution_mode

    await ensure_phase2_attempt_queue_capacity(client)

    inserted_run = await (
        client.schema("public")
        .table("agent_runs")
        .insert(
            {
                "thread_id": thread_id,
                "agent_run_id": agent_run_id,
                "status": "queued",
                "started_at": datetime.now(),
                "agent_id": (agent_config_snapshot or {}).get("agent_id"),
                "agent_version_id": (
                    (agent_config_snapshot or {}).get("current_version_id")
                ),
                "metadata": json.dumps(metadata_payload),
            }
        )
    )
    resolved_agent_run_id = str(
        inserted_run.data[0].get("agent_run_id") or inserted_run.data[0]["id"]
    )

    phase2_attempt_created = False
    try:
        await create_initial_attempt(
            client,
            agent_run_id=resolved_agent_run_id,
        )
        phase2_attempt_created = True
        dispatch_queue_handoff(execution_mode=execution_mode)
    except Exception as dispatch_error:
        error_message = f"Failed to dispatch agent run: {dispatch_error}"
        if phase2_attempt_created:
            await mark_phase2_admission_attempt_failed(
                client,
                agent_run_id=resolved_agent_run_id,
                error_message=error_message,
            )
        await update_agent_run_status(
            client,
            resolved_agent_run_id,
            "failed",
            error=error_message,
        )
        raise

    return {"agent_run_id": resolved_agent_run_id, "status": "queued"}
