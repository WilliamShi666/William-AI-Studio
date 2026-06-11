"""Coordinator orchestration for Shadow Clone execution lifecycle."""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Dict, Iterable, List, Optional, Set
from uuid import uuid4

from services import redis as redis_service
from utils.config import config
from utils.logger import logger

from sandbox.sandbox import resume_or_create_sandbox
from .sandbox_lease import (
    get_run_sandbox_lease,
    keep_run_sandbox_lease_alive,
    require_shadow_clone_sandbox_lease,
    save_run_sandbox_lease,
    sync_run_sandbox_execution_epoch,
    update_run_sandbox_lease,
    set_run_sandbox_binding_state,
)
from sandbox.session_control import clear_shadow_clone_session_state
from .confirmation_hook import wait_for_confirmation
from .constants import (
    ConfirmationResult,
    SHADOW_CLONE_COORDINATOR_RECONCILE_INTERVAL_SECONDS,
    SHADOW_CLONE_CONFIRMATION_TIMEOUT,
    SHADOW_CLONE_ENABLE_PROACTIVE_STANDBY,
    SHADOW_CLONE_RUNTIME_DISPATCH_WINDOW,
    SHADOW_CLONE_RUNTIME_HANDOFF_MAX_CHARS,
    SHADOW_CLONE_RUNTIME_REPLACEMENT_MAX_ATTEMPTS,
    SHADOW_CLONE_RUNTIME_WAKE_MAX_ATTEMPTS,
    SHADOW_CLONE_SUBAGENT_TIMEOUT,
    ShadowCloneMode,
    is_shadow_clone_proactive_standby_enabled,
)
from .dag_executor import topological_sort_layers
from .failure_policy import (
    FAILURE_CLASS_TIMEOUT,
    classify_failure_text,
    is_runtime_recoverable_failure_class,
    is_retryable_failure_class,
)
from .main_agent import MainAgent
from .peer_mailbox import append_supervisor_command
from .result_store import (
    cleanup_results,
    delete_results,
    ensure_terminal_result_summaries_visible,
    read_summaries,
)
from .state_machine import (
    TERMINAL_SHADOW_CLONE_STATUSES,
    bump_execution_epoch,
    cleanup,
    clear_recovery,
    get_execution_epoch,
    get_state,
    init_state,
    initialize_subagent_attempts,
    mark_checkpoint,
    prepare_subagents_for_retry,
    record_recovery_decision,
    record_subagent_failures,
    request_recovery,
    reset_subagents_for_epoch,
    stage_proposal_state,
    transition,
    update_terminal_metadata,
    update_environment,
    update_live_activity,
    update_subagent,
    update_subagent_recovery,
    update_subagent_for_epoch,
)
from .subagent_actor import execute_subagent, run_subagent


SHADOW_CLONE_SUBAGENT_DISPATCH_STAGGER_SECONDS = 0.1
_PROPOSAL_REQUIRED_BUT_MISSING_REASON = "proposal_required_but_missing"
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


def _shadow_clone_lease_keepalive_interval_seconds() -> float:
    raw_value = str(
        os.getenv("SHADOW_CLONE_SANDBOX_LEASE_KEEPALIVE_INTERVAL_SECONDS", "60")
    ).strip()
    try:
        return max(5.0, float(raw_value))
    except (TypeError, ValueError):
        return 60.0


SHADOW_CLONE_LEASE_KEEPALIVE_INTERVAL_SECONDS = (
    _shadow_clone_lease_keepalive_interval_seconds()
)


def _shadow_clone_confirmation_wait_heartbeat_interval_seconds() -> float:
    raw_value = str(
        os.getenv("SHADOW_CLONE_CONFIRMATION_WAIT_HEARTBEAT_INTERVAL_SECONDS", "10")
    ).strip()
    try:
        return max(1.0, float(raw_value))
    except (TypeError, ValueError):
        return 10.0


SHADOW_CLONE_CONFIRMATION_WAIT_HEARTBEAT_INTERVAL_SECONDS = (
    _shadow_clone_confirmation_wait_heartbeat_interval_seconds()
)


def _shadow_clone_subagent_execution_mode() -> str:
    raw_value = os.getenv("SHADOW_CLONE_SUBAGENT_EXECUTION_MODE", "local")
    value = str(raw_value).strip().lower()
    if value == "dramatiq":
        return "dramatiq"
    if value in {"", "local"}:
        return "local"
    logger.warning(
        "Unknown SHADOW_CLONE_SUBAGENT_EXECUTION_MODE=%r; defaulting to local execution",
        raw_value,
    )
    return "local"


def _shadow_clone_subagent_straggler_grace_seconds() -> float:
    raw_value = str(
        os.getenv("SHADOW_CLONE_SUBAGENT_STRAGGLER_GRACE_SECONDS", "45")
    ).strip()
    try:
        return max(0.0, float(raw_value))
    except (TypeError, ValueError):
        return 45.0


def _decode_pubsub_data(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value or "")


def _decode_pubsub_payload(value: Any) -> Dict[str, Any]:
    try:
        payload = json.loads(_decode_pubsub_data(value))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _parse_subagent_update_payload(value: Any) -> Dict[str, Any]:
    payload = value if isinstance(value, dict) else _decode_pubsub_payload(value)
    if not isinstance(payload, dict):
        return {}

    subtask_id = str(payload.get("subtask_id") or "").strip()
    status = str(payload.get("status") or "").strip().lower()
    if not subtask_id or status not in {"completed", "failed"}:
        return {}
    return {
        "subtask_id": subtask_id,
        "status": status,
        "execution_epoch": int(payload.get("execution_epoch") or 0),
        "attempt_index": int(payload.get("attempt_index") or 0) or None,
        "role": str(payload.get("role") or "").strip() or None,
        "result_summary": str(payload.get("result_summary") or "").strip() or None,
        "error": str(payload.get("error") or "").strip() or None,
        "failure_class": str(payload.get("failure_class") or "").strip() or None,
    }


def _parse_subagent_activity_payload(value: Any) -> Optional[Dict[str, Any]]:
    payload = value if isinstance(value, dict) else _decode_pubsub_payload(value)
    if not isinstance(payload, dict):
        return None

    if str(payload.get("event_type") or "").strip().lower() != "activity":
        return None

    subtask_id = str(payload.get("subtask_id") or "").strip()
    if not subtask_id:
        return None
    metadata = payload.get("metadata") or {}
    if not isinstance(metadata, dict):
        metadata = {}

    return {
        "subtask_id": subtask_id,
        "execution_epoch": int(
            payload.get("execution_epoch")
            or metadata.get("shadow_clone_execution_epoch")
            or 0
        ),
        "sequence": payload.get("sequence"),
        "role": payload.get("role"),
        "message_type": payload.get("message_type"),
        "content": payload.get("content") or {},
        "metadata": payload.get("metadata") or {},
        "created_at": payload.get("created_at"),
        "updated_at": payload.get("updated_at"),
    }


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
        if activity_owner == "main_agent" or reason_value in _MAIN_AGENT_CONTINUATION_REASONS:
            return "main_agent_continuation"
        if activity_owner == "shadow_clone":
            return "subagents_running"
    if status_value == "denied" and phase_value == "completed":
        return "completed"
    return None


def _build_shadow_clone_contract(
    *,
    live_activity: Optional[Dict[str, Any]] = None,
    status: Any = None,
    environment_ready: Optional[bool] = None,
    recovery_pending: bool = False,
    ui_phase_override: Optional[str] = None,
    phase_reason: Optional[str] = None,
    activity_owner_override: Optional[str] = None,
) -> Dict[str, Any]:
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
    activity_owner = str(activity_owner_override or "").strip() or _derive_activity_owner(
        scope=scope,
        ui_phase=ui_phase,
        status=status,
    )
    return {
        "activity_owner": activity_owner,
        "ui_phase": ui_phase,
        "phase_reason": resolved_phase_reason,
    }


class ShadowCloneCoordinator:
    """Runs the end-to-end shadow clone flow and emits internal events."""

    def __init__(
        self,
        *,
        thread_id: str,
        project_id: str,
        agent_run_id: str,
        model_key: str,
        db_client,
        mode: ShadowCloneMode,
        subagent_model: Optional[str] = None,
        trace=None,
    ) -> None:
        self.thread_id = thread_id
        self.project_id = project_id
        self.agent_run_id = agent_run_id
        self.model_key = model_key
        self.db_client = db_client
        self.mode = mode
        self.subagent_model = str(subagent_model or "").strip() or None
        self._lf_trace = trace
        self._subagent_execution_mode = _shadow_clone_subagent_execution_mode()
        self._warmup_task: Optional[asyncio.Task[Any]] = None
        self._proposal_state_task: Optional[asyncio.Task[Any]] = None
        self._environment_prepare_task: Optional[asyncio.Task[Any]] = None
        self._bootstrap_clone_task: Optional[asyncio.Task[Any]] = None
        self._active_local_subagent_tasks: Set[asyncio.Task[Any]] = set()
        self.main_agent = MainAgent(
            thread_id=thread_id,
            project_id=project_id,
            agent_run_id=agent_run_id,
            model_key=model_key,
            db_client=db_client,
            mode=mode,
            on_proposal_captured=self._handle_proposal_captured,
            trace=trace,
        )

    def _inject_default_subagent_model(
        self,
        subtasks: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        if not self.subagent_model:
            return subtasks

        normalized_subtasks: List[Dict[str, Any]] = []
        for subtask in subtasks:
            if not isinstance(subtask, dict):
                normalized_subtasks.append(subtask)
                continue

            normalized_subtask = dict(subtask)
            if not str(normalized_subtask.get("model") or "").strip():
                normalized_subtask["model"] = self.subagent_model
            normalized_subtasks.append(normalized_subtask)
        return normalized_subtasks

    def _event_with_contract(
        self,
        event: Dict[str, Any],
        *,
        ui_phase_override: Optional[str] = None,
        phase_reason: Optional[str] = None,
        activity_owner_override: Optional[str] = None,
        environment_ready: Optional[bool] = None,
        recovery_pending: bool = False,
    ) -> Dict[str, Any]:
        contract = _build_shadow_clone_contract(
            live_activity=event.get("live_activity") if isinstance(event, dict) else None,
            status=event.get("status") if isinstance(event, dict) else None,
            environment_ready=environment_ready,
            recovery_pending=recovery_pending,
            ui_phase_override=ui_phase_override,
            phase_reason=phase_reason,
            activity_owner_override=activity_owner_override,
        )
        return {
            **event,
            **{key: value for key, value in contract.items() if value is not None},
        }

    def _route_metadata_with_contract(
        self,
        *,
        shadow_clone_phase: str,
        live_activity: Optional[Dict[str, Any]],
        ui_phase_override: Optional[str] = None,
        phase_reason: Optional[str] = None,
        activity_owner_override: Optional[str] = None,
        environment_ready: Optional[bool] = None,
        recovery_pending: bool = False,
    ) -> Dict[str, Any]:
        contract = _build_shadow_clone_contract(
            live_activity=live_activity,
            environment_ready=environment_ready,
            recovery_pending=recovery_pending,
            ui_phase_override=ui_phase_override,
            phase_reason=phase_reason,
            activity_owner_override=activity_owner_override,
        )
        return {
            "shadow_clone_phase": shadow_clone_phase,
            "live_activity": live_activity,
            **{key: value for key, value in contract.items() if value is not None},
        }

    def _status_event_with_contract(
        self,
        *,
        status: str,
        message: str,
        live_activity: Optional[Dict[str, Any]],
        phase_reason: str,
        ui_phase_override: Optional[str] = None,
        activity_owner_override: Optional[str] = None,
        environment_ready: Optional[bool] = None,
    ) -> Dict[str, Any]:
        return self._event_with_contract(
            {
                "type": "status",
                "status": status,
                "message": message,
                "reason": phase_reason,
                "live_activity": live_activity,
            },
            ui_phase_override=ui_phase_override,
            phase_reason=phase_reason,
            activity_owner_override=activity_owner_override,
            environment_ready=environment_ready,
        )

    async def _set_live_activity(
        self,
        *,
        scope: str,
        phase: str,
        reason: str,
        subtask_id: Optional[str] = None,
        epoch: Optional[int] = None,
    ) -> Dict[str, Any]:
        fallback = {
            "scope": scope,
            "phase": phase,
            "reason": reason,
            "subtask_id": subtask_id,
            "epoch": epoch if epoch is not None else 0,
        }
        try:
            state = await update_live_activity(
                self.agent_run_id,
                scope=scope,
                phase=phase,
                reason=reason,
                subtask_id=subtask_id,
                epoch=epoch,
            )
        except Exception:
            logger.debug(
                "[ShadowClone] live_activity update fell back to in-memory value run_id=%s",
                self.agent_run_id,
                exc_info=True,
            )
            return fallback

        live_activity = state.get("live_activity") if isinstance(state, dict) else None
        if isinstance(live_activity, dict):
            return live_activity
        return fallback

    def _use_local_subagent_execution(self) -> bool:
        return self._subagent_execution_mode != "dramatiq"

    def _discard_local_subagent_task(self, task: asyncio.Task[Any]) -> None:
        self._active_local_subagent_tasks.discard(task)

    def _register_local_subagent_task(self, task: asyncio.Task[Any]) -> None:
        self._active_local_subagent_tasks.add(task)
        task.add_done_callback(self._discard_local_subagent_task)

    def _discard_local_subagent_tasks(
        self,
        tasks: Iterable[asyncio.Task[Any]],
    ) -> None:
        for task in tasks:
            self._active_local_subagent_tasks.discard(task)

    async def _drain_local_subagent_tasks(
        self,
        *,
        tasks: Optional[Iterable[asyncio.Task[Any]]] = None,
        cancel: bool,
        reason: str,
    ) -> None:
        selected_tasks = list(
            tasks if tasks is not None else self._active_local_subagent_tasks
        )
        if not selected_tasks:
            return

        if cancel:
            for task in selected_tasks:
                if not task.done():
                    task.cancel()

        results = await asyncio.gather(*selected_tasks, return_exceptions=True)
        self._discard_local_subagent_tasks(selected_tasks)

        for task, result in zip(selected_tasks, results):
            if result is None or isinstance(result, asyncio.CancelledError):
                continue
            if isinstance(result, Exception):
                logger.warning(
                    "[ShadowClone] Local subagent task finished with error run_id=%s mode=%s reason=%s task=%s error=%s",
                    self.agent_run_id,
                    self._subagent_execution_mode,
                    reason,
                    task.get_name(),
                    result,
                    exc_info=not isinstance(result, RuntimeError),
                )

    async def _record_terminal_metadata(
        self,
        *,
        completion_mode: Optional[str],
        terminal_reason: str,
    ) -> Dict[str, Any]:
        payload = {
            "completion_mode": str(completion_mode or "").strip() or None,
            "terminal_reason": str(terminal_reason or "").strip() or None,
        }
        try:
            state = await update_terminal_metadata(
                self.agent_run_id,
                completion_mode=payload["completion_mode"],
                terminal_reason=payload["terminal_reason"],
            )
        except Exception:
            logger.warning(
                "[ShadowClone] Failed to record terminal metadata run_id=%s completion_mode=%s terminal_reason=%s",
                self.agent_run_id,
                payload["completion_mode"],
                payload["terminal_reason"],
                exc_info=True,
            )
            return payload

        if isinstance(state, dict):
            return {
                "completion_mode": state.get("completion_mode"),
                "terminal_reason": state.get("terminal_reason"),
            }
        return payload

    async def _ensure_terminal_results_visible(
        self,
        terminal_events: List[Dict[str, Any]],
    ) -> None:
        expected_results: List[Dict[str, Any]] = []
        for event in terminal_events:
            if not isinstance(event, dict):
                continue
            event_type = str(event.get("type") or "").strip()
            if event_type not in {"subagent_completed", "subagent_failed"}:
                continue
            subtask_id = str(event.get("subtask_id") or "").strip()
            if not subtask_id:
                continue
            expected_results.append(
                {
                    "subtask_id": subtask_id,
                    "role": str(event.get("role") or "").strip() or None,
                    "status": "failed" if event_type == "subagent_failed" else "completed",
                    "summary": (
                        str(event.get("result_summary") or "").strip()
                        or str(event.get("error") or "").strip()
                        or None
                    ),
                    "attempt_index": (
                        int(event.get("attempt_index"))
                        if event.get("attempt_index") is not None
                        else None
                    ),
                    "failure_class": (
                        str(event.get("failure_class") or "").strip() or None
                    ),
                }
            )

        if not expected_results:
            return
        await ensure_terminal_result_summaries_visible(
            self.agent_run_id,
            expected_results,
        )

    async def _finalize_layer_tasks(
        self,
        tasks: List[asyncio.Task[Any]],
        *,
        recovery_triggered: bool,
    ) -> None:
        if not tasks:
            return

        await self._drain_local_subagent_tasks(
            tasks=tasks,
            cancel=recovery_triggered,
            reason="layer_finalize",
        )

    @staticmethod
    def _truncate_runtime_handoff(value: Any) -> str:
        text = str(value or "").strip()
        if len(text) <= SHADOW_CLONE_RUNTIME_HANDOFF_MAX_CHARS:
            return text
        return text[:SHADOW_CLONE_RUNTIME_HANDOFF_MAX_CHARS].rstrip() + "..."

    def _build_runtime_recovery_handoff(
        self,
        *,
        subtask: Dict[str, Any],
        failure_event: Dict[str, Any],
        subagent_state: Optional[Dict[str, Any]] = None,
    ) -> str:
        role = str(
            failure_event.get("role")
            or (subagent_state or {}).get("role")
            or subtask.get("role")
            or "specialist",
        ).strip()
        task_description = str(subtask.get("task_description") or "").strip()
        failure_class = str(
            failure_event.get("failure_class")
            or (subagent_state or {}).get("failure_class")
            or "",
        ).strip()
        attempt_index = max(
            1,
            int(
                failure_event.get("attempt_index")
                or (subagent_state or {}).get("attempt_index")
                or 1
            ),
        )
        result_summary = str((subagent_state or {}).get("result_summary") or "").strip()
        failure_message = str(
            failure_event.get("error")
            or (subagent_state or {}).get("last_error")
            or result_summary
            or "Shadow Clone subagent failed before completion."
        ).strip()

        parts = [
            f"Assigned role: {role}",
            "Assigned task:",
            task_description or "No task description was persisted.",
            f"Previous attempt index: {attempt_index}",
            (
                f"Failure class: {failure_class}"
                if failure_class
                else "Failure class: unknown"
            ),
            "Latest failure:",
            failure_message,
        ]
        if result_summary and result_summary != failure_message:
            parts.extend(
                [
                    "Latest partial summary:",
                    result_summary,
                ]
            )
        parts.extend(
            [
                "Takeover instruction:",
                "Continue from the preserved context and partial work above. "
                "Do not restart the overall task unless the preserved context is unusable.",
            ]
        )
        return self._truncate_runtime_handoff("\n".join(part for part in parts if part))

    async def _dispatch_subagent_attempt(
        self,
        *,
        subtask: Dict[str, Any],
        layer_index: int,
        execution_epoch: int,
        layer_tasks: Optional[List[asyncio.Task[Any]]],
        live_activity: Optional[Dict[str, Any]] = None,
        shadow_thread_run_id: Optional[str] = None,
        shadow_recovery_mode: Optional[str] = None,
        shadow_recovery_reason: Optional[str] = None,
        shadow_recovery_handoff_summary: Optional[str] = None,
    ) -> Dict[str, Any]:
        state_snapshot = await get_state(self.agent_run_id) or {}
        subagent_snapshot = state_snapshot.get("subagents") or {}
        if not isinstance(subagent_snapshot, dict):
            subagent_snapshot = {}

        subtask_id = str(subtask.get("id") or "").strip()
        subtask_state = (
            subagent_snapshot.get(subtask_id)
            if isinstance(subagent_snapshot.get(subtask_id), dict)
            else {}
        )
        attempt_index = max(
            1,
            int((subtask_state or {}).get("attempt_index") or 1),
        )
        subtask_with_layer = {
            **subtask,
            "layer_index": layer_index,
            "execution_epoch": execution_epoch,
            "attempt_index": attempt_index,
        }
        if shadow_thread_run_id:
            subtask_with_layer["shadow_thread_run_id"] = shadow_thread_run_id
        if shadow_recovery_mode:
            subtask_with_layer["shadow_recovery_mode"] = shadow_recovery_mode
        if shadow_recovery_reason:
            subtask_with_layer["shadow_recovery_reason"] = shadow_recovery_reason
        if shadow_recovery_handoff_summary:
            subtask_with_layer["shadow_recovery_handoff_summary"] = (
                shadow_recovery_handoff_summary
            )

        if self._use_local_subagent_execution():
            task = asyncio.create_task(
                execute_subagent(
                    parent_run_id=self.agent_run_id,
                    subtask_id=subtask_id,
                    subtask_config=subtask_with_layer,
                    project_id=self.project_id,
                    thread_id=self.thread_id,
                    initialize_db=True,
                ),
                name=f"shadow-clone-subagent:{self.agent_run_id}:{subtask_id}",
            )
            self._register_local_subagent_task(task)
            if layer_tasks is not None:
                layer_tasks.append(task)
        else:
            run_subagent.send(
                parent_run_id=self.agent_run_id,
                subtask_id=subtask_id,
                subtask_config=subtask_with_layer,
                project_id=self.project_id,
                thread_id=self.thread_id,
                langfuse_trace_id=self._lf_trace.id if self._lf_trace else "",
            )

        return {
            "type": "subagent_started",
            "subtask_id": subtask_id,
            "role": subtask.get("role"),
            "attempt_index": attempt_index,
            "live_activity": live_activity,
        }

    async def _maybe_schedule_runtime_recovery(
        self,
        *,
        subtask: Dict[str, Any],
        failure_event: Dict[str, Any],
        layer_index: int,
        execution_epoch: int,
        layer_tasks: Optional[List[asyncio.Task[Any]]],
    ) -> List[Dict[str, Any]]:
        subtask_id = str(subtask.get("id") or failure_event.get("subtask_id") or "").strip()
        if not subtask_id:
            return []

        state = await get_state(self.agent_run_id) or {}
        subagents = state.get("subagents") or {}
        subagent_state = (
            subagents.get(subtask_id)
            if isinstance(subagents, dict) and isinstance(subagents.get(subtask_id), dict)
            else {}
        )
        failure_message = str(
            failure_event.get("error")
            or subagent_state.get("last_error")
            or subagent_state.get("result_summary")
            or "Shadow Clone subagent failed before completion."
        ).strip()
        failure_class = str(
            failure_event.get("failure_class")
            or subagent_state.get("failure_class")
            or classify_failure_text(failure_message)
            or ""
        ).strip()
        role = str(
            failure_event.get("role")
            or subagent_state.get("role")
            or subtask.get("role")
            or ""
        ).strip()
        recovery_state = (
            subagent_state.get("recovery")
            if isinstance(subagent_state.get("recovery"), dict)
            else {}
        )
        wake_attempts = max(0, int(recovery_state.get("wake_attempts") or 0))
        replacement_attempts = max(
            0,
            int(recovery_state.get("replacement_attempts") or 0),
        )
        current_phase = str(recovery_state.get("phase") or "").strip().lower()
        current_attempt_index = max(
            1,
            int(
                failure_event.get("attempt_index")
                or subagent_state.get("attempt_index")
                or 1
            ),
        )

        if not is_runtime_recoverable_failure_class(failure_class):
            await update_subagent_recovery(
                self.agent_run_id,
                subtask_id,
                expected_epoch=execution_epoch,
                mode="wake_first",
                phase="runtime_recovery_skipped",
                reason=failure_message,
            )
            return []

        await record_subagent_failures(
            self.agent_run_id,
            failed_subtasks=[
                {
                    "subtask_id": subtask_id,
                    "role": role,
                    "error": failure_message,
                    "failure_class": failure_class,
                    "attempt_index": current_attempt_index,
                }
            ],
        )

        if current_phase == "wake_dispatched":
            await update_subagent_recovery(
                self.agent_run_id,
                subtask_id,
                expected_epoch=execution_epoch,
                phase="wake_failed",
                reason=failure_message,
            )
        elif current_phase == "replacement_started":
            await update_subagent_recovery(
                self.agent_run_id,
                subtask_id,
                expected_epoch=execution_epoch,
                phase="replacement_failed",
                reason=failure_message,
            )

        try:
            if wake_attempts < SHADOW_CLONE_RUNTIME_WAKE_MAX_ATTEMPTS:
                command_id = uuid4().hex
                recovering_live = await self._set_live_activity(
                    scope="shadow_clone_main",
                    phase="execution",
                    reason="subagent_recovering",
                    subtask_id=subtask_id,
                    epoch=execution_epoch,
                )
                await update_subagent_recovery(
                    self.agent_run_id,
                    subtask_id,
                    expected_epoch=execution_epoch,
                    mode="wake_first",
                    phase="wake_requested",
                    reason=failure_message,
                    wake_attempts=wake_attempts + 1,
                    last_command_id=command_id,
                )
                await append_supervisor_command(
                    run_id=self.agent_run_id,
                    recipient_subtask_id=subtask_id,
                    recipient_role=role,
                    summary=(
                        "Resume your current subtask using the context already stored in memory. "
                        "Continue from where the previous attempt stopped."
                    ),
                    details=(
                        "Use the previous attempt's memory and partial work before redoing anything. "
                        "If you are still blocked, briefly summarize the blocker and then continue."
                    ),
                    command_type="wake",
                    attempt_index=current_attempt_index + 1,
                    execution_epoch=execution_epoch,
                    reason=failure_message,
                )
                await delete_results(self.agent_run_id, [subtask_id])
                await prepare_subagents_for_retry(
                    self.agent_run_id,
                    [subtask_id],
                    expected_epoch=execution_epoch,
                    roles={subtask_id: role},
                )
                await update_subagent_recovery(
                    self.agent_run_id,
                    subtask_id,
                    expected_epoch=execution_epoch,
                    mode="wake_first",
                    phase="wake_dispatched",
                    reason=failure_message,
                    wake_attempts=wake_attempts + 1,
                    last_command_id=command_id,
                )
                wake_live = await self._set_live_activity(
                    scope="shadow_clone_main",
                    phase="execution",
                    reason="subagent_wake_sent",
                    subtask_id=subtask_id,
                    epoch=execution_epoch,
                )
                resumed_live = await self._set_live_activity(
                    scope="shadow_clone_main",
                    phase="execution",
                    reason="subagent_resumed",
                    subtask_id=subtask_id,
                    epoch=execution_epoch,
                )
                started_event = await self._dispatch_subagent_attempt(
                    subtask=subtask,
                    layer_index=layer_index,
                    execution_epoch=execution_epoch,
                    layer_tasks=layer_tasks,
                    live_activity=resumed_live,
                    shadow_recovery_mode="wake_first",
                    shadow_recovery_reason=failure_message,
                    shadow_recovery_handoff_summary=(
                        "Continue this subtask from the existing context already stored in memory."
                    ),
                )
                return [
                    {
                        "type": "shadow_clone_subagent_recovering",
                        "subtask_id": subtask_id,
                        "role": role,
                        "attempt_index": current_attempt_index,
                        "failure_class": failure_class,
                        "message": failure_message,
                        "recovery_mode": "wake_first",
                        "recovery_phase": "wake_requested",
                        "live_activity": recovering_live,
                    },
                    {
                        "type": "shadow_clone_subagent_wake_sent",
                        "subtask_id": subtask_id,
                        "role": role,
                        "attempt_index": started_event.get("attempt_index"),
                        "failure_class": failure_class,
                        "message": failure_message,
                        "recovery_mode": "wake_first",
                        "recovery_phase": "wake_dispatched",
                        "live_activity": wake_live,
                    },
                    {
                        "type": "shadow_clone_subagent_resumed",
                        "subtask_id": subtask_id,
                        "role": role,
                        "attempt_index": started_event.get("attempt_index"),
                        "failure_class": failure_class,
                        "message": failure_message,
                        "recovery_mode": "wake_first",
                        "recovery_phase": "wake_dispatched",
                        "live_activity": resumed_live,
                    },
                    started_event,
                ]

            if replacement_attempts < SHADOW_CLONE_RUNTIME_REPLACEMENT_MAX_ATTEMPTS:
                replacement_context_id = uuid4().hex[:8]
                command_id = uuid4().hex
                handoff_summary = self._build_runtime_recovery_handoff(
                    subtask=subtask,
                    failure_event=failure_event,
                    subagent_state=subagent_state,
                )
                recovering_live = await self._set_live_activity(
                    scope="shadow_clone_main",
                    phase="execution",
                    reason="subagent_recovering",
                    subtask_id=subtask_id,
                    epoch=execution_epoch,
                )
                await update_subagent_recovery(
                    self.agent_run_id,
                    subtask_id,
                    expected_epoch=execution_epoch,
                    mode="replacement",
                    phase="replacement_requested",
                    reason=failure_message,
                    replacement_attempts=replacement_attempts + 1,
                    replacement_context_id=replacement_context_id,
                    handoff_summary=handoff_summary,
                    last_command_id=command_id,
                )
                await append_supervisor_command(
                    run_id=self.agent_run_id,
                    recipient_subtask_id=subtask_id,
                    recipient_role=role,
                    summary=(
                        "You are taking over this subtask after an earlier attempt failed. "
                        "Use the handoff below to continue the work."
                    ),
                    details=handoff_summary,
                    command_type="replacement_handoff",
                    attempt_index=current_attempt_index + 1,
                    execution_epoch=execution_epoch,
                    replacement_for=subtask_id,
                    reason=failure_message,
                )
                await delete_results(self.agent_run_id, [subtask_id])
                await prepare_subagents_for_retry(
                    self.agent_run_id,
                    [subtask_id],
                    expected_epoch=execution_epoch,
                    roles={subtask_id: role},
                )
                await update_subagent_recovery(
                    self.agent_run_id,
                    subtask_id,
                    expected_epoch=execution_epoch,
                    mode="replacement",
                    phase="replacement_started",
                    reason=failure_message,
                    replacement_attempts=replacement_attempts + 1,
                    replacement_context_id=replacement_context_id,
                    handoff_summary=handoff_summary,
                    last_command_id=command_id,
                )
                replacement_live = await self._set_live_activity(
                    scope="shadow_clone_main",
                    phase="execution",
                    reason="subagent_replacement_started",
                    subtask_id=subtask_id,
                    epoch=execution_epoch,
                )
                started_event = await self._dispatch_subagent_attempt(
                    subtask=subtask,
                    layer_index=layer_index,
                    execution_epoch=execution_epoch,
                    layer_tasks=layer_tasks,
                    live_activity=replacement_live,
                    shadow_thread_run_id=(
                        f"shadow:{self.agent_run_id}:{subtask_id}:replacement:"
                        f"{replacement_context_id}"
                    ),
                    shadow_recovery_mode="replacement",
                    shadow_recovery_reason=failure_message,
                    shadow_recovery_handoff_summary=handoff_summary,
                )
                return [
                    {
                        "type": "shadow_clone_subagent_recovering",
                        "subtask_id": subtask_id,
                        "role": role,
                        "attempt_index": current_attempt_index,
                        "failure_class": failure_class,
                        "message": failure_message,
                        "recovery_mode": "replacement",
                        "recovery_phase": "replacement_requested",
                        "handoff_summary": handoff_summary,
                        "replacement_context_id": replacement_context_id,
                        "live_activity": recovering_live,
                    },
                    {
                        "type": "shadow_clone_subagent_replacement_started",
                        "subtask_id": subtask_id,
                        "role": role,
                        "attempt_index": started_event.get("attempt_index"),
                        "failure_class": failure_class,
                        "message": failure_message,
                        "recovery_mode": "replacement",
                        "recovery_phase": "replacement_started",
                        "handoff_summary": handoff_summary,
                        "replacement_context_id": replacement_context_id,
                        "live_activity": replacement_live,
                    },
                    started_event,
                ]
        except Exception as recovery_error:
            logger.warning(
                "[ShadowClone] Runtime subagent recovery failed run_id=%s subtask_id=%s error=%s",
                self.agent_run_id,
                subtask_id,
                recovery_error,
                exc_info=True,
            )
            await update_subagent_recovery(
                self.agent_run_id,
                subtask_id,
                expected_epoch=execution_epoch,
                phase="runtime_recovery_error",
                reason=str(recovery_error),
            )
            return []

        exhausted_live = await self._set_live_activity(
            scope="shadow_clone_main",
            phase="execution",
            reason="subagent_recovery_exhausted",
            subtask_id=subtask_id,
            epoch=execution_epoch,
        )
        await update_subagent_recovery(
            self.agent_run_id,
            subtask_id,
            expected_epoch=execution_epoch,
            phase="recovery_exhausted",
            reason=failure_message,
        )
        return [
            {
                "type": "shadow_clone_subagent_recovery_exhausted",
                "subtask_id": subtask_id,
                "role": role,
                "attempt_index": current_attempt_index,
                "failure_class": failure_class,
                "message": failure_message,
                "recovery_phase": "recovery_exhausted",
                "live_activity": exhausted_live,
            }
        ]

    async def close(self) -> None:
        try:
            await self._drain_local_subagent_tasks(
                cancel=True,
                reason="coordinator_close",
            )
            await self._cancel_background_startup_tasks()
            await self.main_agent.close()
        finally:
            clear_shadow_clone_session_state(self.agent_run_id)

    def _proactive_standby_enabled(self) -> bool:
        return bool(
            is_shadow_clone_proactive_standby_enabled()
            if callable(is_shadow_clone_proactive_standby_enabled)
            else SHADOW_CLONE_ENABLE_PROACTIVE_STANDBY
        )

    def _ensure_warmup_started(self) -> asyncio.Task[Any]:
        task = self._warmup_task
        if task is None or task.cancelled():
            task = asyncio.create_task(
                self._warmup_project_sandbox(),
                name=f"shadow-clone-warmup:{self.agent_run_id}",
            )
            self._warmup_task = task
        return task

    async def _await_warmup_sandbox(self) -> Any:
        task = self._warmup_task
        if task is None or task.cancelled():
            task = self._ensure_warmup_started()
        return await task

    async def _stage_proposal_state_once(
        self,
        *,
        subtasks: List[Dict[str, Any]],
        dependencies: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        return await stage_proposal_state(
            self.agent_run_id,
            total=len(subtasks),
            subtasks=subtasks,
            dependencies=dependencies,
        )

    def _ensure_proposal_state_started(
        self,
        proposal: Optional[Dict[str, Any]],
    ) -> asyncio.Task[Any]:
        task = self._proposal_state_task
        if task is None or task.cancelled():
            safe_proposal = proposal if isinstance(proposal, dict) else {}
            subtasks = self._inject_default_subagent_model(
                safe_proposal.get("subtasks") or [],
            )
            dependencies = safe_proposal.get("dependencies") or []
            task = asyncio.create_task(
                self._stage_proposal_state_once(
                    subtasks=subtasks,
                    dependencies=dependencies,
                ),
                name=f"shadow-clone-proposal-state:{self.agent_run_id}",
            )
            self._proposal_state_task = task
        return task

    async def _await_proposal_state_staged(
        self,
        proposal: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        task = self._proposal_state_task
        if task is None or task.cancelled():
            if proposal is None:
                return None
            task = self._ensure_proposal_state_started(proposal)
        return await task

    def _require_strict_lease(
        self,
        lease: Optional[Dict[str, Any]],
        *,
        context: str,
        expected_sandbox_id: Optional[str] = None,
        expected_environment_status: Optional[str] = None,
        expected_environment_ready: Optional[bool] = None,
        expected_execution_epoch: Optional[int] = None,
        require_environment_manifest_consistency: bool = False,
    ) -> Dict[str, Any]:
        try:
            return require_shadow_clone_sandbox_lease(
                lease,
                run_id=self.agent_run_id,
                expected_project_id=self.project_id,
                require_sandbox_id=True,
                expected_sandbox_id=expected_sandbox_id,
                expected_environment_status=expected_environment_status,
                expected_environment_ready=expected_environment_ready,
                expected_execution_epoch=expected_execution_epoch,
                require_environment_manifest_consistency=require_environment_manifest_consistency,
            )
        except RuntimeError as exc:
            raise RuntimeError(f"{context}: {exc}") from exc

    async def _read_required_strict_lease(
        self,
        *,
        context: str,
        expected_sandbox_id: Optional[str] = None,
        expected_environment_status: Optional[str] = None,
        expected_environment_ready: Optional[bool] = None,
        expected_execution_epoch: Optional[int] = None,
        require_environment_manifest_consistency: bool = False,
    ) -> Dict[str, Any]:
        try:
            return require_shadow_clone_sandbox_lease(
                await get_run_sandbox_lease(self.agent_run_id),
                run_id=self.agent_run_id,
                expected_project_id=self.project_id,
                require_sandbox_id=True,
                expected_sandbox_id=expected_sandbox_id,
                expected_environment_status=expected_environment_status,
                expected_environment_ready=expected_environment_ready,
                expected_execution_epoch=expected_execution_epoch,
                require_environment_manifest_consistency=require_environment_manifest_consistency,
            )
        except RuntimeError as exc:
            raise RuntimeError(f"{context}: {exc}") from exc

    async def _keep_strict_lease_alive(self, *, context: str) -> Dict[str, Any]:
        lease = await keep_run_sandbox_lease_alive(self.agent_run_id)
        return self._require_strict_lease(
            lease,
            context=context,
        )

    def _handle_proposal_captured(self, _proposal: Dict[str, Any]) -> None:
        self._ensure_proposal_state_started(_proposal)
        self._ensure_warmup_started()
        self._ensure_environment_prepare_started()
        if self._proactive_standby_enabled():
            self._ensure_bootstrap_clone_started()

    def _ensure_environment_prepare_started(self) -> asyncio.Task[Any]:
        task = self._environment_prepare_task
        if task is None or task.cancelled():
            task = asyncio.create_task(
                self._prepare_shadow_clone_environment_once(),
                name=f"shadow-clone-environment-prepare:{self.agent_run_id}",
            )
            self._environment_prepare_task = task
        return task

    async def _await_environment_prepare(self) -> Dict[str, Any]:
        task = self._environment_prepare_task
        if task is None or task.cancelled():
            task = self._ensure_environment_prepare_started()
        return await task

    def _ensure_bootstrap_clone_started(self) -> asyncio.Task[Any]:
        if not self._proactive_standby_enabled():
            task = self._bootstrap_clone_task
            if task is None or task.cancelled():
                task = asyncio.create_task(
                    asyncio.sleep(0),
                    name=f"shadow-clone-bootstrap-clone-disabled:{self.agent_run_id}",
                )
                self._bootstrap_clone_task = task
            return task
        task = self._bootstrap_clone_task
        if task is None or task.cancelled():
            task = asyncio.create_task(
                self._bootstrap_clone_after_environment_prepare(),
                name=f"shadow-clone-bootstrap-clone:{self.agent_run_id}",
            )
            self._bootstrap_clone_task = task
        return task

    async def _bootstrap_clone_after_environment_prepare(self) -> None:
        if not self._proactive_standby_enabled():
            return
        try:
            await self._await_environment_prepare()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "[ShadowClone] Early standby clone bootstrap skipped after environment prepare failure "
                "run_id=%s: %s",
                self.agent_run_id,
                exc,
            )
            return
        await self._ensure_bootstrap_standby_clone()

    async def _await_bootstrap_clone(self) -> None:
        if not self._proactive_standby_enabled():
            return
        task = self._bootstrap_clone_task
        if task is None or task.cancelled():
            await self._ensure_bootstrap_standby_clone()
            return
        await task

    async def _cancel_background_task(self, attr_name: str, *, label: str) -> None:
        task = getattr(self, attr_name, None)
        if task is None:
            return
        try:
            if not task.done():
                task.cancel()
            await task
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.warning(
                "[ShadowClone] Background %s task ended with error during cleanup "
                "run_id=%s: %s",
                label,
                self.agent_run_id,
                exc,
            )
        finally:
            setattr(self, attr_name, None)

    async def _cancel_background_startup_tasks(self) -> None:
        await self._cancel_background_task(
            "_bootstrap_clone_task",
            label="bootstrap clone",
        )
        await self._cancel_background_task(
            "_environment_prepare_task",
            label="environment prepare",
        )
        await self._cancel_background_task(
            "_proposal_state_task",
            label="proposal state",
        )
        await self._cancel_background_task(
            "_warmup_task",
            label="warmup",
        )

    async def _clear_bootstrap_standby_clone_metadata(self) -> None:
        lease = await get_run_sandbox_lease(self.agent_run_id)
        if lease is None:
            return
        if not str(lease.get("standby_sandbox_id") or "").strip() and not str(
            lease.get("last_clone_error") or ""
        ).strip():
            return
        await update_run_sandbox_lease(
            self.agent_run_id,
            standby_sandbox_id=None,
            standby_sandbox_type=None,
            standby_sandbox_info={},
            standby_snapshot_template_id=None,
            standby_source_sandbox_id=None,
            standby_created_at=None,
            last_clone_error=None,
        )

    async def _cleanup_speculative_startup(
        self,
        *,
        release_lease: bool = False,
        last_error: Optional[str] = None,
        clear_standby: bool = False,
    ) -> None:
        await self._cancel_background_startup_tasks()
        if clear_standby:
            await self._clear_bootstrap_standby_clone_metadata()
        if not release_lease:
            return
        lease = await get_run_sandbox_lease(self.agent_run_id)
        if lease is None:
            return
        if str(lease.get("binding_state") or "").strip().lower() == "lost":
            return
        await set_run_sandbox_binding_state(
            self.agent_run_id,
            "released",
            last_error=last_error,
        )

    async def _warmup_project_sandbox(self) -> None:
        from agentscope_integration.adapters.thread_manager_adapter import ThreadManagerAdapter
        from sandbox.tool_base import SandboxToolsBase

        existing_lease = await get_run_sandbox_lease(self.agent_run_id)
        if existing_lease is not None:
            if (
                not self._proactive_standby_enabled()
                and str(existing_lease.get("standby_sandbox_id") or "").strip()
            ):
                await self._clear_bootstrap_standby_clone_metadata()
                existing_lease = await get_run_sandbox_lease(self.agent_run_id)
            return self._require_strict_lease(
                existing_lease,
                context="Shadow Clone warmup found an invalid existing strict sandbox lease",
            )

        strict_sandbox_type = config.get_shadow_clone_sandbox_type()
        thread_manager = ThreadManagerAdapter(db_client=self.db_client)
        sandbox_warmup = SandboxToolsBase(
            project_id=self.project_id,
            thread_manager=thread_manager,
            sandbox_type=strict_sandbox_type,
        )
        await sandbox_warmup._ensure_sandbox()
        client = await thread_manager.db.client
        sandbox_info = await sandbox_warmup._load_project_sandbox_info(client)
        lease = await save_run_sandbox_lease(
            self.agent_run_id,
            project_id=self.project_id,
            thread_id=self.thread_id,
            sandbox_id=str(sandbox_info.get("id") or sandbox_warmup._sandbox_id or ""),
            sandbox_type=str(sandbox_info.get("type") or sandbox_warmup.sandbox_type or "desktop"),
            sandbox_info=sandbox_info,
            binding_state="locked",
            source="shadow_clone_warmup",
        )
        logger.info(
            "[ShadowClone] Bound strict sandbox lease for project=%s run_id=%s sandbox_id=%s sandbox_type=%s",
            self.project_id,
            self.agent_run_id,
            sandbox_warmup._sandbox_id,
            strict_sandbox_type,
        )
        return await self._read_required_strict_lease(
            context="Shadow Clone warmup failed to reread the canonical strict sandbox lease",
            expected_sandbox_id=str(lease.get("sandbox_id") or "").strip() or None,
        )

    async def _bootstrap_shadow_clone_environment(
        self,
        *,
        lease: Optional[Dict[str, Any]] = None,
        execution_epoch: int = 0,
    ) -> Dict[str, Any]:
        manifest = await self.main_agent.prepare_shadow_clone_environment_commit(
            execution_epoch=execution_epoch,
        )
        refreshed_lease = await get_run_sandbox_lease(self.agent_run_id) or lease or {}
        manifest_sandbox = manifest.get("sandbox") or {}
        if not isinstance(manifest_sandbox, dict):
            manifest_sandbox = {}

        sandbox_id = str(
            manifest_sandbox.get("id")
            or refreshed_lease.get("sandbox_id")
            or ""
        ).strip()
        sandbox_type = str(
            manifest_sandbox.get("type")
            or refreshed_lease.get("sandbox_type")
            or "desktop"
        ).strip() or "desktop"

        return {
            **manifest,
            "subagent_execution_mode": self._subagent_execution_mode,
            "sandbox": {
                **manifest_sandbox,
                "id": sandbox_id,
                "type": sandbox_type,
                "binding_state": str(
                    manifest_sandbox.get("binding_state")
                    or refreshed_lease.get("binding_state")
                    or ""
                ),
            },
        }

    async def _prepare_shadow_clone_environment(self) -> Dict[str, Any]:
        return await self._await_environment_prepare()

    async def _stream_confirmation_wait_with_lease_keepalive(
        self,
        *,
        live_activity: Optional[Dict[str, Any]],
    ) -> AsyncGenerator[Dict[str, Any], None]:
        confirmation_task = asyncio.create_task(
            wait_for_confirmation(
                self.agent_run_id,
                timeout=SHADOW_CLONE_CONFIRMATION_TIMEOUT,
            ),
            name=f"shadow-clone-confirmation:{self.agent_run_id}",
        )
        heartbeat_interval = max(
            0.01,
            float(SHADOW_CLONE_CONFIRMATION_WAIT_HEARTBEAT_INTERVAL_SECONDS),
        )
        next_heartbeat_at = time.monotonic() + heartbeat_interval
        next_keepalive_at = (
            time.monotonic() + SHADOW_CLONE_LEASE_KEEPALIVE_INTERVAL_SECONDS
        )
        try:
            while True:
                now = time.monotonic()
                timeout = max(
                    0.01,
                    min(next_heartbeat_at - now, next_keepalive_at - now),
                )
                try:
                    result = await asyncio.wait_for(
                        asyncio.shield(confirmation_task),
                        timeout=timeout,
                    )
                    yield {
                        "type": "__confirmation_result__",
                        "result": result,
                    }
                    return
                except asyncio.TimeoutError:
                    now = time.monotonic()
                    if now >= next_keepalive_at:
                        await self._keep_strict_lease_alive(
                            context=(
                                "Shadow Clone confirmation wait lost the strict sandbox lease before confirmation"
                            ),
                        )
                        next_keepalive_at = (
                            now + SHADOW_CLONE_LEASE_KEEPALIVE_INTERVAL_SECONDS
                        )
                    if now >= next_heartbeat_at:
                        yield self._status_event_with_contract(
                            status="confirming",
                            message="Waiting for confirmation",
                            live_activity=live_activity,
                            phase_reason="confirmation_waiting",
                            activity_owner_override="shadow_clone",
                        )
                        next_heartbeat_at = (
                            now
                            + SHADOW_CLONE_CONFIRMATION_WAIT_HEARTBEAT_INTERVAL_SECONDS
                        )
        finally:
            if not confirmation_task.done():
                confirmation_task.cancel()
                try:
                    await confirmation_task
                except asyncio.CancelledError:
                    pass

    async def _prepare_shadow_clone_environment_once(self) -> Dict[str, Any]:
        await self._await_proposal_state_staged()
        await update_environment(
            self.agent_run_id,
            status="preparing",
            ready=False,
            last_error=None,
            manifest={
                "subagent_execution_mode": self._subagent_execution_mode,
            },
        )

        lease = self._require_strict_lease(
            await self._await_warmup_sandbox(),
            context="Shadow Clone environment prepare started without a valid strict sandbox lease",
        )
        lease_sandbox_id = str(lease.get("sandbox_id") or "").strip() or None
        execution_epoch = await get_execution_epoch(self.agent_run_id)
        synced_lease = await sync_run_sandbox_execution_epoch(
            self.agent_run_id,
            execution_epoch,
        )
        lease = self._require_strict_lease(
            synced_lease,
            context="Shadow Clone environment prepare failed to sync strict sandbox lease epoch",
            expected_sandbox_id=lease_sandbox_id,
            expected_execution_epoch=execution_epoch,
        )
        preparing_lease = await update_run_sandbox_lease(
            self.agent_run_id,
            environment_status="preparing",
            environment_ready=False,
            last_error=None,
        )
        lease = self._require_strict_lease(
            preparing_lease,
            context="Shadow Clone environment prepare failed to mark the strict sandbox lease preparing",
            expected_sandbox_id=lease_sandbox_id,
            expected_environment_status="preparing",
            expected_environment_ready=False,
            expected_execution_epoch=execution_epoch,
        )

        manifest = await self._bootstrap_shadow_clone_environment(
            lease=lease,
            execution_epoch=execution_epoch,
        )
        prepared_at = str(manifest.get("prepared_at") or "").strip() or datetime.now(
            timezone.utc
        ).isoformat()
        manifest = {
            **manifest,
            "prepared_at": prepared_at,
            "execution_epoch": execution_epoch,
            "confirmation_ready": True,
        }
        manifest_sandbox = manifest.get("sandbox") or {}
        if not isinstance(manifest_sandbox, dict):
            manifest_sandbox = {}

        expected_manifest_sandbox_id = (
            str(manifest_sandbox.get("id") or "").strip()
            or lease_sandbox_id
        )
        ready_lease = await update_run_sandbox_lease(
            self.agent_run_id,
            environment_status="ready",
            environment_ready=True,
            environment_prepared_at=prepared_at,
            environment_manifest=manifest,
            last_error=None,
        )
        self._require_strict_lease(
            ready_lease,
            context="Shadow Clone environment prepare failed to mark the strict sandbox lease ready",
            expected_sandbox_id=expected_manifest_sandbox_id,
            expected_environment_status="ready",
            expected_environment_ready=True,
            expected_execution_epoch=execution_epoch,
            require_environment_manifest_consistency=True,
        )
        canonical_ready_lease = await self._read_required_strict_lease(
            context="Shadow Clone environment prepare could not confirm canonical strict lease truth",
            expected_sandbox_id=expected_manifest_sandbox_id,
            expected_environment_status="ready",
            expected_environment_ready=True,
            expected_execution_epoch=execution_epoch,
            require_environment_manifest_consistency=True,
        )
        canonical_manifest = canonical_ready_lease.get("environment_manifest") or {}
        if not isinstance(canonical_manifest, dict):
            canonical_manifest = dict(manifest)
        prepared_at = (
            str(canonical_ready_lease.get("environment_prepared_at") or "").strip()
            or prepared_at
        )
        await update_environment(
            self.agent_run_id,
            status="ready",
            ready=True,
            last_error=None,
            prepared_at=prepared_at,
            manifest=canonical_manifest,
        )
        return canonical_manifest

    async def _refresh_environment_if_needed(self) -> tuple[Optional[str], Optional[Dict[str, Any]]]:
        execution_epoch = await get_execution_epoch(self.agent_run_id)
        lease = await get_run_sandbox_lease(self.agent_run_id) or {}
        manifest = lease.get("environment_manifest") or {}
        if not isinstance(manifest, dict):
            manifest = {}
        manifest_sandbox = manifest.get("sandbox") or {}
        if not isinstance(manifest_sandbox, dict):
            manifest_sandbox = {}

        reason: Optional[str] = None
        if not bool(lease.get("environment_ready")):
            reason = "not_ready"
        elif int(manifest.get("execution_epoch") or -1) != int(execution_epoch):
            reason = "epoch_mismatch"
        elif str(manifest_sandbox.get("id") or "").strip() != str(lease.get("sandbox_id") or "").strip():
            reason = "sandbox_changed"

        if reason is None:
            return None, manifest

        refreshed_manifest = await self._prepare_shadow_clone_environment()
        refreshed_manifest["refresh_reason"] = reason
        await update_run_sandbox_lease(
            self.agent_run_id,
            environment_manifest=refreshed_manifest,
        )
        await update_environment(
            self.agent_run_id,
            manifest=refreshed_manifest,
        )
        return reason, refreshed_manifest

    async def _get_db_client(self):
        from agentscope_integration.adapters.thread_manager_adapter import ThreadManagerAdapter

        return await ThreadManagerAdapter(db_client=self.db_client).db.client

    async def _checkpoint_completed_layer(
        self,
        *,
        layer_index: int,
        layer: List[Dict[str, Any]],
    ) -> Optional[str]:
        from sandbox.api import create_shadow_clone_standby_sandbox

        subtask_ids = [str(item["id"]) for item in layer if str(item.get("id") or "").strip()]
        await mark_checkpoint(self.agent_run_id, layer_index=layer_index)

        lease = await get_run_sandbox_lease(self.agent_run_id)
        if lease is None:
            return

        checkpoint_payload = {
            "checkpoint_layer_index": layer_index,
            "checkpoint_subtask_ids": subtask_ids,
            "checkpoint_captured_at": datetime.now(timezone.utc).isoformat(),
            "standby_sandbox_id": None,
            "standby_sandbox_type": None,
            "standby_sandbox_info": {},
            "standby_snapshot_template_id": None,
            "standby_source_sandbox_id": None,
            "standby_created_at": None,
            "last_clone_error": None,
        }
        if not self._proactive_standby_enabled():
            await update_run_sandbox_lease(
                self.agent_run_id,
                **checkpoint_payload,
            )
            return None

        try:
            client = await self._get_db_client()
            standby_payload = await create_shadow_clone_standby_sandbox(
                client,
                lease=lease,
                layer_index=layer_index,
                completed_subtask_ids=subtask_ids,
            )
            await update_run_sandbox_lease(
                self.agent_run_id,
                **checkpoint_payload,
                standby_sandbox_id=standby_payload.get("standby_sandbox_id"),
                standby_sandbox_type=standby_payload.get("standby_sandbox_type"),
                standby_sandbox_info=standby_payload.get("standby_sandbox_info"),
                standby_snapshot_template_id=standby_payload.get(
                    "standby_snapshot_template_id"
                ),
                standby_source_sandbox_id=standby_payload.get(
                    "standby_source_sandbox_id"
                ),
                standby_created_at=standby_payload.get("standby_created_at"),
                approved_sandbox_ids=standby_payload.get("approved_sandbox_ids"),
                last_clone_error=None,
            )
            return None
        except Exception as checkpoint_error:
            logger.warning(
                "[ShadowClone] Failed to create standby clone after layer=%s run_id=%s: %s",
                layer_index,
                self.agent_run_id,
                checkpoint_error,
                exc_info=True,
            )
            await update_run_sandbox_lease(
                self.agent_run_id,
                last_clone_error=str(checkpoint_error),
            )
            return str(checkpoint_error)

    async def _ensure_bootstrap_standby_clone(self) -> None:
        from sandbox.api import create_shadow_clone_standby_sandbox

        lease = await get_run_sandbox_lease(self.agent_run_id)
        if lease is None or lease.get("standby_sandbox_id"):
            return

        try:
            client = await self._get_db_client()
            standby_payload = await create_shadow_clone_standby_sandbox(
                client,
                lease=lease,
                layer_index=-1,
                completed_subtask_ids=[],
            )
            await update_run_sandbox_lease(
                self.agent_run_id,
                standby_sandbox_id=standby_payload.get("standby_sandbox_id"),
                standby_sandbox_type=standby_payload.get("standby_sandbox_type"),
                standby_sandbox_info=standby_payload.get("standby_sandbox_info"),
                standby_snapshot_template_id=standby_payload.get(
                    "standby_snapshot_template_id"
                ),
                standby_source_sandbox_id=standby_payload.get(
                    "standby_source_sandbox_id"
                ),
                standby_created_at=standby_payload.get("standby_created_at"),
                approved_sandbox_ids=standby_payload.get("approved_sandbox_ids"),
                last_clone_error=None,
            )
        except Exception as bootstrap_error:
            logger.warning(
                "[ShadowClone] Failed to create bootstrap standby clone run_id=%s: %s",
                self.agent_run_id,
                bootstrap_error,
                exc_info=True,
            )
            await update_run_sandbox_lease(
                self.agent_run_id,
                last_clone_error=str(bootstrap_error),
            )

    async def _promote_standby_clone_for_layer(
        self,
        *,
        layer_index: int,
        layer: List[Dict[str, Any]],
    ) -> tuple[Optional[int], Optional[str]]:
        from sandbox.api import promote_shadow_clone_standby_sandbox

        lease = await get_run_sandbox_lease(self.agent_run_id)
        if lease is None or not lease.get("standby_sandbox_id"):
            return None, "Shadow Clone standby clone was unavailable at recovery time."

        await set_run_sandbox_binding_state(
            self.agent_run_id,
            "recovering",
            last_error=None,
        )
        await update_run_sandbox_lease(
            self.agent_run_id,
            environment_status="recovering",
            environment_ready=False,
            last_error=None,
        )
        await update_environment(
            self.agent_run_id,
            status="recovering",
            ready=False,
            last_error=None,
        )
        try:
            client = await self._get_db_client()
            await promote_shadow_clone_standby_sandbox(client, lease=lease)
        except Exception as clone_error:
            logger.warning(
                "[ShadowClone] Standby clone promotion failed run_id=%s layer=%s: %s",
                self.agent_run_id,
                layer_index,
                clone_error,
                exc_info=True,
            )
            await update_run_sandbox_lease(
                self.agent_run_id,
                binding_state="lost",
                last_error=str(clone_error),
            )
            return None, str(clone_error)

        replenishment_error = await self._replenish_standby_clone_after_promotion(
            layer_index=layer_index,
        )
        if replenishment_error:
            await update_run_sandbox_lease(
                self.agent_run_id,
                last_error=replenishment_error,
            )
            return None, replenishment_error

        next_epoch = await bump_execution_epoch(self.agent_run_id)
        await sync_run_sandbox_execution_epoch(self.agent_run_id, next_epoch)
        roles = {
            str(item["id"]): str(item.get("role") or "")
            for item in layer
            if str(item.get("id") or "").strip()
        }
        subtask_ids = list(roles.keys())
        await delete_results(self.agent_run_id, subtask_ids)
        await reset_subagents_for_epoch(
            self.agent_run_id,
            subtask_ids,
            expected_epoch=next_epoch,
            roles=roles,
        )
        await clear_recovery(self.agent_run_id)
        logger.info(
            "[ShadowClone] Promoted standby clone and reset layer run_id=%s layer=%s epoch=%s",
            self.agent_run_id,
            layer_index,
            next_epoch,
        )
        return next_epoch, None

    async def _replace_active_sandbox_for_layer(
        self,
        *,
        layer_index: int,
        layer: List[Dict[str, Any]],
        recovery_reason: str,
    ) -> tuple[Optional[int], Optional[str]]:
        lease = await get_run_sandbox_lease(self.agent_run_id)
        if lease is None:
            return None, "Shadow Clone sandbox lease was unavailable at recovery time."

        active_sandbox_id = str(lease.get("sandbox_id") or "").strip()
        sandbox_type = str(lease.get("sandbox_type") or "desktop").strip() or "desktop"
        sandbox_info = dict(lease.get("sandbox_info") or {})
        recovery_password = str(sandbox_info.get("pass") or "").strip() or str(uuid4())

        await set_run_sandbox_binding_state(
            self.agent_run_id,
            "recovering",
            last_error=None,
        )
        await update_run_sandbox_lease(
            self.agent_run_id,
            environment_status="recovering",
            environment_ready=False,
            last_error=None,
        )
        await update_environment(
            self.agent_run_id,
            status="recovering",
            ready=False,
            last_error=None,
        )

        try:
            replacement_sandbox, recovery_action = await resume_or_create_sandbox(
                password=recovery_password,
                project_id=self.project_id,
                sandbox_type=sandbox_type,
                sandbox_info={
                    **sandbox_info,
                    "id": active_sandbox_id,
                    "type": sandbox_type,
                },
                allow_create=True,
            )
        except Exception as recovery_error:
            logger.warning(
                "[ShadowClone] Replacement sandbox recovery failed run_id=%s layer=%s: %s",
                self.agent_run_id,
                layer_index,
                recovery_error,
                exc_info=True,
            )
            await update_run_sandbox_lease(
                self.agent_run_id,
                binding_state="lost",
                environment_status="failed",
                environment_ready=False,
                last_error=str(recovery_error),
            )
            return None, str(recovery_error)

        replacement_sandbox_id = str(
            getattr(replacement_sandbox, "sandbox_id", None)
            or getattr(replacement_sandbox, "id", None)
            or active_sandbox_id
        ).strip()
        if not replacement_sandbox_id:
            error_message = "Shadow Clone replacement recovery returned an empty sandbox id."
            await update_run_sandbox_lease(
                self.agent_run_id,
                binding_state="lost",
                environment_status="failed",
                environment_ready=False,
                last_error=error_message,
            )
            return None, error_message

        next_epoch = await bump_execution_epoch(self.agent_run_id)
        clear_shadow_clone_session_state(self.agent_run_id)

        replacement_manifest = {
            "execution_epoch": next_epoch,
            "sandbox": {
                "id": replacement_sandbox_id,
                "type": sandbox_type,
                "binding_state": "locked",
            },
            "replacement_of_sandbox_id": active_sandbox_id or None,
            "replacement_action": str(recovery_action or "").strip() or None,
            "recovery_reason": str(recovery_reason or "").strip() or None,
        }
        replacement_info = {
            **sandbox_info,
            "id": replacement_sandbox_id,
            "type": sandbox_type,
            "pass": recovery_password,
            "state": "running",
            "shadow_clone_recovery_action": str(recovery_action or "").strip() or None,
            "shadow_clone_replaced_sandbox_id": active_sandbox_id or None,
            "shadow_clone_recovered_at": datetime.now(timezone.utc).isoformat(),
        }
        client = await self._get_db_client()
        await (
            client.table("projects")
            .eq("project_id", self.project_id)
            .update({"sandbox": json.dumps(replacement_info)})
            .execute()
        )
        await update_run_sandbox_lease(
            self.agent_run_id,
            sandbox_id=replacement_sandbox_id,
            sandbox_type=sandbox_type,
            sandbox_info=replacement_info,
            binding_state="locked",
            standby_sandbox_id=None,
            standby_sandbox_type=None,
            standby_sandbox_info={},
            standby_snapshot_template_id=None,
            standby_source_sandbox_id=None,
            standby_created_at=None,
            execution_epoch=next_epoch,
            lease_epoch=next_epoch,
            environment_status="pending",
            environment_ready=False,
            environment_prepared_at=None,
            environment_manifest=replacement_manifest,
            last_error=None,
            last_clone_error=None,
        )
        await sync_run_sandbox_execution_epoch(self.agent_run_id, next_epoch)
        await update_environment(
            self.agent_run_id,
            status="preparing",
            ready=False,
            last_error=None,
            manifest=replacement_manifest,
        )
        roles = {
            str(item["id"]): str(item.get("role") or "")
            for item in layer
            if str(item.get("id") or "").strip()
        }
        subtask_ids = list(roles.keys())
        await delete_results(self.agent_run_id, subtask_ids)
        await reset_subagents_for_epoch(
            self.agent_run_id,
            subtask_ids,
            expected_epoch=next_epoch,
            roles=roles,
        )
        await clear_recovery(self.agent_run_id)
        logger.info(
            "[ShadowClone] Replaced active sandbox and reset layer run_id=%s layer=%s epoch=%s sandbox_id=%s",
            self.agent_run_id,
            layer_index,
            next_epoch,
            replacement_sandbox_id,
        )
        return next_epoch, None

    async def _replenish_standby_clone_after_promotion(
        self,
        *,
        layer_index: int,
    ) -> Optional[str]:
        lease = await get_run_sandbox_lease(self.agent_run_id)
        if lease is None:
            return "Shadow Clone sandbox lease disappeared after standby promotion."
        if lease.get("standby_sandbox_id"):
            return None

        checkpoint_layer_index = int(
            lease.get("checkpoint_layer_index")
            if lease.get("checkpoint_layer_index") is not None
            else -1
        )
        checkpoint_subtask_ids = [
            str(item)
            for item in (lease.get("checkpoint_subtask_ids") or [])
            if str(item or "").strip()
        ]
        replenishment_error = await self._checkpoint_clone_from_existing_checkpoint(
            layer_index=checkpoint_layer_index,
            subtask_ids=checkpoint_subtask_ids,
        )
        if replenishment_error:
            logger.warning(
                "[ShadowClone] Promoted standby clone but failed to replenish next standby "
                "run_id=%s recovery_layer=%s checkpoint_layer=%s: %s",
                self.agent_run_id,
                layer_index,
                checkpoint_layer_index,
                replenishment_error,
            )
            return (
                "Shadow Clone promoted the recovery sandbox but could not prepare the next "
                f"safe standby clone. {replenishment_error}"
            )
        return None

    async def _checkpoint_clone_from_existing_checkpoint(
        self,
        *,
        layer_index: int,
        subtask_ids: List[str],
    ) -> Optional[str]:
        from sandbox.api import create_shadow_clone_standby_sandbox

        lease = await get_run_sandbox_lease(self.agent_run_id)
        if lease is None:
            return "Shadow Clone sandbox lease is missing."

        try:
            client = await self._get_db_client()
            standby_payload = await create_shadow_clone_standby_sandbox(
                client,
                lease=lease,
                layer_index=layer_index,
                completed_subtask_ids=subtask_ids,
            )
            await update_run_sandbox_lease(
                self.agent_run_id,
                checkpoint_layer_index=layer_index,
                checkpoint_subtask_ids=subtask_ids,
                checkpoint_captured_at=(
                    str(lease.get("checkpoint_captured_at") or "").strip()
                    or datetime.now(timezone.utc).isoformat()
                ),
                standby_sandbox_id=standby_payload.get("standby_sandbox_id"),
                standby_sandbox_type=standby_payload.get("standby_sandbox_type"),
                standby_sandbox_info=standby_payload.get("standby_sandbox_info"),
                standby_snapshot_template_id=standby_payload.get(
                    "standby_snapshot_template_id"
                ),
                standby_source_sandbox_id=standby_payload.get(
                    "standby_source_sandbox_id"
                ),
                standby_created_at=standby_payload.get("standby_created_at"),
                approved_sandbox_ids=standby_payload.get("approved_sandbox_ids"),
                last_clone_error=None,
            )
            return None
        except Exception as clone_error:
            await update_run_sandbox_lease(
                self.agent_run_id,
                last_clone_error=str(clone_error),
            )
            return str(clone_error)

    async def _stream_main_agent_replan(
        self,
        *,
        layer_index: int,
        reason: str,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        live_activity = await self._set_live_activity(
            scope="main_agent",
            phase="execution",
            reason="replan_continue",
        )
        summaries = await read_summaries(self.agent_run_id)
        summary_payload = json.dumps(summaries, ensure_ascii=False, default=str, indent=2)
        prompt = (
            "Shadow Clone execution hit a sandbox failure mid-run.\n\n"
            f"Failure while executing layer {layer_index}.\n"
            f"Failure detail: {reason}\n\n"
            "Completed subtask summaries retained so far:\n"
            f"```json\n{summary_payload}\n```\n\n"
            "Continue the user's request directly with the normal toolchain now.\n"
            "Treat the current Shadow Clone layer as interrupted and unfinished.\n"
            "Use `read_results` / `read_full_result(subtask_id)` for preserved completed work.\n"
            "Do not call `spawn_subagents` again in this continuation turn.\n"
            "If live sandbox tools remain unavailable, continue with preserved results and durable workspace artifacts only."
        )
        async for msg, is_last in self.main_agent.stream_after_sandbox_failure(prompt):
            yield {
                "type": "passthrough_message",
                "msg": msg,
                "is_last": is_last,
                "route_metadata": self._route_metadata_with_contract(
                    shadow_clone_phase="execution",
                    live_activity=live_activity,
                ),
            }

    async def _stream_main_agent_failed_layer_continue(
        self,
        *,
        layer_index: int,
        failed_subtasks: List[Dict[str, Any]],
        retryable_subtask_ids: List[str],
        reason: str,
        rationale: Optional[str] = None,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        live_activity = await self._set_live_activity(
            scope="main_agent",
            phase="execution",
            reason="failed_layer_continue",
        )
        failure_payload = json.dumps(
            failed_subtasks,
            ensure_ascii=False,
            default=str,
            indent=2,
        )
        prompt = (
            "Shadow Clone execution finished a layer with failed subtasks.\n\n"
            f"Layer index: {layer_index}\n"
            f"Recovery detail: {reason}\n"
            f"Retryable failed subtasks: {json.dumps(retryable_subtask_ids, ensure_ascii=False)}\n"
            f"Coordinator rationale: {rationale or 'Continue directly with preserved partial results.'}\n\n"
            "Failed subtask details:\n"
            f"```json\n{failure_payload}\n```\n\n"
            "Continue the user's request directly now with the normal toolchain.\n"
            "Use `read_results` / `read_full_result(subtask_id)` for preserved completed work.\n"
            "Do not call `spawn_subagents` again in this continuation turn.\n"
            "If a failed subtask depended on a brittle sandbox or provider path, prefer using preserved results "
            "and durable artifacts instead of recreating the exact same failure mode."
        )
        async for msg, is_last in self.main_agent.stream_after_sandbox_failure(prompt):
            yield {
                "type": "passthrough_message",
                "msg": msg,
                "is_last": is_last,
                "route_metadata": self._route_metadata_with_contract(
                    shadow_clone_phase="execution",
                    live_activity=live_activity,
                ),
            }

    @staticmethod
    def _build_failed_subtasks_for_review(
        layer: List[Dict[str, Any]],
        terminal_events: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        role_by_subtask = {
            str(item.get("id") or "").strip(): str(item.get("role") or "").strip() or None
            for item in layer
            if str(item.get("id") or "").strip()
        }
        failed_subtasks: List[Dict[str, Any]] = []
        for event in terminal_events:
            if str(event.get("type") or "").strip() != "subagent_failed":
                continue
            subtask_id = str(event.get("subtask_id") or "").strip()
            if not subtask_id:
                continue
            error_text = str(event.get("error") or "").strip() or None
            failure_class = str(event.get("failure_class") or "").strip() or None
            if failure_class is None and error_text:
                failure_class = classify_failure_text(error_text)
            attempt_index = int(event.get("attempt_index") or 0) or 1
            failed_subtasks.append(
                {
                    "subtask_id": subtask_id,
                    "role": str(event.get("role") or "").strip() or role_by_subtask.get(subtask_id),
                    "error": error_text,
                    "failure_class": failure_class,
                    "attempt_index": attempt_index,
                }
            )
        return failed_subtasks

    @staticmethod
    def _build_failed_layer_review_message(
        *,
        layer_index: int,
        failed_subtasks: List[Dict[str, Any]],
        retryable_subtask_ids: List[str],
    ) -> str:
        return (
            "Review the failed Shadow Clone layer and decide whether to retry failed subtasks.\n\n"
            f"Layer index: {layer_index}\n"
            f"Retryable failed subtasks by policy: {json.dumps(retryable_subtask_ids, ensure_ascii=False)}\n\n"
            "Failed subtask details:\n"
            f"```json\n{json.dumps(failed_subtasks, ensure_ascii=False, default=str, indent=2)}\n```\n"
        )

    @staticmethod
    def _normalize_retry_decision_subtask_ids(
        *,
        requested_subtask_ids: List[str],
        retryable_subtask_ids: List[str],
    ) -> List[str]:
        retryable = {
            str(subtask_id).strip()
            for subtask_id in retryable_subtask_ids
            if str(subtask_id).strip()
        }
        normalized: List[str] = []
        for subtask_id in requested_subtask_ids:
            candidate = str(subtask_id).strip()
            if not candidate or candidate not in retryable or candidate in normalized:
                continue
            normalized.append(candidate)
        return normalized

    async def run(
        self,
        *,
        user_message: str,
        thread_run_id: str,
        user_media_refs=None,
        resume_strategy: str = "auto",
        resume_window_minutes: int = 1440,
    ) -> AsyncGenerator[dict, None]:
        await init_state(
            self.agent_run_id,
            self.mode.value,
            total=0,
            subtasks=[],
            dependencies=[],
        )
        planning_live_activity = await self._set_live_activity(
            scope="shadow_clone_main",
            phase="planning",
            reason="planning_started",
        )
        yield self._event_with_contract(
            {
                "type": "shadow_clone_planning_started",
                "reason": "planning_started",
                "live_activity": planning_live_activity,
            }
        )
        yield self._status_event_with_contract(
            status="pending",
            message="Planning Shadow Clone proposal",
            live_activity=planning_live_activity,
            phase_reason="planning_started",
            activity_owner_override="shadow_clone",
        )

        self._ensure_warmup_started()
        async for msg, is_last in self.main_agent.stream_decompose(user_message):
            yield {
                "type": "passthrough_message",
                "msg": msg,
                "is_last": is_last,
                "route_metadata": self._route_metadata_with_contract(
                    shadow_clone_phase="planning",
                    live_activity=planning_live_activity,
                ),
            }

        proposal = self.main_agent.get_proposal()
        if proposal is None:
            if self.mode == ShadowCloneMode.ON:
                failure_message = (
                    "Shadow Clone mode 'on' requires a proposal via spawn_subagents, "
                    "but the planning turn completed without producing one."
                )
                await update_environment(
                    self.agent_run_id,
                    status="failed",
                    ready=False,
                    last_error=failure_message,
                    manifest={
                        "proposal_required": True,
                        "proposal_missing": True,
                        "shadow_clone_mode": self.mode.value,
                    },
                )
                failed_live_activity = await self._set_live_activity(
                    scope="shadow_clone_main",
                    phase="failed",
                    reason=_PROPOSAL_REQUIRED_BUT_MISSING_REASON,
                )
                await transition(self.agent_run_id, "pending", "failed")
                terminal_metadata = await self._record_terminal_metadata(
                    completion_mode=None,
                    terminal_reason=_PROPOSAL_REQUIRED_BUT_MISSING_REASON,
                )
                await self._cleanup_speculative_startup(
                    release_lease=True,
                    clear_standby=True,
                    last_error=failure_message,
                )
                await cleanup(self.agent_run_id)
                await cleanup_results(self.agent_run_id)
                yield self._event_with_contract(
                    {
                        "type": "status",
                        "status": "failed",
                        "message": failure_message,
                        "live_activity": failed_live_activity,
                        **terminal_metadata,
                    },
                    phase_reason=_PROPOSAL_REQUIRED_BUT_MISSING_REASON,
                )
                return
            await transition(self.agent_run_id, "pending", "completed")
            terminal_metadata = await self._record_terminal_metadata(
                completion_mode="direct_execution_without_proposal",
                terminal_reason="direct_execution_without_proposal",
            )
            complete_live_activity = await self._set_live_activity(
                scope="main_agent",
                phase="completed",
                reason="direct_execution_without_proposal",
            )
            await self._cleanup_speculative_startup(
                release_lease=True,
                clear_standby=True,
            )
            await cleanup(self.agent_run_id)
            await cleanup_results(self.agent_run_id)
            yield self._event_with_contract(
                {
                    "type": "shadow_clone_complete",
                    "live_activity": complete_live_activity,
                    **terminal_metadata,
                }
            )
            return

        subtasks = self._inject_default_subagent_model(proposal.get("subtasks") or [])
        dependencies = proposal.get("dependencies") or []

        await self._await_proposal_state_staged(
            {
                "subtasks": subtasks,
                "dependencies": dependencies,
            }
        )
        for subtask in subtasks:
            await update_subagent(
                self.agent_run_id,
                str(subtask["id"]),
                "pending",
                role=str(subtask.get("role") or ""),
            )
        await initialize_subagent_attempts(
            self.agent_run_id,
            subtask_ids=[
                str(subtask["id"])
                for subtask in subtasks
                if str(subtask.get("id") or "").strip()
            ],
        )
        await transition(self.agent_run_id, "pending", "confirming")
        proposal_live_activity = await self._set_live_activity(
            scope="shadow_clone_main",
            phase="confirming",
            reason="proposal_created",
        )

        yield self._event_with_contract(
            {
                "type": "shadow_clone_proposed",
                "subtasks": subtasks,
                "dependencies": dependencies,
                "live_activity": proposal_live_activity,
            }
        )

        yield self._event_with_contract(
            {
                "type": "shadow_clone_environment_preparing",
                "reason": "initial",
                "live_activity": proposal_live_activity,
            },
            ui_phase_override="preparing_environment",
            phase_reason="initial",
            environment_ready=False,
        )

        try:
            environment_manifest = await self._prepare_shadow_clone_environment()
        except Exception as exc:
            logger.error(
                "[ShadowClone] Failed to prepare strict sandbox environment project=%s run_id=%s: %s",
                self.project_id,
                self.agent_run_id,
                exc,
                exc_info=True,
            )
            await update_environment(
                self.agent_run_id,
                status="failed",
                ready=False,
                last_error=str(exc),
            )
            await update_run_sandbox_lease(
                self.agent_run_id,
                environment_status="failed",
                environment_ready=False,
                last_error=str(exc),
            )
            await transition(self.agent_run_id, "confirming", "failed")
            terminal_metadata = await self._record_terminal_metadata(
                completion_mode=None,
                terminal_reason="environment_prepare_failed",
            )
            await set_run_sandbox_binding_state(
                self.agent_run_id,
                "lost",
                last_error=str(exc),
            )
            failed_live_activity = await self._set_live_activity(
                scope="shadow_clone_main",
                phase="failed",
                reason="environment_prepare_failed",
            )
            yield self._event_with_contract(
                {
                    "type": "status",
                    "status": "failed",
                    "error": str(exc),
                    "live_activity": failed_live_activity,
                    **terminal_metadata,
                },
                phase_reason="environment_prepare_failed",
            )
            await self._cleanup_speculative_startup(clear_standby=True)
            await cleanup(self.agent_run_id)
            await cleanup_results(self.agent_run_id)
            return

        yield self._event_with_contract(
            {
                "type": "shadow_clone_environment_ready",
                "reason": "initial",
                "environment": environment_manifest,
                "live_activity": proposal_live_activity,
            },
            phase_reason="initial",
            environment_ready=True,
        )
        confirmation_wait_live_activity = await self._set_live_activity(
            scope="shadow_clone_main",
            phase="confirming",
            reason="confirmation_waiting",
        )
        yield self._status_event_with_contract(
            status="confirming",
            message="Waiting for confirmation",
            live_activity=confirmation_wait_live_activity,
            phase_reason="confirmation_waiting",
            activity_owner_override="shadow_clone",
        )

        confirm_result = ConfirmationResult.TIMEOUT
        async for confirmation_event in self._stream_confirmation_wait_with_lease_keepalive(
            live_activity=confirmation_wait_live_activity,
        ):
            if confirmation_event.get("type") == "__confirmation_result__":
                confirm_result = confirmation_event.get("result", ConfirmationResult.TIMEOUT)
                break
            yield confirmation_event
        if confirm_result == ConfirmationResult.DENIED:
            await transition(self.agent_run_id, "confirming", "denied")
            terminal_metadata = await self._record_terminal_metadata(
                completion_mode="denied_continue",
                terminal_reason="confirmation_denied",
            )
            await self._cleanup_speculative_startup(
                clear_standby=True,
            )
            denial_live_activity = await self._set_live_activity(
                scope="main_agent",
                phase="execution",
                reason="denied_continue",
            )
            try:
                async for msg, is_last in self.main_agent.stream_after_denial():
                    yield {
                        "type": "passthrough_message",
                        "msg": msg,
                        "is_last": is_last,
                        "route_metadata": self._route_metadata_with_contract(
                            shadow_clone_phase="execution",
                            live_activity=denial_live_activity,
                        ),
                    }
            finally:
                await self._cleanup_speculative_startup(
                    release_lease=True,
                    last_error="denied",
                )
                await cleanup(self.agent_run_id)
                await cleanup_results(self.agent_run_id)
            denied_complete_live_activity = await self._set_live_activity(
                scope="main_agent",
                phase="completed",
                reason="denied_complete",
            )
            yield self._event_with_contract(
                {
                    "type": "shadow_clone_complete",
                    "live_activity": denied_complete_live_activity,
                    **terminal_metadata,
                }
            )
            return

        if confirm_result == ConfirmationResult.CANCELLED:
            await transition(self.agent_run_id, "confirming", "cancelled")
            terminal_metadata = await self._record_terminal_metadata(
                completion_mode=None,
                terminal_reason="confirmation_cancelled",
            )
            await self._cleanup_speculative_startup(
                release_lease=True,
                last_error="cancelled",
                clear_standby=True,
            )
            cancelled_live_activity = await self._set_live_activity(
                scope="shadow_clone_main",
                phase="cancelled",
                reason="confirmation_cancelled",
            )
            yield self._event_with_contract(
                {
                    "type": "status",
                    "status": "cancelled",
                    "live_activity": cancelled_live_activity,
                    **terminal_metadata,
                },
                phase_reason="confirmation_cancelled",
            )
            await cleanup(self.agent_run_id)
            await cleanup_results(self.agent_run_id)
            return

        if confirm_result == ConfirmationResult.TIMEOUT:
            await transition(self.agent_run_id, "confirming", "timeout")
            terminal_metadata = await self._record_terminal_metadata(
                completion_mode=None,
                terminal_reason="confirmation_timeout",
            )
            await self._cleanup_speculative_startup(
                release_lease=True,
                last_error="timeout",
                clear_standby=True,
            )
            timeout_live_activity = await self._set_live_activity(
                scope="shadow_clone_main",
                phase="timeout",
                reason="confirmation_timeout",
            )
            yield self._event_with_contract(
                {
                    "type": "status",
                    "status": "timeout",
                    "live_activity": timeout_live_activity,
                    **terminal_metadata,
                },
                phase_reason="confirmation_timeout",
            )
            await cleanup(self.agent_run_id)
            await cleanup_results(self.agent_run_id)
            return

        try:
            await self._await_bootstrap_clone()
        except Exception as exc:
            logger.error(
                "[ShadowClone] Failed to prepare standby clone project=%s run_id=%s: %s",
                self.project_id,
                self.agent_run_id,
                exc,
                exc_info=True,
            )
            await transition(self.agent_run_id, "confirming", "failed")
            terminal_metadata = await self._record_terminal_metadata(
                completion_mode=None,
                terminal_reason="bootstrap_clone_failed",
            )
            await set_run_sandbox_binding_state(
                self.agent_run_id,
                "lost",
                last_error=str(exc),
            )
            failed_live_activity = await self._set_live_activity(
                scope="shadow_clone_main",
                phase="failed",
                reason="bootstrap_clone_failed",
            )
            yield self._event_with_contract(
                {
                    "type": "status",
                    "status": "failed",
                    "error": str(exc),
                    "live_activity": failed_live_activity,
                    **terminal_metadata,
                },
                phase_reason="bootstrap_clone_failed",
            )
            await self._cleanup_speculative_startup(clear_standby=True)
            await cleanup(self.agent_run_id)
            await cleanup_results(self.agent_run_id)
            return

        await transition(self.agent_run_id, "confirming", "running")
        execution_live_activity = await self._set_live_activity(
            scope="shadow_clone_main",
            phase="execution",
            reason="subagents_running",
        )
        try:
            try:
                layers = topological_sort_layers(
                    subtasks,
                    dependencies,
                    max_parallelism=SHADOW_CLONE_RUNTIME_DISPATCH_WINDOW,
                )
            except TypeError:
                layers = topological_sort_layers(subtasks, dependencies)

            for layer_idx, layer in enumerate(layers):
                full_layer = list(layer)
                current_layer = list(full_layer)
                while True:
                    execution_epoch = await get_execution_epoch(self.agent_run_id)
                    execution_live_activity = await self._set_live_activity(
                        scope="shadow_clone_main",
                        phase="execution",
                        reason="subagents_running",
                        epoch=execution_epoch,
                    )
                    refresh_reason, refreshed_manifest = await self._refresh_environment_if_needed()
                    if refresh_reason is not None:
                        yield self._event_with_contract(
                            {
                                "type": "shadow_clone_environment_preparing",
                                "reason": refresh_reason,
                                "live_activity": execution_live_activity,
                            },
                            ui_phase_override="preparing_environment",
                            phase_reason=refresh_reason,
                        )
                        yield self._event_with_contract(
                            {
                                "type": "shadow_clone_environment_ready",
                                "reason": refresh_reason,
                                "environment": refreshed_manifest or {},
                                "live_activity": execution_live_activity,
                            },
                            phase_reason=refresh_reason,
                        )
                    layer_tasks: List[asyncio.Task[Any]] = []
                    for subtask_index, subtask in enumerate(current_layer):
                        yield await self._dispatch_subagent_attempt(
                            subtask=subtask,
                            layer_index=layer_idx,
                            execution_epoch=execution_epoch,
                            layer_tasks=layer_tasks,
                            live_activity=execution_live_activity,
                        )
                        if subtask_index < len(current_layer) - 1:
                            await asyncio.sleep(
                                SHADOW_CLONE_SUBAGENT_DISPATCH_STAGGER_SECONDS,
                            )

                    recovery_event: Optional[Dict[str, Any]] = None
                    terminal_status_event: Optional[Dict[str, Any]] = None
                    terminal_events: List[Dict[str, Any]] = []
                    try:
                        layer_events_iter = self._wait_for_layer_completion(
                            current_layer,
                            layer_index=layer_idx,
                            execution_epoch=execution_epoch,
                            layer_tasks=layer_tasks,
                            live_activity=execution_live_activity,
                        )
                    except TypeError:
                        layer_events_iter = self._wait_for_layer_completion(current_layer)

                    async for layer_event in layer_events_iter:
                        if layer_event.get("type") == "shadow_clone_layer_recovery":
                            recovery_event = layer_event
                            break
                        layer_event_status = str(
                            layer_event.get("status") or ""
                        ).strip().lower()
                        if (
                            str(layer_event.get("type") or "").strip() == "status"
                            and layer_event_status
                            in TERMINAL_SHADOW_CLONE_STATUSES.union({"stopped"})
                        ):
                            terminal_status_event = layer_event
                            yield layer_event
                            break
                        if layer_event.get("type") in {"subagent_completed", "subagent_failed"}:
                            terminal_events.append(layer_event)
                        yield layer_event

                    await self._finalize_layer_tasks(
                        layer_tasks,
                        recovery_triggered=(
                            recovery_event is not None
                            or terminal_status_event is not None
                        ),
                    )

                    if terminal_status_event is not None:
                        await cleanup(self.agent_run_id)
                        return

                    await self._ensure_terminal_results_visible(terminal_events)

                    if recovery_event is None:
                        failed_subtasks = self._build_failed_subtasks_for_review(
                            current_layer,
                            terminal_events,
                        )
                        if failed_subtasks:
                            retryable_subtasks = [
                                str(item.get("subtask_id") or "").strip()
                                for item in failed_subtasks
                                if is_retryable_failure_class(item.get("failure_class"))
                            ]
                            recovery_reason = (
                                "Shadow Clone finished the current layer, but one or more "
                                "subtasks failed and require a conservative recovery decision."
                            )
                            await record_subagent_failures(
                                self.agent_run_id,
                                failed_subtasks=failed_subtasks,
                            )
                            await request_recovery(
                                self.agent_run_id,
                                kind="layer_review",
                                error_code="SHADOW_CLONE_LAYER_REVIEW_REQUIRED",
                                message=recovery_reason,
                                source="shadow_clone_coordinator",
                                scope=f"layer:{layer_idx}",
                                failed_subtasks=failed_subtasks,
                                retryable_subtasks=retryable_subtasks,
                            )
                            try:
                                review_decision = await self.main_agent.review_failed_layer(
                                    self._build_failed_layer_review_message(
                                        layer_index=layer_idx,
                                        failed_subtasks=failed_subtasks,
                                        retryable_subtask_ids=retryable_subtasks,
                                    )
                                )
                            except Exception as review_error:
                                logger.warning(
                                    "[ShadowClone] Failed-layer review fallback to partial continuation "
                                    "run_id=%s layer=%s error=%s",
                                    self.agent_run_id,
                                    layer_idx,
                                    review_error,
                                    exc_info=True,
                                )
                                review_decision = {
                                    "action": "continue_with_partial_results",
                                    "subtask_ids": [],
                                    "rationale": (
                                        "Failed-layer review could not complete, so Shadow Clone "
                                        "is continuing conservatively with preserved partial results."
                                    ),
                                }
                            requested_retry_ids = [
                                str(item).strip()
                                for item in (review_decision.get("subtask_ids") or [])
                                if str(item).strip()
                            ]
                            selected_retry_ids = self._normalize_retry_decision_subtask_ids(
                                requested_subtask_ids=requested_retry_ids,
                                retryable_subtask_ids=retryable_subtasks,
                            )
                            requested_action = str(
                                review_decision.get("action") or ""
                            ).strip().lower()
                            applied_action = (
                                "retry_failed_subtasks"
                                if requested_action == "retry_failed_subtasks" and selected_retry_ids
                                else "continue_with_partial_results"
                            )
                            rationale = str(review_decision.get("rationale") or "").strip() or None
                            if requested_action == "retry_failed_subtasks" and not selected_retry_ids:
                                rationale = (
                                    rationale
                                    or "No retryable failed subtasks were selected for retry."
                                )
                            await record_recovery_decision(
                                self.agent_run_id,
                                decision=applied_action,
                                decision_source="shadow_clone_failure_review_agent",
                                message=recovery_reason,
                                failed_subtasks=failed_subtasks,
                                retryable_subtasks=retryable_subtasks,
                                scope=f"layer:{layer_idx}",
                            )
                            if applied_action == "retry_failed_subtasks":
                                next_epoch = await bump_execution_epoch(self.agent_run_id)
                                await sync_run_sandbox_execution_epoch(
                                    self.agent_run_id,
                                    next_epoch,
                                )
                                await delete_results(self.agent_run_id, selected_retry_ids)
                                retry_role_map = {
                                    str(item.get("id") or "").strip(): str(item.get("role") or "")
                                    for item in current_layer
                                    if str(item.get("id") or "").strip()
                                }
                                await prepare_subagents_for_retry(
                                    self.agent_run_id,
                                    selected_retry_ids,
                                    expected_epoch=next_epoch,
                                    roles=retry_role_map,
                                )
                                current_layer = [
                                    item
                                    for item in current_layer
                                    if str(item.get("id") or "").strip() in selected_retry_ids
                                ]
                                continue

                            async for continuation_event in self._stream_main_agent_failed_layer_continue(
                                layer_index=layer_idx,
                                failed_subtasks=failed_subtasks,
                                retryable_subtask_ids=retryable_subtasks,
                                reason=recovery_reason,
                                rationale=rationale,
                            ):
                                yield continuation_event
                            await transition(self.agent_run_id, "running", "completed")
                            terminal_metadata = await self._record_terminal_metadata(
                                completion_mode="failed_layer_continue",
                                terminal_reason="failed_layer_continue_complete",
                            )
                            await set_run_sandbox_binding_state(
                                self.agent_run_id,
                                "released",
                                last_error=None,
                            )
                            failed_layer_complete = await self._set_live_activity(
                                scope="main_agent",
                                phase="completed",
                                reason="failed_layer_continue_complete",
                            )
                            yield self._event_with_contract(
                                {
                                    "type": "shadow_clone_complete",
                                    "live_activity": failed_layer_complete,
                                    **terminal_metadata,
                                }
                            )
                            await cleanup(self.agent_run_id)
                            return

                        checkpoint_error = await self._checkpoint_completed_layer(
                            layer_index=layer_idx,
                            layer=full_layer,
                        )
                        if checkpoint_error is None:
                            break
                        recovery_event = {
                            "type": "shadow_clone_layer_recovery",
                            "recovery_kind": "replan",
                            "layer_index": layer_idx,
                            "message": (
                                "Shadow Clone could not prepare the next safe checkpoint "
                                f"after layer {layer_idx}. {checkpoint_error}"
                            ),
                        }

                    recovery_kind = str(recovery_event.get("recovery_kind") or "clone")
                    recovery_reason = str(
                        recovery_event.get("message")
                        or "Shadow Clone sandbox recovery required."
                    )
                    yield self._event_with_contract(
                        {
                            "type": "shadow_clone_environment_recovering",
                            "recovery_kind": recovery_kind,
                            "layer_index": layer_idx,
                            "message": recovery_reason,
                            "live_activity": execution_live_activity,
                        },
                        ui_phase_override="recovering",
                        phase_reason=recovery_reason,
                    )

                    if recovery_kind == "clone":
                        current_lease = await get_run_sandbox_lease(self.agent_run_id) or {}
                        if (
                            not self._proactive_standby_enabled()
                            and str(current_lease.get("standby_sandbox_id") or "").strip()
                        ):
                            await self._clear_bootstrap_standby_clone_metadata()
                            current_lease = await get_run_sandbox_lease(self.agent_run_id) or {}

                        if (
                            self._proactive_standby_enabled()
                            and str(current_lease.get("standby_sandbox_id") or "").strip()
                        ):
                            next_epoch, promotion_error = await self._promote_standby_clone_for_layer(
                                layer_index=layer_idx,
                                layer=full_layer,
                            )
                            if next_epoch is not None:
                                continue
                            await self._clear_bootstrap_standby_clone_metadata()
                            replacement_epoch, replacement_error = await self._replace_active_sandbox_for_layer(
                                layer_index=layer_idx,
                                layer=full_layer,
                                recovery_reason=promotion_error or recovery_reason,
                            )
                            if replacement_epoch is not None:
                                continue
                            recovery_failure_reason = (
                                replacement_error
                                or promotion_error
                                or (
                                    "Shadow Clone standby clone could not be activated. "
                                    f"{recovery_reason}"
                                )
                            )
                        else:
                            next_epoch, promotion_error = await self._replace_active_sandbox_for_layer(
                                layer_index=layer_idx,
                                layer=full_layer,
                                recovery_reason=recovery_reason,
                            )
                            recovery_failure_reason = promotion_error or (
                                "Shadow Clone recovery sandbox replacement failed. "
                                f"{recovery_reason}"
                            )
                        if next_epoch is not None:
                            continue
                        recovery_kind = "replan"
                        recovery_reason = promotion_error or recovery_failure_reason

                    await request_recovery(
                        self.agent_run_id,
                        kind="replan",
                        error_code="SHADOW_CLONE_SANDBOX_REPLAN_REQUIRED",
                        message=recovery_reason,
                        source="shadow_clone_coordinator",
                    )
                    next_epoch = await bump_execution_epoch(self.agent_run_id)
                    await sync_run_sandbox_execution_epoch(self.agent_run_id, next_epoch)
                    async for replan_event in self._stream_main_agent_replan(
                        layer_index=layer_idx,
                        reason=recovery_reason,
                    ):
                        yield replan_event
                    await transition(self.agent_run_id, "running", "completed")
                    terminal_metadata = await self._record_terminal_metadata(
                        completion_mode="replan_continue",
                        terminal_reason="replan_continue_complete",
                    )
                    await update_run_sandbox_lease(
                        self.agent_run_id,
                        binding_state="released",
                        last_error=recovery_reason,
                    )
                    replan_complete = await self._set_live_activity(
                        scope="main_agent",
                        phase="completed",
                        reason="replan_continue_complete",
                    )
                    yield self._event_with_contract(
                        {
                            "type": "shadow_clone_complete",
                            "live_activity": replan_complete,
                            **terminal_metadata,
                        }
                    )
                    await cleanup(self.agent_run_id)
                    return

            aggregate_transition_started = await transition(
                self.agent_run_id,
                "running",
                "aggregating",
            )
            if not aggregate_transition_started:
                state = await get_state(self.agent_run_id) or {}
                current_status = str(state.get("status") or "").strip().lower()
                if current_status in TERMINAL_SHADOW_CLONE_STATUSES:
                    await cleanup(self.agent_run_id)
                    return
                raise RuntimeError(
                    "Shadow Clone could not enter aggregating state "
                    f"(run_id={self.agent_run_id} current_status={current_status or 'unknown'})"
                )
            aggregate_live_activity = await self._set_live_activity(
                scope="main_agent",
                phase="aggregate",
                reason="aggregate_started",
            )
            yield self._event_with_contract(
                {
                    "type": "shadow_clone_aggregating",
                    "live_activity": aggregate_live_activity,
                }
            )
            async for msg, is_last in self.main_agent.stream_aggregate():
                yield {
                    "type": "passthrough_message",
                    "msg": msg,
                    "is_last": is_last,
                    "route_metadata": self._route_metadata_with_contract(
                        shadow_clone_phase="aggregate",
                        live_activity=aggregate_live_activity,
                    ),
                }
            aggregate_transition_completed = await transition(
                self.agent_run_id,
                "aggregating",
                "completed",
            )
            if not aggregate_transition_completed:
                state = await get_state(self.agent_run_id) or {}
                current_status = str(state.get("status") or "").strip().lower()
                if current_status in TERMINAL_SHADOW_CLONE_STATUSES:
                    await cleanup(self.agent_run_id)
                    return
                raise RuntimeError(
                    "Shadow Clone could not complete aggregation "
                    f"(run_id={self.agent_run_id} current_status={current_status or 'unknown'})"
                )
            terminal_metadata = await self._record_terminal_metadata(
                completion_mode="aggregate",
                terminal_reason="aggregate_complete",
            )
            await set_run_sandbox_binding_state(
                self.agent_run_id,
                "released",
                last_error=None,
            )
            aggregate_complete = await self._set_live_activity(
                scope="main_agent",
                phase="completed",
                reason="aggregate_complete",
            )
            yield self._event_with_contract(
                {
                    "type": "shadow_clone_complete",
                    "live_activity": aggregate_complete,
                    **terminal_metadata,
                }
            )
            await cleanup(self.agent_run_id)
        except Exception as exc:
            logger.error(
                "[ShadowClone] Coordinator execution failed run_id=%s: %s",
                self.agent_run_id,
                exc,
                exc_info=True,
            )
            await self._drain_local_subagent_tasks(
                cancel=True,
                reason="execution_exception",
            )
            state = await get_state(self.agent_run_id) or {}
            current_status = str(state.get("status") or "").strip().lower()
            if current_status == "running":
                await transition(self.agent_run_id, "running", "failed")
            elif current_status == "aggregating":
                await transition(self.agent_run_id, "aggregating", "failed")
            elif current_status == "confirming":
                await transition(self.agent_run_id, "confirming", "failed")
            await self._record_terminal_metadata(
                completion_mode=None,
                terminal_reason="coordinator_exception",
            )

            lease = await get_run_sandbox_lease(self.agent_run_id)
            if lease is not None and str(lease.get("binding_state") or "").strip().lower() != "lost":
                await set_run_sandbox_binding_state(
                    self.agent_run_id,
                    "released",
                    last_error=str(exc),
                )
            await cleanup(self.agent_run_id)
            raise
        finally:
            await self._drain_local_subagent_tasks(
                cancel=True,
                reason="execution_exit",
            )

    async def _wait_for_layer_completion(
        self,
        layer: Iterable[Dict[str, Any]],
        *,
        layer_index: int = 0,
        execution_epoch: int = 0,
        layer_tasks: Optional[List[asyncio.Task[Any]]] = None,
        live_activity: Optional[Dict[str, Any]] = None,
        timeout: int = SHADOW_CLONE_SUBAGENT_TIMEOUT,
    ) -> AsyncGenerator[Dict[str, Any], None]:
        timeout_window = max(1, int(timeout))
        layer_items = [item for item in layer if str(item.get("id") or "").strip()]
        pending: Set[str] = {str(item["id"]) for item in layer_items}
        if not pending:
            return

        subtask_map = {
            str(item["id"]): dict(item)
            for item in layer_items
            if str(item.get("id") or "").strip()
        }
        subtask_deadlines = {
            subtask_id: time.monotonic() + timeout_window for subtask_id in pending
        }
        straggler_subtask_id: Optional[str] = None
        straggler_deadline_at: Optional[float] = None
        updates_channel = f"shadow_clone:{self.agent_run_id}:updates"
        heartbeat_interval = 15.0
        next_heartbeat_at = time.monotonic() + heartbeat_interval
        next_lease_keepalive_at = (
            time.monotonic() + SHADOW_CLONE_LEASE_KEEPALIVE_INTERVAL_SECONDS
        )
        reconcile_interval_seconds = max(
            0.0,
            float(SHADOW_CLONE_COORDINATOR_RECONCILE_INTERVAL_SECONDS),
        )
        next_reconcile_at = time.monotonic() + reconcile_interval_seconds
        shutdown_reason: Optional[str] = None
        pubsub = await redis_service.create_pubsub()

        def _refresh_straggler_deadline(
            *,
            now: Optional[float] = None,
            force: bool = False,
        ) -> None:
            nonlocal straggler_subtask_id, straggler_deadline_at
            if len(layer_items) <= 1 or len(pending) != 1:
                straggler_subtask_id = None
                straggler_deadline_at = None
                return

            only_subtask_id = next(iter(pending))
            if (
                not force
                and straggler_subtask_id == only_subtask_id
                and straggler_deadline_at is not None
            ):
                return

            straggler_subtask_id = only_subtask_id
            straggler_deadline_at = (
                (time.monotonic() if now is None else now)
                + _shadow_clone_subagent_straggler_grace_seconds()
            )

        async def _handle_failed_subtask(
            failure_event: Dict[str, Any],
        ) -> List[Dict[str, Any]]:
            subtask_id = str(failure_event.get("subtask_id") or "").strip()
            if not subtask_id:
                return []

            recovery_events = await self._maybe_schedule_runtime_recovery(
                subtask=subtask_map.get(subtask_id) or {"id": subtask_id},
                failure_event=failure_event,
                layer_index=layer_index,
                execution_epoch=execution_epoch,
                layer_tasks=layer_tasks,
            )
            if any(
                event.get("type")
                in {
                    "shadow_clone_subagent_wake_sent",
                    "shadow_clone_subagent_resumed",
                    "shadow_clone_subagent_replacement_started",
                    "subagent_started",
                }
                for event in recovery_events
            ):
                subtask_deadlines[subtask_id] = time.monotonic() + timeout_window
                _refresh_straggler_deadline(force=True)
                return recovery_events

            pending.discard(subtask_id)
            subtask_deadlines.pop(subtask_id, None)
            _refresh_straggler_deadline()
            return [*recovery_events, failure_event]

        async def _detect_terminal_stop() -> Optional[Dict[str, Any]]:
            nonlocal shutdown_reason
            try:
                state = await get_state(self.agent_run_id) or {}
            except Exception as exc:
                logger.warning(
                    "[ShadowClone] Failed to inspect state while waiting for layer completion "
                    "run_id=%s: %s",
                    self.agent_run_id,
                    exc,
                )
                state = {}

            current_status = str(state.get("status") or "").strip().lower()
            current_epoch = int(state.get("execution_epoch") or execution_epoch)
            if current_status in TERMINAL_SHADOW_CLONE_STATUSES:
                shutdown_reason = current_status
                pending.clear()
                subtask_deadlines.clear()
                _refresh_straggler_deadline(force=True)
                return {
                    "type": "status",
                    "status": current_status,
                    "reason": f"shadow_clone_{current_status}",
                    "shadow_clone_status": current_status,
                    "execution_epoch": current_epoch,
                    "completion_mode": (
                        str(state.get("completion_mode") or "").strip() or None
                    ),
                    "terminal_reason": (
                        str(state.get("terminal_reason") or "").strip()
                        or f"shadow_clone_{current_status}"
                    ),
                }

            if current_epoch != execution_epoch:
                shutdown_reason = "execution_epoch_superseded"
                pending.clear()
                subtask_deadlines.clear()
                _refresh_straggler_deadline(force=True)
                return {
                    "type": "status",
                    "status": "stopped",
                    "reason": "execution_epoch_superseded",
                    "execution_epoch": current_epoch,
                }

            try:
                lease = await get_run_sandbox_lease(self.agent_run_id)
            except Exception:
                lease = None

            binding_state = str((lease or {}).get("binding_state") or "").strip().lower()
            if binding_state == "released":
                shutdown_reason = "lease_released"
                pending.clear()
                subtask_deadlines.clear()
                _refresh_straggler_deadline(force=True)
                return {
                    "type": "status",
                    "status": "stopped",
                    "reason": "lease_released",
                    "execution_epoch": current_epoch,
                }

            return None

        async def _expire_timed_out_subtasks() -> List[Dict[str, Any]]:
            now = time.monotonic()
            expired: Dict[str, str] = {
                subtask_id: "timeout"
                for subtask_id in list(pending)
                if subtask_deadlines.get(subtask_id, now + 1) <= now
            }
            if (
                straggler_subtask_id
                and straggler_subtask_id in pending
                and straggler_deadline_at is not None
                and straggler_deadline_at <= now
            ):
                expired.setdefault(straggler_subtask_id, "straggler")
            if not expired:
                return []

            events: List[Dict[str, Any]] = []
            for subtask_id, expiration_kind in expired.items():
                failure_summary = (
                    "straggler_timeout_after_peer_completion"
                    if expiration_kind == "straggler"
                    else "timeout"
                )
                failure_class = (
                    "stalled" if expiration_kind == "straggler" else FAILURE_CLASS_TIMEOUT
                )
                if expiration_kind == "straggler":
                    logger.warning(
                        "[ShadowClone] Failing last running subagent after peer completion "
                        "run_id=%s subtask_id=%s grace_seconds=%s",
                        self.agent_run_id,
                        subtask_id,
                        _shadow_clone_subagent_straggler_grace_seconds(),
                    )
                try:
                    await update_subagent_for_epoch(
                        self.agent_run_id,
                        subtask_id,
                        "failed",
                        expected_epoch=execution_epoch,
                        result_summary=failure_summary,
                    )
                except Exception as exc:
                    logger.warning(
                        "[ShadowClone] Failed to mark timed out subagent failed run_id=%s subtask_id=%s: %s",
                        self.agent_run_id,
                        subtask_id,
                        exc,
                    )

                state_snapshot = await get_state(self.agent_run_id) or {}
                subagent_state = (
                    ((state_snapshot.get("subagents") or {}).get(subtask_id))
                    if isinstance((state_snapshot.get("subagents") or {}).get(subtask_id), dict)
                    else {}
                )
                failure_event = {
                    "type": "subagent_failed",
                    "subtask_id": subtask_id,
                    "role": str(
                        (subagent_state or {}).get("role")
                        or (subtask_map.get(subtask_id) or {}).get("role")
                        or ""
                    ).strip()
                    or None,
                    "error": failure_summary,
                    "attempt_index": max(
                        1,
                        int((subagent_state or {}).get("attempt_index") or 1),
                    ),
                    "failure_class": failure_class,
                }
                if expiration_kind == "straggler":
                    pending.discard(subtask_id)
                    subtask_deadlines.pop(subtask_id, None)
                    _refresh_straggler_deadline(now=now)
                    events.append(failure_event)
                    continue

                events.extend(await _handle_failed_subtask(failure_event))
            return events

        async def _reconcile_pending(
            terminal_hints: Optional[Dict[str, Any]] = None,
        ) -> List[Dict[str, Any]]:
            events: List[Dict[str, Any]] = []
            state: Dict[str, Any] = {}
            try:
                state = await get_state(self.agent_run_id) or {}
            except Exception as exc:
                logger.warning(
                    "[ShadowClone] Failed to reconcile state run_id=%s: %s",
                    self.agent_run_id,
                    exc,
                )
            subagent_map = state.get("subagents") or {}

            for subtask_id in list(pending):
                subagent_state = subagent_map.get(subtask_id) or {}
                status = str(subagent_state.get("status") or "")
                hint_matches = (
                    str((terminal_hints or {}).get("subtask_id") or "") == subtask_id
                    and int((terminal_hints or {}).get("execution_epoch") or execution_epoch)
                    == execution_epoch
                )
                if status not in {"completed", "failed"}:
                    if hint_matches:
                        status = str((terminal_hints or {}).get("status") or "")
                event_role = str(subagent_state.get("role") or "").strip() or None
                event_result_summary = (
                    str(subagent_state.get("result_summary") or "").strip() or None
                )
                event_attempt_index = int(subagent_state.get("attempt_index") or 0) or None
                event_failure_class = (
                    str(subagent_state.get("failure_class") or "").strip() or None
                )
                event_error = None
                recovery_state = (
                    subagent_state.get("recovery")
                    if isinstance(subagent_state.get("recovery"), dict)
                    else {}
                )
                recovery_phase = str(recovery_state.get("phase") or "").strip().lower()
                if hint_matches:
                    event_role = str((terminal_hints or {}).get("role") or "").strip() or event_role
                    event_result_summary = (
                        str((terminal_hints or {}).get("result_summary") or "").strip()
                        or event_result_summary
                    )
                    event_attempt_index = (
                        int((terminal_hints or {}).get("attempt_index") or 0)
                        or event_attempt_index
                    )
                    event_error = (
                        str((terminal_hints or {}).get("error") or "").strip() or None
                    )
                    event_failure_class = (
                        str((terminal_hints or {}).get("failure_class") or "").strip()
                        or event_failure_class
                    )
                if status == "completed":
                    pending.discard(subtask_id)
                    subtask_deadlines.pop(subtask_id, None)
                    _refresh_straggler_deadline()
                    if recovery_phase == "wake_dispatched":
                        await update_subagent_recovery(
                            self.agent_run_id,
                            subtask_id,
                            expected_epoch=execution_epoch,
                            phase="wake_succeeded",
                            reason=None,
                        )
                    elif recovery_phase == "replacement_started":
                        await update_subagent_recovery(
                            self.agent_run_id,
                            subtask_id,
                            expected_epoch=execution_epoch,
                            phase="replacement_succeeded",
                            reason=None,
                        )
                        replacement_live = await self._set_live_activity(
                            scope="shadow_clone_main",
                            phase="execution",
                            reason="subagent_replacement_completed",
                            subtask_id=subtask_id,
                            epoch=execution_epoch,
                        )
                        events.append(
                            {
                                "type": "shadow_clone_subagent_replacement_completed",
                                "subtask_id": subtask_id,
                                "role": event_role,
                                "attempt_index": event_attempt_index,
                                "recovery_mode": str(
                                    recovery_state.get("mode") or "replacement",
                                ).strip()
                                or "replacement",
                                "recovery_phase": "replacement_succeeded",
                                "replacement_context_id": (
                                    str(recovery_state.get("replacement_context_id") or "").strip()
                                    or None
                                ),
                                "handoff_summary": (
                                    str(recovery_state.get("handoff_summary") or "").strip()
                                    or None
                                ),
                                "live_activity": replacement_live,
                            }
                        )
                    event = {
                        "type": "subagent_completed",
                        "subtask_id": subtask_id,
                    }
                    if event_role:
                        event["role"] = event_role
                    if event_attempt_index is not None:
                        event["attempt_index"] = event_attempt_index
                    if event_result_summary:
                        event["result_summary"] = event_result_summary
                    events.append(event)
                elif status == "failed":
                    failure_event = {
                        "type": "subagent_failed",
                        "subtask_id": subtask_id,
                    }
                    if event_role:
                        failure_event["role"] = event_role
                    if event_attempt_index is not None:
                        failure_event["attempt_index"] = event_attempt_index
                    if event_error or event_result_summary:
                        failure_event["error"] = event_error or event_result_summary
                    if event_failure_class:
                        failure_event["failure_class"] = event_failure_class
                    events.extend(await _handle_failed_subtask(failure_event))
            return events

        try:
            await pubsub.subscribe(updates_channel)
            while pending:
                stop_event = await _detect_terminal_stop()
                if stop_event is not None:
                    yield stop_event
                    return

                for event in await _expire_timed_out_subtasks():
                    yield event
                if not pending:
                    break

                now = time.monotonic()
                remaining = max(
                    0.0,
                    min(subtask_deadlines.get(subtask_id, now) for subtask_id in pending) - now,
                )
                if (
                    straggler_subtask_id
                    and straggler_subtask_id in pending
                    and straggler_deadline_at is not None
                ):
                    remaining = min(
                        remaining,
                        max(0.0, straggler_deadline_at - now),
                    )
                poll_timeout = min(
                    2.0,
                    remaining,
                    SHADOW_CLONE_COORDINATOR_RECONCILE_INTERVAL_SECONDS,
                )
                if poll_timeout <= 0:
                    await asyncio.sleep(0)
                    continue

                terminal_hints: Dict[str, Any] = {}
                received_activity = False
                try:
                    message = await pubsub.get_message(
                        ignore_subscribe_messages=True,
                        timeout=poll_timeout,
                    )
                except ConnectionError as exc:
                    logger.warning(
                        "[ShadowClone] Pubsub connection error while waiting for layer completion "
                        "run_id=%s: %s. Re-subscribing.",
                        self.agent_run_id,
                        exc,
                    )
                    await pubsub.subscribe(updates_channel)
                    message = None

                if message and message.get("type") == "message":
                    payload = _decode_pubsub_data(message.get("data"))
                    logger.debug(
                        "[ShadowClone] Layer update run_id=%s payload=%s",
                        self.agent_run_id,
                        payload[:400],
                    )
                    decoded_payload = _decode_pubsub_payload(message.get("data"))
                    activity_payload = _parse_subagent_activity_payload(decoded_payload)
                    if activity_payload and int(
                        activity_payload.get("execution_epoch") or execution_epoch
                    ) == execution_epoch:
                        received_activity = True
                        yield {
                            "type": "subagent_activity",
                            **activity_payload,
                        }
                        if (
                            straggler_subtask_id
                            and str(activity_payload.get("subtask_id") or "").strip()
                            == straggler_subtask_id
                        ):
                            straggler_deadline_at = (
                                time.monotonic()
                                + _shadow_clone_subagent_straggler_grace_seconds()
                            )
                    terminal_hints = _parse_subagent_update_payload(decoded_payload)
                should_reconcile = (
                    not received_activity
                    or bool(terminal_hints)
                    or time.monotonic() >= next_reconcile_at
                )
                if should_reconcile:
                    for event in await _reconcile_pending(terminal_hints):
                        yield event
                    _refresh_straggler_deadline()
                    next_reconcile_at = time.monotonic() + reconcile_interval_seconds

                    stop_event = await _detect_terminal_stop()
                    if stop_event is not None:
                        yield stop_event
                        return

                    try:
                        lease = await get_run_sandbox_lease(self.agent_run_id)
                    except Exception:
                        lease = None
                    binding_state = str((lease or {}).get("binding_state") or "").strip().lower()
                    if binding_state == "recovery_required":
                        shutdown_reason = "recovery_required"
                        recovery_message = str((lease or {}).get("last_error") or "").strip()
                        await request_recovery(
                            self.agent_run_id,
                            kind="clone",
                            error_code="SHADOW_CLONE_SANDBOX_CLONE_RECOVERY_REQUIRED",
                            message=recovery_message or "Shadow Clone standby clone recovery required.",
                            source="shadow_clone_coordinator",
                        )
                        yield {
                            "type": "shadow_clone_layer_recovery",
                            "recovery_kind": "clone",
                            "layer_index": layer_index,
                            "message": recovery_message,
                        }
                        return
                    if binding_state == "lost":
                        shutdown_reason = "lost"
                        recovery_message = str((lease or {}).get("last_error") or "").strip()
                        await request_recovery(
                            self.agent_run_id,
                            kind="clone",
                            error_code="SHADOW_CLONE_SANDBOX_REPLACEMENT_REQUIRED",
                            message=(
                                recovery_message
                                or "Shadow Clone sandbox could not be reattached and requires replacement."
                            ),
                            source="shadow_clone_coordinator",
                        )
                        yield {
                            "type": "shadow_clone_layer_recovery",
                            "recovery_kind": "clone",
                            "layer_index": layer_index,
                            "message": recovery_message,
                        }
                        return

                now = time.monotonic()
                if now >= next_lease_keepalive_at:
                    await self._keep_strict_lease_alive(
                        context=(
                            "Shadow Clone layer wait lost the strict sandbox lease before all "
                            "subagents finished"
                        ),
                    )
                    next_lease_keepalive_at = (
                        now + SHADOW_CLONE_LEASE_KEEPALIVE_INTERVAL_SECONDS
                    )
                if now >= next_heartbeat_at:
                    yield {
                        "type": "heartbeat",
                        "pending": len(pending),
                    }
                    next_heartbeat_at = now + heartbeat_interval
        finally:
            if pending and shutdown_reason is None:
                try:
                    lease = await get_run_sandbox_lease(self.agent_run_id)
                except Exception:
                    lease = None
                binding_state = str((lease or {}).get("binding_state") or "").strip().lower()
                if binding_state not in {"released", "lost", "recovery_required"}:
                    try:
                        await set_run_sandbox_binding_state(
                            self.agent_run_id,
                            "released",
                            last_error=None,
                        )
                    except Exception:
                        logger.warning(
                            "[ShadowClone] Failed to release strict sandbox lease after layer wait shutdown "
                            "run_id=%s pending=%s",
                            self.agent_run_id,
                            sorted(pending),
                            exc_info=True,
                        )
            try:
                await pubsub.unsubscribe(updates_channel)
            except Exception:
                pass
            try:
                await pubsub.close()
            except Exception:
                pass
