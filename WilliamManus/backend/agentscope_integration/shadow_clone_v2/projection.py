"""Pure event-log projection for Shadow Clone V2 public state."""

from __future__ import annotations

from typing import Any


def _event_type(event: Any) -> str:
    raw = getattr(event, "type", None)
    value = getattr(raw, "value", raw)
    return str(value or "").strip()


def _payload(event: Any) -> dict[str, Any]:
    payload = getattr(event, "payload", None)
    return payload if isinstance(payload, dict) else {}


def _created_at(event: Any) -> str | None:
    value = getattr(event, "created_at", None)
    return str(value).strip() if value is not None and str(value).strip() else None


def _sequence(event: Any) -> int:
    value = getattr(event, "sequence", None)
    if isinstance(value, int):
        return value
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _event_id(event: Any) -> str:
    return str(getattr(event, "id", "") or "")


def _ordered_events(events: list[Any]) -> list[Any]:
    return sorted(
        enumerate(events),
        key=lambda item: (_sequence(item[1]), _event_id(item[1]), item[0]),
    )


def _status_value(value: Any, default: str | None = None) -> str | None:
    raw = getattr(value, "value", value)
    text = str(raw or "").strip()
    return text or default


def _task_public_status(status: str | None) -> str:
    normalized = str(status or "").strip().lower()
    if normalized == "in_progress":
        return "running"
    return normalized or "pending"


def _terminal_result_visibility_summary(status: str) -> str:
    return "Task completed." if status == "completed" else "Task failed."


def _transcript_content_from_payload(payload: dict[str, Any]) -> str:
    for key in ("content", "output", "result_summary", "result_ref"):
        value = payload.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ""


def _ensure_agent(
    agents: dict[str, dict[str, Any]],
    agent_name: str,
    *,
    role: str | None = None,
) -> dict[str, Any]:
    current = agents.setdefault(agent_name, {"agent_name": agent_name})
    if role and not current.get("role"):
        current["role"] = role
    return current


def _task_agent_name(task: dict[str, Any]) -> str:
    metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
    return str(task.get("agent_name") or metadata.get("agent_name") or "").strip()


def _apply_task_snapshot(
    tasks: dict[str, dict[str, Any]],
    snapshot: dict[str, Any],
    *,
    event_created_at: str | None,
) -> dict[str, Any] | None:
    task_id = str(snapshot.get("id") or snapshot.get("task_id") or "").strip()
    if not task_id:
        return None
    metadata = (
        snapshot.get("metadata") if isinstance(snapshot.get("metadata"), dict) else {}
    )
    task = tasks.setdefault(task_id, {"id": task_id})
    for source_key, target_key in (
        ("subject", "subject"),
        ("description", "description"),
        ("status", "status"),
        ("priority", "priority"),
        ("owner_agent", "owner_agent"),
        ("lease_expires_at", "lease_expires_at"),
        ("attempt", "attempt"),
        ("max_attempts", "max_attempts"),
        ("created_by", "created_by"),
        ("result_ref", "result_ref"),
    ):
        if source_key in snapshot:
            task[target_key] = snapshot.get(source_key)
    task["role"] = task.get("subject")
    task["metadata"] = dict(metadata)
    task["agent_name"] = (
        str(snapshot.get("agent_name") or metadata.get("agent_name") or "").strip()
        or None
    )
    task["blocked_by"] = list(snapshot.get("blocked_by") or [])
    task["blocks"] = list(snapshot.get("blocks") or [])
    task["version"] = (
        snapshot.get("version") or snapshot.get("plan_revision") or task.get("version")
    )
    if "error" in snapshot:
        task["error"] = (
            snapshot.get("error") if isinstance(snapshot.get("error"), dict) else {}
        )
        if task["error"].get("error_type"):
            task["failure_class"] = task["error"].get("error_type")
        if task["error"].get("message"):
            task["last_error"] = task["error"].get("message")
    task["created_at"] = (
        snapshot.get("created_at") or task.get("created_at") or event_created_at
    )
    task["updated_at"] = snapshot.get("updated_at") or event_created_at
    return task


def _legacy_subagent_from_task(
    task: dict[str, Any],
    agents: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    agent_name = _task_agent_name(task)
    agent = agents.get(agent_name) or {}
    public = {
        "status": _task_public_status(task.get("status")),
        "role": task.get("role") or task.get("subject") or agent.get("role"),
        "task_description": task.get("description"),
        "created_at": task.get("created_at"),
    }
    for source_key, target_key in (
        ("claimed_at", "claimed_at"),
        ("lease_expires_at", "lease_expires_at"),
        ("result_ref", "result_summary"),
        ("finished_at", "finished_at"),
        ("last_error", "last_error"),
        ("failure_class", "failure_class"),
    ):
        if task.get(source_key) is not None:
            public[target_key] = task.get(source_key)
    for source_key in (
        "agent_status",
        "idle_since",
        "idle_expires_at",
        "wake_reason",
        "wake_message_id",
        "shutdown_reason",
        "shutdown_at",
    ):
        if agent.get(source_key) is not None:
            public[source_key] = agent.get(source_key)
    return public


def project_shadow_clone_v2_public_state(
    *,
    agent_run_id: str,
    events: list[Any],
    parent_status: Any,
) -> dict[str, Any] | None:
    """Project ordered or unordered V2 events into public status state."""
    if not events:
        return None

    team: dict[str, Any] = {"members": {}}
    agents: dict[str, dict[str, Any]] = {}
    tasks: dict[str, dict[str, Any]] = {}
    messages: dict[str, dict[str, Any]] = {}
    transcripts: list[dict[str, Any]] = []
    tool_calls: dict[str, dict[str, Any]] = {}
    team_shutdown: dict[str, Any] | None = None
    run_status: str | None = None
    terminal_reason: str | None = None
    model: dict[str, Any] = {}
    final_output: dict[str, Any] | None = None
    updated_at: str | None = None

    for _index, event in _ordered_events(events):
        event_type = _event_type(event)
        payload = _payload(event)
        event_created_at = _created_at(event)
        updated_at = event_created_at or updated_at

        if event_type == "run_started":
            run_status = run_status or "running"
            requested_model = (
                payload.get("requested_model")
                or payload.get("model_key")
                or payload.get("model")
            )
            effective_model = (
                payload.get("effective_model")
                or payload.get("model_key")
                or payload.get("model")
            )
            if requested_model:
                model["requested"] = str(requested_model)
            if effective_model:
                model["effective"] = str(effective_model)
            continue

        if event_type == "team_created":
            team["team_id"] = str(payload.get("team_id") or "").strip() or None
            team["facilitator_id"] = (
                str(payload.get("facilitator_id") or "").strip() or None
            )
            team["account_id"] = str(payload.get("account_id") or "").strip() or None
            team["created_at"] = event_created_at
            for member in payload.get("members") or []:
                if not isinstance(member, dict):
                    continue
                agent_name = str(member.get("agent_name") or "").strip()
                if not agent_name:
                    continue
                agent = _ensure_agent(
                    agents,
                    agent_name,
                    role=str(member.get("role") or "").strip() or None,
                )
                role = str(member.get("role") or "").strip()
                agent.update(
                    {
                        "agent_id": member.get("agent_id"),
                        "account_id": member.get("account_id"),
                        **({"role": role} if role else {}),
                        "status": str(member.get("status") or "starting").strip()
                        or "starting",
                        "current_run_id": member.get("current_run_id"),
                        "idle_since": member.get("idle_since"),
                        "idle_expires_at": member.get("idle_expires_at"),
                        "last_handoff_summary": member.get("last_handoff_summary"),
                        "metadata": (
                            dict(member.get("metadata"))
                            if isinstance(member.get("metadata"), dict)
                            else {}
                        ),
                        "created_at": event_created_at,
                    }
                )
                team["members"][agent_name] = agent
            continue

        if event_type == "team_updated":
            if payload.get("metadata") is not None:
                team["metadata"] = payload.get("metadata")
            team["updated_at"] = event_created_at
            continue

        if event_type == "agent_spawned":
            agent_name = str(payload.get("agent_name") or "").strip()
            if not agent_name:
                continue
            agent = _ensure_agent(
                agents,
                agent_name,
                role=str(payload.get("role") or "").strip() or None,
            )
            role = str(payload.get("role") or "").strip()
            agent.update(
                {
                    "agent_id": payload.get("agent_id") or agent.get("agent_id"),
                    "account_id": payload.get("account_id") or agent.get("account_id"),
                    **({"role": role} if role else {}),
                    "status": str(payload.get("status") or "running").strip()
                    or "running",
                    "created_at": event_created_at,
                }
            )
            for stale_key in (
                "agent_status",
                "shutdown_reason",
                "shutdown_at",
                "failure_class",
                "idle_since",
                "idle_expires_at",
                "wake_reason",
                "wake_message_id",
                "wake_task_id",
                "previous_status",
                "previous_run_id",
                "previous_idle_since",
                "previous_idle_expires_at",
            ):
                agent.pop(stale_key, None)
            team.setdefault("members", {})[agent_name] = agent
            continue

        if event_type == "task_created":
            snapshot = (
                payload.get("task") if isinstance(payload.get("task"), dict) else None
            )
            if snapshot is not None:
                task = _apply_task_snapshot(
                    tasks, snapshot, event_created_at=event_created_at
                )
                if task is None:
                    continue
                task_id = task["id"]
                agent_name = _task_agent_name(task)
                if agent_name:
                    agent = _ensure_agent(agents, agent_name, role=task.get("role"))
                    current_task_ids = list(agent.get("current_task_ids") or [])
                    if task_id not in current_task_ids:
                        current_task_ids.append(task_id)
                    agent["current_task_ids"] = current_task_ids
                continue
            task_id = str(payload.get("id") or payload.get("task_id") or "").strip()
            if not task_id:
                continue
            metadata = (
                payload.get("metadata")
                if isinstance(payload.get("metadata"), dict)
                else {}
            )
            agent_name = str(
                metadata.get("agent_name") or payload.get("agent_name") or ""
            ).strip()
            task = tasks.setdefault(task_id, {"id": task_id})
            task.update(
                {
                    "id": task_id,
                    "subject": str(payload.get("subject") or "").strip() or None,
                    "role": str(payload.get("subject") or "").strip() or None,
                    "description": str(payload.get("description") or "").strip()
                    or None,
                    "status": _status_value(payload.get("status"), "pending"),
                    "metadata": metadata,
                    "agent_name": agent_name or None,
                    "blocked_by": list(payload.get("blocked_by") or []),
                    "blocks": list(payload.get("blocks") or []),
                    "version": int(
                        payload.get("plan_revision") or payload.get("version") or 1
                    ),
                    "created_at": event_created_at,
                    "updated_at": event_created_at,
                }
            )
            if agent_name:
                agent = _ensure_agent(agents, agent_name, role=task.get("role"))
                current_task_ids = list(agent.get("current_task_ids") or [])
                if task_id not in current_task_ids:
                    current_task_ids.append(task_id)
                agent["current_task_ids"] = current_task_ids
            continue

        if event_type == "task_claimed":
            task_id = str(payload.get("task_id") or "").strip()
            if not task_id:
                continue
            task = tasks.setdefault(task_id, {"id": task_id})
            agent_name = str(
                payload.get("agent_name") or task.get("agent_name") or ""
            ).strip()
            task.update(
                {
                    "status": "in_progress",
                    "agent_name": agent_name or task.get("agent_name"),
                    "owner_agent": agent_name or task.get("owner_agent"),
                    "lease_expires_at": str(
                        payload.get("lease_expires_at") or ""
                    ).strip()
                    or None,
                    "attempt": payload.get("attempt"),
                    "version": payload.get("new_version") or task.get("version"),
                    "claimed_at": event_created_at,
                    "updated_at": event_created_at,
                }
            )
            if agent_name:
                _ensure_agent(agents, agent_name)["status"] = "working"
            continue

        if event_type == "task_updated":
            task_id = str(payload.get("task_id") or payload.get("id") or "").strip()
            if not task_id:
                continue
            snapshot = (
                payload.get("task") if isinstance(payload.get("task"), dict) else None
            )
            if snapshot is not None:
                _apply_task_snapshot(tasks, snapshot, event_created_at=event_created_at)
                for affected in payload.get("affected_tasks") or []:
                    if isinstance(affected, dict):
                        _apply_task_snapshot(
                            tasks, affected, event_created_at=event_created_at
                        )
                continue
            task = tasks.setdefault(task_id, {"id": task_id})
            if "new_status" in payload and "status" not in payload:
                task["status"] = payload.get("new_status")
            for field in (
                "subject",
                "description",
                "status",
                "owner_agent",
                "lease_expires_at",
            ):
                if field in payload:
                    task[field] = payload.get(field)
            if "metadata" in payload and isinstance(payload.get("metadata"), dict):
                existing_metadata = (
                    task.get("metadata")
                    if isinstance(task.get("metadata"), dict)
                    else {}
                )
                task["metadata"] = {**existing_metadata, **payload["metadata"]}
            for field in ("blocked_by", "blocks"):
                if field in payload:
                    task[field] = list(payload.get(field) or [])
            if "new_version" in payload:
                task["version"] = payload.get("new_version")
            if "change_reason" in payload:
                task["last_change_reason"] = payload.get("change_reason")
            if "error_type" in payload:
                task["failure_class"] = payload.get("error_type")
            task["updated_at"] = event_created_at
            continue

        if event_type == "task_completed":
            task_id = str(payload.get("task_id") or "").strip()
            if not task_id:
                continue
            task = tasks.setdefault(task_id, {"id": task_id})
            agent_name = str(
                payload.get("agent_name") or task.get("agent_name") or ""
            ).strip() or task.get("agent_name")
            task.update(
                {
                    "status": "completed",
                    "agent_name": agent_name,
                    "result_ref": str(payload.get("result_ref") or "").strip(),
                    "finished_at": event_created_at,
                    "updated_at": event_created_at,
                }
            )
            transcript_content = _transcript_content_from_payload(payload)
            if transcript_content:
                transcripts.append(
                    {
                        "message_id": f"task-completed:{task_id}:{_sequence(event)}",
                        "task_id": task_id,
                        "agent_name": agent_name,
                        "role": "assistant",
                        "content": transcript_content,
                        "stream_status": "complete",
                        "source_event_type": "task_completed",
                        "created_at": event_created_at,
                    }
                )
            continue

        if event_type == "task_failed":
            task_id = str(payload.get("task_id") or "").strip()
            if not task_id:
                continue
            task = tasks.setdefault(task_id, {"id": task_id})
            task.update(
                {
                    "status": "failed",
                    "agent_name": str(
                        payload.get("agent_name") or task.get("agent_name") or ""
                    ).strip()
                    or task.get("agent_name"),
                    "last_error": str(payload.get("error") or "").strip(),
                    "failure_class": str(payload.get("error_type") or "").strip()
                    or None,
                    "finished_at": event_created_at,
                    "updated_at": event_created_at,
                }
            )
            continue

        if event_type == "agent_idle":
            agent_name = str(payload.get("agent_name") or "").strip()
            if not agent_name:
                continue
            agent = _ensure_agent(agents, agent_name)
            agent.update(
                {
                    "status": "idle",
                    "idle_since": payload.get("idle_since"),
                    "idle_expires_at": payload.get("idle_expires_at"),
                    "updated_at": event_created_at,
                }
            )
            continue

        if event_type == "agent_wake":
            agent_name = str(payload.get("agent_name") or "").strip()
            if not agent_name:
                continue
            agent = _ensure_agent(
                agents,
                agent_name,
                role=str(payload.get("role") or "").strip() or None,
            )
            role = str(payload.get("role") or "").strip()
            agent.update(
                {
                    "agent_id": payload.get("agent_id") or agent.get("agent_id"),
                    "account_id": payload.get("account_id") or agent.get("account_id"),
                    **({"role": role} if role else {}),
                    "status": str(payload.get("status") or "working").strip()
                    or "working",
                    "wake_reason": payload.get("wake_reason"),
                    "wake_message_id": payload.get("message_id"),
                    "wake_task_id": payload.get("task_id"),
                    "previous_status": payload.get("previous_status"),
                    "previous_run_id": payload.get("previous_run_id"),
                    "previous_idle_since": payload.get("idle_since"),
                    "previous_idle_expires_at": payload.get("idle_expires_at"),
                    "idle_since": None,
                    "idle_expires_at": None,
                    "updated_at": event_created_at,
                }
            )
            continue

        if event_type == "agent_shutdown":
            agent_name = str(payload.get("agent_name") or "").strip()
            if not agent_name:
                continue
            agent = _ensure_agent(agents, agent_name)
            agent.update(
                {
                    "account_id": payload.get("account_id") or agent.get("account_id"),
                    "status": str(payload.get("status") or "closed").strip()
                    or "closed",
                    "agent_status": str(payload.get("status") or "closed").strip()
                    or "closed",
                    "shutdown_reason": str(payload.get("shutdown_reason") or "").strip()
                    or None,
                    "shutdown_at": event_created_at,
                    "updated_at": event_created_at,
                }
            )
            continue

        if event_type == "mailbox_sent":
            message_id = str(
                payload.get("message_id") or payload.get("id") or ""
            ).strip()
            if not message_id:
                continue
            messages[message_id] = {
                "message_id": message_id,
                "sender": payload.get("sender"),
                "recipient": payload.get("recipient") or payload.get("agent_name"),
                "kind": payload.get("kind"),
                "message_type": payload.get("message_type") or payload.get("type"),
                "summary": payload.get("summary"),
                "text": payload.get("text"),
                "payload": payload.get("payload") if isinstance(payload.get("payload"), dict) else {},
                "status": "unread",
                "created_at": event_created_at,
            }
            continue

        if event_type == "mailbox_acked":
            message_id = str(payload.get("message_id") or "").strip()
            if not message_id:
                continue
            message = messages.setdefault(message_id, {"message_id": message_id})
            message.update(
                {
                    "status": "acked",
                    "acked_by": payload.get("acked_by"),
                    "acked_at": payload.get("acked_at") or event_created_at,
                    "already_acked": payload.get("already_acked"),
                }
            )
            continue

        if event_type == "mailbox_dead_lettered":
            message_id = str(payload.get("message_id") or "").strip()
            if not message_id:
                continue
            message = messages.setdefault(message_id, {"message_id": message_id})
            message.update(
                {
                    "status": "dead_lettered",
                    "dead_letter_reason": payload.get("reason")
                    or payload.get("dead_letter_reason"),
                    "attempts": payload.get("attempts"),
                    "dead_lettered_at": payload.get("dead_lettered_at")
                    or event_created_at,
                }
            )
            continue

        if event_type in {
            "tool_call_started",
            "tool_call_completed",
            "tool_call_failed",
        }:
            tool_call_id = str(
                payload.get("tool_call_id")
                or payload.get("id")
                or f"tool-{_sequence(event)}"
            ).strip()
            if not tool_call_id:
                continue
            tool_call = tool_calls.setdefault(
                tool_call_id,
                {
                    "tool_call_id": tool_call_id,
                    "status": "pending",
                },
            )
            task_id = str(payload.get("task_id") or "").strip() or None
            agent_name = str(payload.get("agent_name") or "").strip() or None
            tool_call.update(
                {
                    "tool_call_id": tool_call_id,
                    "tool_name": payload.get("tool_name")
                    or tool_call.get("tool_name"),
                    "agent_name": agent_name or tool_call.get("agent_name"),
                    "task_id": task_id or tool_call.get("task_id"),
                    "activity_owner": getattr(event, "activity_owner", None),
                    "updated_at": event_created_at,
                }
            )
            if payload.get("arguments") is not None:
                tool_call["arguments"] = payload.get("arguments")
            if payload.get("arguments_summary") is not None:
                tool_call["arguments_summary"] = payload.get("arguments_summary")
            if payload.get("arguments_redacted") is not None:
                tool_call["arguments_redacted"] = bool(
                    payload.get("arguments_redacted")
                )
            if payload.get("arguments_size_bytes") is not None:
                tool_call["arguments_size_bytes"] = payload.get(
                    "arguments_size_bytes"
                )
            if event_type == "tool_call_started":
                tool_call["status"] = "running"
                tool_call["started_at"] = event_created_at
            elif event_type == "tool_call_completed":
                tool_call["status"] = "completed"
                tool_call["completed_at"] = event_created_at
                tool_call["result_summary"] = payload.get("result_summary")
            else:
                tool_call["status"] = "failed"
                tool_call["failed_at"] = event_created_at
                tool_call["error"] = payload.get("error")
                tool_call["error_type"] = payload.get("error_type")
            if task_id:
                task = tasks.setdefault(task_id, {"id": task_id})
                task_tool_call_ids = list(task.get("tool_call_ids") or [])
                if tool_call_id not in task_tool_call_ids:
                    task_tool_call_ids.append(tool_call_id)
                task["tool_call_ids"] = task_tool_call_ids
            if agent_name:
                agent = _ensure_agent(agents, agent_name)
                agent_tool_call_ids = list(agent.get("tool_call_ids") or [])
                if tool_call_id not in agent_tool_call_ids:
                    agent_tool_call_ids.append(tool_call_id)
                agent["tool_call_ids"] = agent_tool_call_ids
            continue

        if event_type == "team_deleted":
            team_shutdown = {
                "team_id": str(payload.get("team_id") or "").strip()
                or team.get("team_id"),
                "shutdown_reason": str(payload.get("shutdown_reason") or "").strip()
                or None,
            }
            run_status = "completed"
            terminal_reason = "shadow_clone_v2_team_shutdown"
            continue

        if event_type == "run_completed":
            run_status = "completed"
            terminal_reason = "shadow_clone_v2_completed"
            output = payload.get("final_output") or payload.get("output")
            if output:
                final_output = {
                    "content": str(output),
                    "source": "run_completed",
                    "created_at": event_created_at,
                }
            continue

        if event_type == "run_failed":
            run_status = "failed"
            terminal_reason = "shadow_clone_v2_failed"
            continue

    subagents = {
        task_id: _legacy_subagent_from_task(task, agents)
        for task_id, task in tasks.items()
    }
    total = len(subagents)
    completed = sum(1 for task in tasks.values() if task.get("status") == "completed")
    failed = sum(1 for task in tasks.values() if task.get("status") == "failed")
    running = sum(
        1
        for task in tasks.values()
        if _task_public_status(str(task.get("status") or ""))
        in {"running", "claimed", "working"}
    )
    status = run_status
    parent_status_value = str(parent_status or "").strip().lower()
    if status is None and parent_status_value in {"failed", "stopped"}:
        status = "failed" if parent_status_value == "failed" else parent_status_value
        terminal_reason = f"shadow_clone_v2_parent_{parent_status_value}"
    if status is None:
        status = "running" if running or total else "pending"
    phase = (
        status
        if status in {"completed", "failed", "cancelled", "timeout", "blocked"}
        else "execution"
    )
    public_team = {
        **team,
        "members": {name: dict(agent) for name, agent in agents.items()},
    }
    return {
        "status": status,
        "mode": "v2",
        "total": total,
        "completed": completed,
        "failed": failed,
        "running": running,
        "terminal_reason": terminal_reason,
        "updated_at": updated_at,
        "team": public_team,
        "tasks": tasks,
        "agents": agents,
        "messages": messages,
        "transcripts": transcripts,
        "tool_calls": tool_calls,
        "model": (
            {
                "requested": model.get("requested"),
                "effective": model.get("effective"),
            }
            if model
            else {}
        ),
        "final_output": final_output,
        "proposal": {
            "subtasks": [
                {
                    "id": task_id,
                    "role": payload.get("role"),
                    "task_description": payload.get("description"),
                }
                for task_id, payload in tasks.items()
            ],
            "dependencies": [],
        },
        "subagents": subagents,
        "live_activity": {
            "scope": "shadow_clone_main",
            "phase": phase,
            "reason": terminal_reason or "shadow_clone_v2_event_projection",
            "updated_at": updated_at,
        },
        "team_shutdown": team_shutdown,
    }


def project_shadow_clone_v2_results(events: list[Any]) -> list[dict[str, Any]]:
    state = project_shadow_clone_v2_public_state(
        agent_run_id="", events=events, parent_status=None
    )
    if state is None:
        return []
    rows: list[dict[str, Any]] = []
    for task_id, task in state["tasks"].items():
        status = str(task.get("status") or "").strip()
        if status not in {"completed", "failed"}:
            continue
        summary = (
            str(task.get("result_ref") or "").strip()
            if status == "completed"
            else str(task.get("last_error") or "").strip()
        )
        row = {
            "subtask_id": task_id,
            "role": task.get("subject") or task.get("role"),
            "status": status,
            "summary": summary or _terminal_result_visibility_summary(status),
            "submitted_at": task.get("finished_at"),
        }
        if status == "failed":
            row["failure_class"] = task.get("failure_class")
        rows.append(row)
    return rows


def project_shadow_clone_v2_full_result(
    *,
    events: list[Any],
    subtask_id: str,
) -> str | None:
    normalized_subtask_id = str(subtask_id or "").strip()
    if not normalized_subtask_id:
        return None
    state = project_shadow_clone_v2_public_state(
        agent_run_id="", events=events, parent_status=None
    )
    if state is None:
        return None
    task = state["tasks"].get(normalized_subtask_id)
    if not task:
        return None
    if task.get("status") == "completed":
        return str(task.get("result_ref") or "").strip() or None
    if task.get("status") == "failed":
        return str(task.get("last_error") or "").strip() or None
    return None
