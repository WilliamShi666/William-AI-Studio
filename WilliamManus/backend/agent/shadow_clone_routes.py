from __future__ import annotations

import json
from typing import Any, Optional
from uuid import UUID

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel

from agentscope_integration.shadow_clone.confirmation_hook import (
    ConfirmationDecisionWriteStatus,
    confirmation_channel,
    get_confirmation_result,
    record_confirmation_result,
)
from agentscope_integration.shadow_clone.file_delivery_source import (
    build_run_file_delivery_source,
)
from agentscope_integration.shadow_clone.sandbox_lease import (
    get_run_sandbox_lease,
    is_attachable_shadow_clone_binding_state,
)
from agentscope_integration.shadow_clone.constants import ConfirmationResult
from agentscope_integration.shadow_clone_v2.projection import (
    project_shadow_clone_v2_full_result as _project_v2_full_result,
)
from agentscope_integration.shadow_clone_v2.projection import (
    project_shadow_clone_v2_public_state as _project_v2_public_state,
)
from agentscope_integration.shadow_clone_v2.projection import (
    project_shadow_clone_v2_results as _project_v2_results,
)
from agentscope_integration.shadow_clone.state_machine import (
    TERMINAL_SHADOW_CLONE_STATUSES,
    get_state,
)
from agent.run_status_projection import read_terminal_status_from_response_tail
from services import redis as redis_service
from services.postgresql import DBConnection
from utils.logger import logger
from utils.simple_auth_middleware import (
    get_current_user_id_from_jwt,
    verify_thread_access,
)

shadow_clone_router = APIRouter()

_PUBLIC_STATE_CACHE_MAX_ENTRIES = 256
_PUBLIC_STATE_CACHE: dict[str, tuple[str, dict[str, Any]]] = {}
_PUBLIC_STATE_FIELDS = (
    "status",
    "mode",
    "total",
    "completed",
    "failed",
    "running",
    "completion_mode",
    "terminal_reason",
    "updated_at",
)
_PUBLIC_ENVIRONMENT_FIELDS = (
    "status",
    "ready",
    "last_error",
    "prepared_at",
    "manifest",
)
_PUBLIC_PROPOSAL_SUBTASK_FIELDS = ("id", "role", "task_description")
_PUBLIC_PROPOSAL_DEPENDENCY_FIELDS = ("from_id", "to_id")
_PUBLIC_LIVE_ACTIVITY_FIELDS = (
    "scope",
    "phase",
    "reason",
    "subtask_id",
    "epoch",
    "updated_at",
)
_PUBLIC_RECOVERY_FIELDS = (
    "pending",
    "kind",
    "message",
    "requested_at",
    "scope",
    "failed_subtasks",
    "retryable_subtasks",
    "decision",
    "decision_at",
)
_PUBLIC_SUBAGENT_FIELDS = (
    "status",
    "role",
    "result_summary",
    "started_at",
    "finished_at",
    "created_at",
    "attempt_index",
    "failure_class",
    "last_error",
)
_PUBLIC_SUBAGENT_RECOVERY_FIELDS = (
    "mode",
    "phase",
    "reason",
    "wake_attempts",
    "replacement_attempts",
    "replacement_context_id",
    "handoff_summary",
    "updated_at",
)
_PUBLIC_RESULT_FIELDS = (
    "subtask_id",
    "role",
    "status",
    "summary",
    "submitted_at",
    "late_peer_notes_count",
    "attempt_index",
    "failure_class",
)
_PUBLIC_RESULT_FIELD_SET = frozenset(_PUBLIC_RESULT_FIELDS)
_RESULT_STORE_TERMINAL_STATUS_MAP = {
    "completed": "completed",
    "failed": "failed",
    "cancelled": "cancelled",
    "stopped": "cancelled",
    "timeout": "timeout",
    "denied": "denied",
}
_NONTERMINAL_RESULT_STATUSES = frozenset({"pending", "running"})
_PUBLIC_PARENT_RUN_TERMINAL_STATUS_MAP = {
    "failed": "failed",
    "stopped": "cancelled",
}
_PUBLIC_PARENT_RUN_TERMINAL_REASON_MAP = {
    "failed": "parent_run_failed",
    "stopped": "parent_run_stopped",
}
_PUBLIC_STREAM_TERMINAL_STATUS_MAP = {
    "completed": "completed",
    "failed": "failed",
    "stopped": "cancelled",
}
_PUBLIC_STREAM_TERMINAL_REASON_MAP = {
    "completed": "response_stream_completed",
    "failed": "response_stream_failed",
    "stopped": "response_stream_stopped",
}
_PUBLIC_RUNTIME_FAILURE_TERMINAL_REASON = "proposal_required_but_missing"
_PUBLIC_NONTERMINAL_SHADOW_CLONE_STATUSES = frozenset(
    {"pending", "confirming", "running", "aggregating"},
)
_TERMINAL_UI_PHASES = frozenset({"completed", "failed", "cancelled", "timeout"})
_MAIN_AGENT_CONTINUATION_REASONS = frozenset(
    {"denied_continue", "replan_continue", "failed_layer_continue"},
)
_RECOVERING_REASONS = frozenset(
    {
        "subagent_recovering",
        "subagent_wake_sent",
        "subagent_resumed",
        "subagent_replacement_started",
        "subagent_replacement_completed",
        "subagent_recovery_exhausted",
    }
)


class DenyRequest(BaseModel):
    reason: Optional[str] = None


def _confirmation_conflict_detail(result: ConfirmationResult | None) -> str:
    if result == ConfirmationResult.CONFIRMED:
        return "Shadow Clone was already confirmed"
    if result == ConfirmationResult.DENIED:
        return "Shadow Clone was already denied"
    return "Shadow Clone decision was already recorded"


def _pick_dict_fields(payload: dict, allowed_keys: tuple[str, ...]) -> dict:
    sanitized = {}
    for key in allowed_keys:
        if key in payload:
            sanitized[key] = payload[key]
    return sanitized


def _derive_activity_owner(
    *,
    scope: Any = None,
    ui_phase: Any = None,
    status: Any = None,
) -> str:
    ui_phase_value = str(ui_phase or "").strip().lower()
    status_value = str(status or "").strip().lower()
    if ui_phase_value in _TERMINAL_UI_PHASES or status_value in _TERMINAL_UI_PHASES:
        return "none"

    scope_value = str(scope or "").strip().lower()
    if scope_value == "shadow_clone_main":
        return "shadow_clone"
    if scope_value == "main_agent":
        return "main_agent"
    return "none"


def _derive_ui_phase(
    *,
    scope: Any = None,
    phase: Any = None,
    reason: Any = None,
    status: Any = None,
    environment_ready: Optional[bool] = None,
    recovery_pending: bool = False,
    ui_phase_override: Optional[str] = None,
) -> Optional[str]:
    if ui_phase_override:
        return str(ui_phase_override).strip() or None

    status_value = str(status or "").strip().lower()
    phase_value = str(phase or "").strip().lower()
    reason_value = str(reason or "").strip().lower()

    if status_value in _TERMINAL_UI_PHASES:
        return status_value
    if phase_value in _TERMINAL_UI_PHASES:
        return phase_value
    if phase_value == "planning":
        return "planning"
    if phase_value == "aggregate" or status_value == "aggregating":
        return "aggregating"
    if status_value == "confirming" or phase_value == "confirming":
        if environment_ready is False:
            return "preparing_environment"
        return "confirming"
    if recovery_pending or reason_value in _RECOVERING_REASONS:
        return "recovering"
    if phase_value == "execution":
        activity_owner = _derive_activity_owner(scope=scope, status=status)
        if (
            activity_owner == "main_agent"
            or reason_value in _MAIN_AGENT_CONTINUATION_REASONS
        ):
            return "main_agent_continuation"
        if activity_owner == "shadow_clone":
            return "subagents_running"
    if status_value == "denied" and phase_value == "completed":
        return "completed"
    return None


def _build_shadow_clone_contract(
    *,
    live_activity: Optional[dict[str, Any]] = None,
    status: Any = None,
    environment_ready: Optional[bool] = None,
    recovery_pending: bool = False,
    ui_phase_override: Optional[str] = None,
    phase_reason: Optional[str] = None,
    activity_owner_override: Optional[str] = None,
) -> dict[str, Any]:
    live_activity_payload = live_activity if isinstance(live_activity, dict) else {}
    scope = live_activity_payload.get("scope")
    phase = live_activity_payload.get("phase")
    resolved_phase_reason = (
        str(phase_reason or "").strip()
        or str(live_activity_payload.get("reason") or "").strip()
        or None
    )
    ui_phase = _derive_ui_phase(
        scope=scope,
        phase=phase,
        reason=resolved_phase_reason,
        status=status,
        environment_ready=environment_ready,
        recovery_pending=recovery_pending,
        ui_phase_override=ui_phase_override,
    )
    activity_owner = str(
        activity_owner_override or ""
    ).strip() or _derive_activity_owner(
        scope=scope,
        ui_phase=ui_phase,
        status=status,
    )
    return {
        "activity_owner": activity_owner,
        "ui_phase": ui_phase,
        "phase_reason": resolved_phase_reason,
    }


def _attach_public_shadow_clone_contract(state: dict[str, Any]) -> dict[str, Any]:
    live_activity = state.get("live_activity")
    environment = state.get("environment")
    recovery = state.get("recovery")
    environment_ready = None
    if isinstance(environment, dict) and "ready" in environment:
        environment_ready = bool(environment.get("ready"))
    phase_reason = (
        str((live_activity or {}).get("reason") or "").strip()
        or str(state.get("terminal_reason") or "").strip()
        or (
            str((recovery or {}).get("message") or "").strip()
            if isinstance(recovery, dict)
            else ""
        )
        or None
    )
    contract = _build_shadow_clone_contract(
        live_activity=live_activity if isinstance(live_activity, dict) else None,
        status=state.get("status"),
        environment_ready=environment_ready,
        recovery_pending=(
            bool((recovery or {}).get("pending"))
            if isinstance(recovery, dict)
            else False
        ),
        phase_reason=phase_reason,
    )
    return {
        **state,
        **{key: value for key, value in contract.items() if value is not None},
    }


def _sanitize_public_shadow_clone_state_uncached(
    state: dict[str, Any],
) -> dict[str, Any]:
    raw = state if isinstance(state, dict) else {}
    sanitized = _pick_dict_fields(
        raw,
        _PUBLIC_STATE_FIELDS,
    )

    environment = raw.get("environment")
    if isinstance(environment, dict):
        sanitized["environment"] = _pick_dict_fields(
            environment, _PUBLIC_ENVIRONMENT_FIELDS
        )

    proposal = raw.get("proposal")
    if isinstance(proposal, dict):
        sanitized_proposal = {}
        subtasks = proposal.get("subtasks")
        if isinstance(subtasks, list):
            sanitized_proposal["subtasks"] = [
                _pick_dict_fields(item, _PUBLIC_PROPOSAL_SUBTASK_FIELDS)
                for item in subtasks
                if isinstance(item, dict)
            ]
        dependencies = proposal.get("dependencies")
        if isinstance(dependencies, list):
            sanitized_proposal["dependencies"] = [
                _pick_dict_fields(item, _PUBLIC_PROPOSAL_DEPENDENCY_FIELDS)
                for item in dependencies
                if isinstance(item, dict)
            ]
        sanitized["proposal"] = sanitized_proposal

    live_activity = raw.get("live_activity")
    if isinstance(live_activity, dict):
        sanitized["live_activity"] = _pick_dict_fields(
            live_activity, _PUBLIC_LIVE_ACTIVITY_FIELDS
        )

    recovery = raw.get("recovery")
    if isinstance(recovery, dict):
        sanitized["recovery"] = _pick_dict_fields(recovery, _PUBLIC_RECOVERY_FIELDS)

    subagents = raw.get("subagents")
    if isinstance(subagents, dict):
        sanitized["subagents"] = {
            subtask_id: (
                {
                    **_pick_dict_fields(
                        payload,
                        _PUBLIC_SUBAGENT_FIELDS,
                    ),
                    **(
                        {
                            "recovery": _pick_dict_fields(
                                payload.get("recovery"),
                                _PUBLIC_SUBAGENT_RECOVERY_FIELDS,
                            )
                        }
                        if isinstance(payload.get("recovery"), dict)
                        else {}
                    ),
                }
                if isinstance(payload, dict)
                else payload
            )
            for subtask_id, payload in subagents.items()
        }

    return sanitized


def _sanitize_public_shadow_clone_state(
    agent_run_id: str,
    state: dict[str, Any],
) -> dict[str, Any]:
    updated_at = str((state or {}).get("updated_at") or "").strip()
    if updated_at:
        cached = _PUBLIC_STATE_CACHE.get(agent_run_id)
        if cached and cached[0] == updated_at:
            return cached[1]

    sanitized = _sanitize_public_shadow_clone_state_uncached(state)
    if updated_at:
        if (
            len(_PUBLIC_STATE_CACHE) >= _PUBLIC_STATE_CACHE_MAX_ENTRIES
            and agent_run_id not in _PUBLIC_STATE_CACHE
        ):
            _PUBLIC_STATE_CACHE.clear()
        _PUBLIC_STATE_CACHE[agent_run_id] = (updated_at, sanitized)
    return sanitized


def _apply_public_shadow_clone_runtime_failure_truth(
    state: dict[str, Any],
) -> dict[str, Any]:
    current_status = str(state.get("status") or "").strip().lower()
    if current_status != "pending":
        return state

    live_activity = state.get("live_activity")
    if not isinstance(live_activity, dict):
        return state
    if str(live_activity.get("phase") or "").strip().lower() != "failed":
        return state
    if (
        str(live_activity.get("reason") or "").strip().lower()
        != "proposal_required_but_missing"
    ):
        return state

    environment = state.get("environment")
    if not isinstance(environment, dict):
        return state
    if str(environment.get("status") or "").strip().lower() != "failed":
        return state

    return {
        **state,
        "status": "failed",
        "completion_mode": None,
        "terminal_reason": _PUBLIC_RUNTIME_FAILURE_TERMINAL_REASON,
    }


def _parse_agent_run_metadata(agent_run: dict[str, Any]) -> dict[str, Any]:
    metadata = agent_run.get("metadata")
    if isinstance(metadata, dict):
        return metadata
    if not isinstance(metadata, str):
        return {}
    try:
        parsed = json.loads(metadata)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _agent_run_has_shadow_clone_context(agent_run: dict[str, Any]) -> bool:
    metadata = _parse_agent_run_metadata(agent_run)
    raw_mode = metadata.get("shadow_clone_mode")
    if isinstance(raw_mode, bool):
        return raw_mode
    normalized_mode = str(raw_mode or "").strip().lower()
    return normalized_mode not in {"", "off", "false", "none", "disabled"}


def _agent_run_is_shadow_clone_v2(agent_run: dict[str, Any]) -> bool:
    metadata = _parse_agent_run_metadata(agent_run)
    runtime = (
        str(
            metadata.get("shadow_clone_runtime")
            or metadata.get("shadow_clone_engine")
            or ""
        )
        .strip()
        .lower()
    )
    if runtime == "v2":
        return True
    return str(metadata.get("shadow_clone_mode") or "").strip().lower() == "v2"


async def _read_shadow_clone_v2_events(agent_run_id: str) -> list[Any]:
    from agentscope_integration.shadow_clone_v2.event_log import read_events

    return await read_events(agent_run_id)


def _shadow_clone_v2_event_type(event: Any) -> str:
    raw_type = (
        event.get("type") if isinstance(event, dict) else getattr(event, "type", None)
    )
    return str(getattr(raw_type, "value", raw_type) or "").strip().lower()


def _shadow_clone_v2_event_payload(event: Any) -> dict[str, Any]:
    payload = (
        event.get("payload")
        if isinstance(event, dict)
        else getattr(event, "payload", None)
    )
    return payload if isinstance(payload, dict) else {}


def _shadow_clone_v2_event_created_at(event: Any) -> Optional[str]:
    created_at = (
        event.get("created_at")
        if isinstance(event, dict)
        else getattr(event, "created_at", None)
    )
    text = str(created_at or "").strip()
    return text or None


def _project_shadow_clone_v2_public_state(
    *,
    agent_run_id: str,
    events: list[Any],
    parent_status: Any,
) -> Optional[dict[str, Any]]:
    return _project_v2_public_state(
        agent_run_id=agent_run_id,
        events=events,
        parent_status=parent_status,
    )


def _project_shadow_clone_v2_results(events: list[Any]) -> list[dict[str, Any]]:
    return _project_v2_results(events)


def _project_shadow_clone_v2_full_result(
    *,
    events: list[Any],
    subtask_id: str,
) -> Optional[str]:
    return _project_v2_full_result(events=events, subtask_id=subtask_id)


def _is_parent_run_terminal_status(status_value: Any) -> bool:
    return str(status_value or "").strip().lower() in {"completed", "failed", "stopped"}


async def _project_agent_run_terminal_truth_from_stream(
    agent_run: dict[str, Any],
) -> tuple[dict[str, Any], Optional[str], Optional[str]]:
    current_status = str(agent_run.get("status") or "").strip().lower()
    if _is_parent_run_terminal_status(current_status):
        return agent_run, None, None

    agent_run_id = str(
        agent_run.get("agent_run_id") or agent_run.get("id") or ""
    ).strip()
    if not agent_run_id:
        return agent_run, None, None

    projected_status, projected_message = await read_terminal_status_from_response_tail(
        agent_run_id,
    )
    if projected_status is None:
        return agent_run, None, None

    logger.warning(
        "[ShadowClone] Projecting parent run terminal truth from Redis response tail "
        "run_id=%s db_status=%s projected_status=%s",
        agent_run_id,
        current_status or None,
        projected_status,
    )
    projected_agent_run = dict(agent_run)
    projected_agent_run["status"] = projected_status
    if (
        projected_status == "failed"
        and not projected_agent_run.get("error")
        and projected_message
    ):
        projected_agent_run["error"] = projected_message
    return projected_agent_run, projected_status, projected_message


def _coerce_public_subagents_terminal_status(
    subagents: dict[str, Any],
    *,
    terminal_status: str,
) -> dict[str, Any]:
    sanitized: dict[str, Any] = {}
    for subtask_id, payload in subagents.items():
        if not isinstance(payload, dict):
            sanitized[subtask_id] = payload
            continue
        current_status = str(payload.get("status") or "").strip().lower()
        if current_status in {"pending", "running"}:
            sanitized[subtask_id] = {
                **payload,
                "status": terminal_status,
            }
            continue
        sanitized[subtask_id] = payload
    return sanitized


def _build_minimal_stream_terminal_shadow_state(
    stream_status: str,
    *,
    message: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    terminal_status = _PUBLIC_STREAM_TERMINAL_STATUS_MAP.get(stream_status)
    if terminal_status is None:
        return None

    state: dict[str, Any] = {
        "status": terminal_status,
        "completion_mode": None,
        "terminal_reason": _PUBLIC_STREAM_TERMINAL_REASON_MAP[stream_status],
        "environment": {
            "ready": False,
            "status": terminal_status,
        },
    }
    if terminal_status == "failed" and message:
        state["environment"]["last_error"] = message
    return state


def _apply_public_shadow_clone_stream_terminal_truth(
    state: Optional[dict[str, Any]],
    *,
    stream_status: Optional[str],
    message: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    normalized_stream_status = str(stream_status or "").strip().lower()
    terminal_status = _PUBLIC_STREAM_TERMINAL_STATUS_MAP.get(normalized_stream_status)
    if terminal_status is None:
        return state

    if state is None:
        return _build_minimal_stream_terminal_shadow_state(
            normalized_stream_status,
            message=message,
        )

    current_status = str((state or {}).get("status") or "").strip().lower()
    if (
        current_status in TERMINAL_SHADOW_CLONE_STATUSES
        and current_status != terminal_status
    ):
        return state

    merged_state = dict(state or {})
    merged_state["status"] = terminal_status
    if current_status not in TERMINAL_SHADOW_CLONE_STATUSES:
        merged_state["completion_mode"] = None
    merged_state["terminal_reason"] = (
        str(merged_state.get("terminal_reason") or "").strip()
        or _PUBLIC_STREAM_TERMINAL_REASON_MAP[normalized_stream_status]
    )

    if "running" in merged_state:
        merged_state["running"] = 0

    live_activity = merged_state.get("live_activity")
    if isinstance(live_activity, dict):
        current_phase = str(live_activity.get("phase") or "").strip().lower()
        if current_phase not in TERMINAL_SHADOW_CLONE_STATUSES:
            merged_state["live_activity"] = {
                **live_activity,
                "phase": terminal_status,
                "reason": _PUBLIC_STREAM_TERMINAL_REASON_MAP[normalized_stream_status],
            }

    environment = (
        dict(merged_state.get("environment"))
        if isinstance(merged_state.get("environment"), dict)
        else {}
    )
    environment["ready"] = False
    current_environment_status = str(environment.get("status") or "").strip().lower()
    if current_environment_status not in TERMINAL_SHADOW_CLONE_STATUSES:
        environment["status"] = terminal_status
    if terminal_status == "failed" and message and not environment.get("last_error"):
        environment["last_error"] = message
    if environment:
        merged_state["environment"] = environment

    subagents = merged_state.get("subagents")
    if isinstance(subagents, dict):
        merged_state["subagents"] = _coerce_public_subagents_terminal_status(
            subagents,
            terminal_status=terminal_status,
        )

    return merged_state


def _apply_public_shadow_clone_parent_run_truth(
    agent_run: dict[str, Any],
    state: Optional[dict[str, Any]],
    *,
    lease: Optional[dict[str, Any]] = None,
) -> Optional[dict[str, Any]]:
    parent_status = str(agent_run.get("status") or "").strip().lower()
    terminal_status = _PUBLIC_PARENT_RUN_TERMINAL_STATUS_MAP.get(parent_status)
    if terminal_status is None:
        return state
    terminal_reason = (
        _PUBLIC_PARENT_RUN_TERMINAL_REASON_MAP.get(parent_status)
        or f"parent_run_{parent_status}"
    )

    if (
        state is None
        and lease is None
        and not _agent_run_has_shadow_clone_context(agent_run)
    ):
        return None

    current_status = str((state or {}).get("status") or "").strip().lower()
    if (
        current_status in TERMINAL_SHADOW_CLONE_STATUSES
        and current_status != terminal_status
    ):
        return state

    merged_state = dict(state or {})
    merged_state["status"] = terminal_status
    if current_status not in TERMINAL_SHADOW_CLONE_STATUSES:
        merged_state["completion_mode"] = None
    merged_state["terminal_reason"] = (
        str(merged_state.get("terminal_reason") or "").strip() or terminal_reason
    )

    if "running" in merged_state:
        merged_state["running"] = 0

    live_activity = merged_state.get("live_activity")
    if isinstance(live_activity, dict):
        current_phase = str(live_activity.get("phase") or "").strip().lower()
        if current_phase not in TERMINAL_SHADOW_CLONE_STATUSES:
            merged_state["live_activity"] = {
                **live_activity,
                "phase": terminal_status,
                "reason": terminal_reason,
            }

    environment = (
        dict(merged_state.get("environment"))
        if isinstance(merged_state.get("environment"), dict)
        else {}
    )
    environment["ready"] = False
    current_environment_status = str(environment.get("status") or "").strip().lower()
    if current_environment_status not in TERMINAL_SHADOW_CLONE_STATUSES:
        environment["status"] = terminal_status
    if terminal_status == "failed" and not environment.get("last_error"):
        fallback_error = str(agent_run.get("error") or "").strip() or None
        if fallback_error:
            environment["last_error"] = fallback_error
    if environment:
        merged_state["environment"] = environment

    subagents = merged_state.get("subagents")
    if isinstance(subagents, dict):
        merged_state["subagents"] = _coerce_public_subagents_terminal_status(
            subagents,
            terminal_status=terminal_status,
        )

    return merged_state


def _get_public_shadow_clone_status(state: Optional[dict[str, Any]]) -> str:
    return str((state or {}).get("status") or "").strip().lower()


def _should_refresh_parent_run_truth(
    agent_run: dict[str, Any],
    state: Optional[dict[str, Any]],
    *,
    lease: Optional[dict[str, Any]] = None,
) -> bool:
    public_status = _get_public_shadow_clone_status(state)
    if public_status not in _PUBLIC_NONTERMINAL_SHADOW_CLONE_STATUSES:
        return False

    parent_status = str(agent_run.get("status") or "").strip().lower()
    if parent_status in _PUBLIC_PARENT_RUN_TERMINAL_STATUS_MAP:
        return True

    if public_status == "aggregating":
        return True

    if not isinstance(lease, dict):
        return False

    binding_state = str(lease.get("binding_state") or "").strip().lower()
    environment_status = str(lease.get("environment_status") or "").strip().lower()
    return (
        binding_state == "released"
        or environment_status in TERMINAL_SHADOW_CLONE_STATUSES
    )


def _build_minimal_parent_terminal_shadow_state(
    agent_run: dict[str, Any],
) -> dict[str, Any]:
    parent_status = str(agent_run.get("status") or "").strip().lower()
    terminal_status = _PUBLIC_PARENT_RUN_TERMINAL_STATUS_MAP[parent_status]
    minimal_state: dict[str, Any] = {
        "status": terminal_status,
        "completion_mode": None,
        "terminal_reason": (
            _PUBLIC_PARENT_RUN_TERMINAL_REASON_MAP.get(parent_status)
            or f"parent_run_{parent_status}"
        ),
        "environment": {
            "ready": False,
            "status": terminal_status,
        },
    }
    if terminal_status == "failed":
        fallback_error = str(agent_run.get("error") or "").strip() or None
        if fallback_error:
            minimal_state["environment"]["last_error"] = fallback_error
    return minimal_state


def _attach_file_delivery_source(
    agent_run: dict[str, Any],
    state: Optional[dict[str, Any]],
    *,
    lease: Optional[dict[str, Any]] = None,
) -> Optional[dict[str, Any]]:
    if state is None:
        return None
    agent_run_id = str(
        agent_run.get("agent_run_id") or agent_run.get("id") or ""
    ).strip()
    return {
        **state,
        "file_delivery_source": build_run_file_delivery_source(
            agent_run_id=agent_run_id,
            parent_status=agent_run.get("status"),
            lease=lease,
            effective_status=state.get("status"),
            state=state,
        ),
    }


def _sanitize_public_shadow_clone_results(rows: list[dict]) -> list[dict]:
    if all(
        isinstance(row, dict) and row.keys() <= _PUBLIC_RESULT_FIELD_SET for row in rows
    ):
        return rows

    sanitized_rows = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        sanitized_rows.append(_pick_dict_fields(row, _PUBLIC_RESULT_FIELDS))
    return sanitized_rows


def _terminal_result_visibility_summary(status: Any) -> str:
    normalized_status = str(status or "").strip().lower()
    if normalized_status == "completed":
        return "Subagent completed without a visible textual result."
    if normalized_status == "failed":
        return "Subagent failed before producing a visible textual result."
    if normalized_status == "cancelled":
        return "Subagent stopped before producing a visible textual result."
    if normalized_status == "timeout":
        return "Subagent timed out before producing a visible textual result."
    if normalized_status == "denied":
        return "Subagent was denied before producing a visible textual result."
    return "Subagent finished without a visible textual result."


def _build_terminal_result_expectations_from_state(
    state: Optional[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not isinstance(state, dict):
        return []

    subagents = state.get("subagents")
    if not isinstance(subagents, dict):
        return []

    expected_results: list[dict[str, Any]] = []
    for subtask_id, payload in subagents.items():
        if not isinstance(payload, dict):
            continue
        normalized_subtask_id = str(subtask_id or "").strip()
        if not normalized_subtask_id:
            continue
        normalized_status = _RESULT_STORE_TERMINAL_STATUS_MAP.get(
            str(payload.get("status") or "").strip().lower(),
        )
        if normalized_status is None:
            continue
        expected_results.append(
            {
                "subtask_id": normalized_subtask_id,
                "role": str(payload.get("role") or "").strip() or None,
                "status": normalized_status,
                "summary": (
                    str(payload.get("result_summary") or "").strip()
                    or str(payload.get("last_error") or "").strip()
                    or _terminal_result_visibility_summary(normalized_status)
                ),
                "attempt_index": (
                    int(payload.get("attempt_index"))
                    if payload.get("attempt_index") is not None
                    else None
                ),
                "failure_class": (
                    str(payload.get("failure_class") or "").strip() or None
                ),
            }
        )
    return expected_results


def _reconcile_public_shadow_clone_results(
    rows: list[dict],
    *,
    state: Optional[dict[str, Any]],
    parent_status: Optional[str],
) -> list[dict]:
    sanitized_rows = _sanitize_public_shadow_clone_results(rows)
    expectations_by_subtask = {
        str(item.get("subtask_id") or "").strip(): item
        for item in _build_terminal_result_expectations_from_state(state)
        if str(item.get("subtask_id") or "").strip()
    }
    parent_result_status = _RESULT_STORE_TERMINAL_STATUS_MAP.get(
        str(parent_status or "").strip().lower(),
    )

    reconciled_rows: list[dict] = []
    for row in sanitized_rows:
        normalized_subtask_id = str(row.get("subtask_id") or "").strip()
        expected_row = expectations_by_subtask.get(normalized_subtask_id)
        if expected_row is not None:
            reconciled_rows.append(
                {
                    **row,
                    "role": row.get("role") or expected_row.get("role"),
                    "status": expected_row["status"],
                    "summary": (
                        str(row.get("summary") or "").strip()
                        or str(expected_row.get("summary") or "").strip()
                        or _terminal_result_visibility_summary(expected_row["status"])
                    ),
                    "attempt_index": (
                        row.get("attempt_index")
                        if row.get("attempt_index") is not None
                        else expected_row.get("attempt_index")
                    ),
                    "failure_class": row.get("failure_class")
                    or expected_row.get("failure_class"),
                }
            )
            continue

        normalized_row_status = str(row.get("status") or "").strip().lower()
        if (
            parent_result_status is not None
            and normalized_row_status in _NONTERMINAL_RESULT_STATUSES
        ):
            reconciled_rows.append(
                {
                    **row,
                    "status": parent_result_status,
                    "summary": (
                        str(row.get("summary") or "").strip()
                        or _terminal_result_visibility_summary(parent_result_status)
                    ),
                }
            )
            continue

        reconciled_rows.append(row)

    return reconciled_rows


def _merge_public_shadow_clone_lease_truth(
    state: dict[str, Any],
    lease: dict[str, Any],
) -> dict[str, Any]:
    binding_state = str(lease.get("binding_state") or "").strip().lower()
    environment_status = str(lease.get("environment_status") or "").strip().lower()
    lease_last_error = str(lease.get("last_error") or "").strip() or None

    merged_state = {
        **state,
        "sandbox": {
            "id": lease.get("sandbox_id"),
            "type": lease.get("sandbox_type"),
            "binding_state": lease.get("binding_state"),
            "environment_status": lease.get("environment_status"),
            "environment_ready": lease.get("environment_ready"),
            "last_error": lease_last_error,
        },
    }
    if not isinstance(merged_state.get("environment"), dict):
        merged_state["environment"] = {}

    current_status = str(merged_state.get("status") or "").strip().lower()
    lost_recovery_pending = binding_state == "lost" and (
        current_status in _PUBLIC_NONTERMINAL_SHADOW_CLONE_STATUSES
        or not current_status
    )

    environment = {
        **merged_state.get("environment", {}),
        "manifest": {
            **(merged_state.get("environment", {}).get("manifest", {}) or {}),
            **(lease.get("environment_manifest") or {}),
        },
    }
    effective_environment_status = (
        environment_status or str(environment.get("status") or "").strip().lower()
    )
    if binding_state in {"recovery_required", "recovering"} or lost_recovery_pending:
        effective_environment_status = "recovering"
    elif binding_state == "lost":
        effective_environment_status = "failed"

    environment["prepared_at"] = lease.get(
        "environment_prepared_at"
    ) or environment.get("prepared_at")
    environment["ready"] = bool(lease.get("environment_ready"))
    environment["status"] = effective_environment_status or environment.get("status")

    if binding_state in {"recovery_required", "recovering", "lost"}:
        environment["ready"] = False

    if lease_last_error and (
        environment_status == "failed"
        or binding_state in {"recovery_required", "lost"}
        or lost_recovery_pending
    ):
        environment["last_error"] = lease_last_error

    merged_state["environment"] = environment
    merged_state["sandbox"]["environment_status"] = environment["status"]

    if environment_status == "failed" or (
        binding_state == "lost" and not lost_recovery_pending
    ):
        merged_state["status"] = "failed"
        merged_state["terminal_reason"] = str(
            merged_state.get("terminal_reason") or ""
        ).strip() or ("lease_lost" if binding_state == "lost" else "environment_failed")
        merged_state["completion_mode"] = None
    elif lost_recovery_pending:
        if not current_status:
            merged_state["status"] = "running"
        recovery = (
            dict(merged_state.get("recovery"))
            if isinstance(merged_state.get("recovery"), dict)
            else {}
        )
        if not bool(recovery.get("pending")):
            recovery.update(
                {
                    "pending": True,
                    "kind": "replacement",
                    "message": lease_last_error
                    or "Shadow Clone recovery sandbox replacement required.",
                }
            )
            merged_state["recovery"] = recovery
    elif binding_state == "recovery_required":
        recovery = (
            dict(merged_state.get("recovery"))
            if isinstance(merged_state.get("recovery"), dict)
            else {}
        )
        if not bool(recovery.get("pending")):
            recovery.update(
                {
                    "pending": True,
                    "kind": "clone",
                    "message": lease_last_error
                    or "Shadow Clone standby clone recovery required.",
                }
            )
            merged_state["recovery"] = recovery

    if binding_state and is_attachable_shadow_clone_binding_state(binding_state):
        return merged_state

    if binding_state in {"recovery_required", "recovering", "lost"}:
        merged_state["sandbox"]["environment_ready"] = False

    return merged_state


async def _get_agent_run_with_access_check(agent_run_id: str, user_id: str) -> dict:
    db = DBConnection()
    client = await db.client

    agent_run_result = await (
        client.table("agent_runs")
        .select("*")
        .eq("agent_run_id", agent_run_id)
        .execute()
    )
    agent_run_rows = agent_run_result.data or []
    if not agent_run_rows:
        # Compatibility fallback for legacy rows queried by primary key.
        try:
            UUID(str(agent_run_id))
        except Exception:
            agent_run_rows = []
        else:
            agent_run_result = await (
                client.table("agent_runs").select("*").eq("id", agent_run_id).execute()
            )
            agent_run_rows = agent_run_result.data or []

    if not agent_run_rows:
        raise HTTPException(status_code=404, detail="Agent run not found")

    agent_run = agent_run_rows[0]
    thread_id = agent_run.get("thread_id")
    if not thread_id:
        raise HTTPException(status_code=404, detail="Thread not found")

    thread_result = await (
        client.table("threads")
        .select("account_id")
        .eq("thread_id", thread_id)
        .execute()
    )
    thread_rows = thread_result.data or []
    if not thread_rows:
        raise HTTPException(status_code=404, detail="Thread not found")

    thread_user_id = thread_rows[0].get("account_id")
    if thread_user_id != user_id:
        await verify_thread_access(client, thread_id, user_id)

    return agent_run


@shadow_clone_router.post("/{agent_run_id}/shadow-clone/confirm")
async def confirm_shadow_clone(
    agent_run_id: str,
    user_id: str = Depends(get_current_user_id_from_jwt),
):
    await _get_agent_run_with_access_check(agent_run_id, user_id)

    existing_result = await get_confirmation_result(agent_run_id)
    if existing_result == ConfirmationResult.CONFIRMED:
        await redis_service.publish(
            confirmation_channel(agent_run_id),
            "CONFIRMED",
        )
        return {"status": "confirmed"}
    if existing_result == ConfirmationResult.DENIED:
        raise HTTPException(
            status_code=409,
            detail=_confirmation_conflict_detail(existing_result),
        )

    state = await get_state(agent_run_id)
    if not state or state.get("status") != "confirming":
        raise HTTPException(status_code=409, detail="Not in confirming state")
    environment = state.get("environment") or {}
    if environment and not bool(environment.get("ready")):
        raise HTTPException(
            status_code=409,
            detail="Shadow Clone environment is not ready for confirmation",
        )
    lease = await get_run_sandbox_lease(agent_run_id)
    if lease is not None:
        binding_state = str(lease.get("binding_state") or "").strip().lower()
        if (
            binding_state
            and not is_attachable_shadow_clone_binding_state(binding_state)
        ) or not bool(lease.get("environment_ready")):
            raise HTTPException(
                status_code=409,
                detail="Shadow Clone environment is not ready for confirmation",
            )

    write_status, stored_result = await record_confirmation_result(
        agent_run_id,
        ConfirmationResult.CONFIRMED,
    )
    if write_status == ConfirmationDecisionWriteStatus.CONFLICT:
        raise HTTPException(
            status_code=409,
            detail=_confirmation_conflict_detail(stored_result),
        )

    await redis_service.publish(
        confirmation_channel(agent_run_id),
        "CONFIRMED",
    )
    return {"status": "confirmed"}


@shadow_clone_router.post("/{agent_run_id}/shadow-clone/deny")
async def deny_shadow_clone(
    agent_run_id: str,
    body: Optional[DenyRequest] = Body(None),
    user_id: str = Depends(get_current_user_id_from_jwt),
):
    await _get_agent_run_with_access_check(agent_run_id, user_id)

    existing_result = await get_confirmation_result(agent_run_id)
    if existing_result == ConfirmationResult.DENIED:
        await redis_service.publish(
            confirmation_channel(agent_run_id),
            "DENIED",
        )
        return {
            "status": "denied",
            "reason": (body.reason if body else None),
        }
    if existing_result == ConfirmationResult.CONFIRMED:
        raise HTTPException(
            status_code=409,
            detail=_confirmation_conflict_detail(existing_result),
        )

    state = await get_state(agent_run_id)
    if not state or state.get("status") != "confirming":
        raise HTTPException(status_code=409, detail="Not in confirming state")

    write_status, stored_result = await record_confirmation_result(
        agent_run_id,
        ConfirmationResult.DENIED,
    )
    if write_status == ConfirmationDecisionWriteStatus.CONFLICT:
        raise HTTPException(
            status_code=409,
            detail=_confirmation_conflict_detail(stored_result),
        )

    await redis_service.publish(
        confirmation_channel(agent_run_id),
        "DENIED",
    )
    return {
        "status": "denied",
        "reason": (body.reason if body else None),
    }


@shadow_clone_router.get("/{agent_run_id}/shadow-clone/status")
async def get_shadow_clone_status(
    agent_run_id: str,
    user_id: str = Depends(get_current_user_id_from_jwt),
):
    agent_run = await _get_agent_run_with_access_check(agent_run_id, user_id)
    agent_run, stream_terminal_status, stream_terminal_message = (
        await _project_agent_run_terminal_truth_from_stream(agent_run)
    )
    has_shadow_clone_context = _agent_run_has_shadow_clone_context(agent_run)

    lease = await get_run_sandbox_lease(agent_run_id)
    if _agent_run_is_shadow_clone_v2(agent_run):
        v2_events = await _read_shadow_clone_v2_events(agent_run_id)
        v2_state = _project_shadow_clone_v2_public_state(
            agent_run_id=agent_run_id,
            events=v2_events,
            parent_status=agent_run.get("status"),
        )
        if v2_state is not None:
            response = _attach_file_delivery_source(agent_run, v2_state, lease=lease)
            if response is None:
                return {"status": "not_found"}
            return _attach_public_shadow_clone_contract(response)

    raw_state = await get_state(agent_run_id)

    state: Optional[dict[str, Any]] = None
    if raw_state is not None:
        state = _sanitize_public_shadow_clone_state(agent_run_id, raw_state)
        state = _apply_public_shadow_clone_runtime_failure_truth(state)
        if lease is not None:
            state = _merge_public_shadow_clone_lease_truth(state, lease)
    elif lease is not None and has_shadow_clone_context:
        state = _merge_public_shadow_clone_lease_truth({}, lease)

    if state is not None or lease is not None or has_shadow_clone_context:
        state = _apply_public_shadow_clone_stream_terminal_truth(
            state,
            stream_status=stream_terminal_status,
            message=stream_terminal_message,
        )
    state = _apply_public_shadow_clone_parent_run_truth(
        agent_run,
        state,
        lease=lease,
    )
    if state is not None and _should_refresh_parent_run_truth(
        agent_run,
        state,
        lease=lease,
    ):
        refreshed_agent_run = await _get_agent_run_with_access_check(
            agent_run_id, user_id
        )
        if refreshed_agent_run.get("status") != agent_run.get("status"):
            logger.warning(
                "[ShadowClone] Parent run status changed during public status projection "
                "run_id=%s initial_parent_status=%s refreshed_parent_status=%s "
                "public_status=%s lease_binding_state=%s lease_environment_status=%s",
                agent_run_id,
                agent_run.get("status"),
                refreshed_agent_run.get("status"),
                state.get("status"),
                (lease or {}).get("binding_state"),
                (lease or {}).get("environment_status"),
            )
        agent_run = refreshed_agent_run
        state = _apply_public_shadow_clone_parent_run_truth(
            agent_run,
            state,
            lease=lease,
        )

    final_parent_status = str(agent_run.get("status") or "").strip().lower()
    final_public_status = _get_public_shadow_clone_status(state)
    expected_terminal_status = _PUBLIC_PARENT_RUN_TERMINAL_STATUS_MAP.get(
        final_parent_status
    )
    if (
        state is not None
        and expected_terminal_status is not None
        and final_public_status in _PUBLIC_NONTERMINAL_SHADOW_CLONE_STATUSES
    ):
        logger.error(
            "[ShadowClone] Terminal parent run status still projected as nonterminal public state; "
            "forcing minimal terminal projection run_id=%s parent_status=%s public_status=%s "
            "lease_binding_state=%s lease_environment_status=%s",
            agent_run_id,
            final_parent_status,
            final_public_status,
            (lease or {}).get("binding_state"),
            (lease or {}).get("environment_status"),
        )
        state = _build_minimal_parent_terminal_shadow_state(agent_run)
    if state is None:
        return {"status": "not_found"}
    response = _attach_file_delivery_source(agent_run, state, lease=lease)
    if response is None:
        return {"status": "not_found"}
    return _attach_public_shadow_clone_contract(response)


@shadow_clone_router.get("/{agent_run_id}/shadow-clone/results")
async def get_shadow_clone_results(
    agent_run_id: str,
    user_id: str = Depends(get_current_user_id_from_jwt),
):
    agent_run = await _get_agent_run_with_access_check(agent_run_id, user_id)
    agent_run, _stream_terminal_status, _stream_terminal_message = (
        await _project_agent_run_terminal_truth_from_stream(agent_run)
    )
    if _agent_run_is_shadow_clone_v2(agent_run):
        summaries = _project_shadow_clone_v2_results(
            await _read_shadow_clone_v2_events(agent_run_id)
        )
        return {"results": summaries, "count": len(summaries)}

    from agentscope_integration.shadow_clone.result_store import (
        ensure_terminal_result_summaries_visible,
        read_summaries,
    )

    raw_state = await get_state(agent_run_id)
    terminal_expectations = _build_terminal_result_expectations_from_state(raw_state)
    if terminal_expectations:
        await ensure_terminal_result_summaries_visible(
            agent_run_id,
            terminal_expectations,
        )

    summaries = _reconcile_public_shadow_clone_results(
        await read_summaries(agent_run_id),
        state=raw_state,
        parent_status=agent_run.get("status"),
    )
    return {"results": summaries, "count": len(summaries)}


@shadow_clone_router.get("/{agent_run_id}/shadow-clone/results/{subtask_id}")
async def get_shadow_clone_full_result(
    agent_run_id: str,
    subtask_id: str,
    user_id: str = Depends(get_current_user_id_from_jwt),
):
    agent_run = await _get_agent_run_with_access_check(agent_run_id, user_id)
    if _agent_run_is_shadow_clone_v2(agent_run):
        text = _project_shadow_clone_v2_full_result(
            events=await _read_shadow_clone_v2_events(agent_run_id),
            subtask_id=subtask_id,
        )
        if text is None:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"Result not found for subtask '{subtask_id}'. "
                    "It may have expired or never completed."
                ),
            )
        return {"subtask_id": subtask_id, "result": text}

    from agentscope_integration.shadow_clone.result_store import read_full_result

    text = await read_full_result(agent_run_id, subtask_id)
    if text is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"Result not found for subtask '{subtask_id}'. "
                "It may have expired (24h TTL) or never completed."
            ),
        )
    return {"subtask_id": subtask_id, "result": text}
