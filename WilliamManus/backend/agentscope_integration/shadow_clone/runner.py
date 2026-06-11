"""Public runner facade for Shadow Clone mode."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Dict, List, Optional

from .constants import ShadowCloneMode
from .coordinator import ShadowCloneCoordinator
from agentscope_integration.streaming.sse_adapter import SSEAdapter

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


class ShadowCloneRunner:
    """Shadow Clone runner with AgentScopeRunner-compatible run signature."""

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
        self.subagent_model = subagent_model
        self._sequence = 0
        self._lf_trace = trace
        self.coordinator = ShadowCloneCoordinator(
            thread_id=thread_id,
            project_id=project_id,
            agent_run_id=agent_run_id,
            model_key=model_key,
            db_client=db_client,
            mode=mode,
            subagent_model=subagent_model,
            trace=trace,
        )

    async def run(
        self,
        user_message: str,
        thread_run_id: str,
        user_media_refs: Optional[List[Dict[str, Any]]] = None,
        resume_strategy: str = "auto",
        resume_window_minutes: int = 1440,
    ) -> AsyncGenerator[dict, None]:
        self._sequence = 0
        _lf_span = None
        if self._lf_trace:
            try:
                _lf_span = self._lf_trace.start_observation(
                    name="shadow_clone_run",
                    as_type="span",
                    input={
                        "thread_run_id": thread_run_id,
                        "model_key": self.model_key,
                        "mode": self.mode.value if hasattr(self.mode, "value") else str(self.mode),
                    },
                )
            except Exception:
                pass

        try:
            async for event in self.coordinator.run(
                user_message=user_message,
                thread_run_id=thread_run_id,
                user_media_refs=user_media_refs,
                resume_strategy=resume_strategy,
                resume_window_minutes=resume_window_minutes,
            ):
                if event.get("type") == "passthrough_sse":
                    yield event["payload"]
                    continue

                if event.get("type") == "passthrough_message":
                    payload = self._convert_passthrough_message(
                        event["msg"],
                        bool(event.get("is_last")),
                        thread_run_id,
                        route_metadata=event.get("route_metadata"),
                    )
                    if payload is not None:
                        yield payload
                    continue

                if event.get("type") == "status":
                    status_payload = {
                        "type": "status",
                        "status": event.get("status"),
                        "message": event.get("message", ""),
                    }
                    if event.get("completion_mode") is not None:
                        status_payload["completion_mode"] = event.get("completion_mode")
                    if event.get("terminal_reason") is not None:
                        status_payload["terminal_reason"] = event.get("terminal_reason")
                    if isinstance(event.get("live_activity"), dict):
                        status_payload["live_activity"] = event.get("live_activity")
                    status_payload.update(
                        {
                            key: value
                            for key, value in self._event_contract(event).items()
                            if value is not None
                        }
                    )
                    yield status_payload
                    continue

                yield self._to_sse(event, thread_run_id)
        finally:
            if _lf_span:
                try:
                    _lf_span.update(output={"status": "completed"})
                    _lf_span.end()
                except Exception:
                    pass

    async def close(self) -> None:
        """Close Shadow Clone sidecar resources."""
        await self.coordinator.close()

    @staticmethod
    def _is_spawn_subagents_tool_name(value: Any) -> bool:
        return str(value or "").strip() == "spawn_subagents"

    def _normalize_planning_payload(
        self,
        payload: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        try:
            metadata = json.loads(payload.get("metadata") or "{}")
            content = json.loads(payload.get("content") or "{}")
        except Exception:
            return payload

        stream_status = str(metadata.get("stream_status") or "").strip()
        tool_calls = metadata.get("tool_calls") or content.get("tool_calls") or []
        has_spawn_subagents_tool = any(
            self._is_spawn_subagents_tool_name(
                tool_call.get("function", {}).get("name"),
            )
            for tool_call in tool_calls
            if isinstance(tool_call, dict)
        )

        if payload.get("type") == "tool":
            if self._is_spawn_subagents_tool_name(content.get("tool_name")):
                return None
            return payload

        if not has_spawn_subagents_tool:
            return payload

        cleaned_content = dict(content) if isinstance(content, dict) else {}
        cleaned_content.pop("tool_calls", None)
        metadata.pop("tool_calls", None)

        text_content = str(cleaned_content.get("content") or "").strip()
        reasoning_content = str(cleaned_content.get("reasoning_content") or "").strip()

        if text_content:
            metadata["stream_status"] = "complete" if stream_status == "complete" else "chunk"
        elif reasoning_content:
            metadata["stream_status"] = "complete" if stream_status == "complete" else "reasoning_chunk"
        else:
            return None

        payload["type"] = "assistant"
        payload["content"] = json.dumps(cleaned_content, ensure_ascii=False)
        payload["metadata"] = json.dumps(metadata, ensure_ascii=False)
        return payload

    def _convert_passthrough_message(
        self,
        msg: Any,
        is_last: bool,
        thread_run_id: str,
        route_metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        normalized_route_metadata = self._route_metadata_with_contract(route_metadata)
        adapter = SSEAdapter(
            self.thread_id,
            thread_run_id,
            route_metadata=normalized_route_metadata,
        )
        adapter.sequence = self._sequence
        payload = adapter.convert(msg, is_last)
        self._sequence = adapter.sequence

        if (
            isinstance(normalized_route_metadata, dict)
            and normalized_route_metadata.get("shadow_clone_phase") == "planning"
        ):
            return self._normalize_planning_payload(payload)
        return payload

    def _route_metadata_with_contract(
        self,
        route_metadata: Optional[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        if not isinstance(route_metadata, dict):
            return route_metadata

        normalized_route_metadata = dict(route_metadata)
        contract = _build_shadow_clone_contract(
            live_activity=(
                normalized_route_metadata.get("live_activity")
                if isinstance(normalized_route_metadata.get("live_activity"), dict)
                else None
            ),
        )
        for key, value in contract.items():
            if value is not None and key not in normalized_route_metadata:
                normalized_route_metadata[key] = value
        return normalized_route_metadata

    def _event_contract(
        self,
        event: Dict[str, Any],
    ) -> Dict[str, Any]:
        if not isinstance(event, dict):
            return {}

        explicit_contract = {
            key: value
            for key, value in {
                "activity_owner": event.get("activity_owner"),
                "ui_phase": event.get("ui_phase"),
                "phase_reason": event.get("phase_reason"),
            }.items()
            if value is not None
        }
        if len(explicit_contract) == 3:
            return explicit_contract

        event_type = str(event.get("type") or "").strip()
        ui_phase_override = None
        activity_owner_override = None
        phase_reason = (
            str(explicit_contract.get("phase_reason") or "").strip()
            or str(event.get("terminal_reason") or "").strip()
            or None
        )
        environment_ready = None

        if event_type == "shadow_clone_environment_preparing":
            ui_phase_override = "preparing_environment"
            activity_owner_override = "shadow_clone"
            phase_reason = str(event.get("reason") or "").strip() or phase_reason
            environment_ready = False
        elif event_type == "shadow_clone_environment_recovering":
            ui_phase_override = "recovering"
            activity_owner_override = "shadow_clone"
            phase_reason = str(event.get("message") or "").strip() or phase_reason
        elif event_type == "subagent_activity":
            ui_phase_override = "subagents_running"
            activity_owner_override = "shadow_clone"
            phase_reason = phase_reason or "subagent_activity"
        elif event_type == "subagent_started":
            ui_phase_override = "subagents_running"
            activity_owner_override = "shadow_clone"
            phase_reason = phase_reason or "subagents_running"
        elif event_type == "shadow_clone_aggregating":
            ui_phase_override = "aggregating"
            phase_reason = phase_reason or "aggregate_started"
        elif event_type == "status":
            phase_reason = (
                phase_reason
                or str(event.get("reason") or "").strip()
                or str(event.get("message") or "").strip()
                or None
            )

        contract = _build_shadow_clone_contract(
            live_activity=event.get("live_activity") if isinstance(event.get("live_activity"), dict) else None,
            status=event.get("status"),
            environment_ready=environment_ready,
            ui_phase_override=ui_phase_override,
            phase_reason=phase_reason,
            activity_owner_override=activity_owner_override,
        )
        return {
            **contract,
            **explicit_contract,
        }

    def _to_sse(self, event: Dict[str, Any], thread_run_id: str) -> Dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        event_type = str(event.get("type") or "chunk")
        stream_status = event_type
        message_type = "assistant"
        is_llm_message = True
        payload_content: Dict[str, Any]

        if event_type == "assistant_text":
            stream_status = "complete"
            payload_content = {
                "role": "assistant",
                "content": str(event.get("content") or ""),
            }
        elif event_type == "shadow_clone_planning_started":
            payload_content = {
                "reason": event.get("reason"),
            }
        elif event_type == "shadow_clone_proposed":
            payload_content = {
                "subtasks": event.get("subtasks", []),
                "dependencies": event.get("dependencies", []),
            }
        elif event_type == "subagent_started":
            payload_content = {
                "subtask_id": event.get("subtask_id"),
                "role": event.get("role"),
                "attempt_index": event.get("attempt_index"),
            }
        elif event_type == "shadow_clone_environment_preparing":
            payload_content = {
                "reason": event.get("reason"),
            }
        elif event_type == "shadow_clone_environment_ready":
            payload_content = {
                "reason": event.get("reason"),
                "environment": event.get("environment", {}),
            }
        elif event_type == "shadow_clone_environment_recovering":
            payload_content = {
                "recovery_kind": event.get("recovery_kind"),
                "layer_index": event.get("layer_index"),
                "message": event.get("message"),
            }
        elif event_type in {"subagent_completed", "subagent_failed"}:
            payload_content = {
                "subtask_id": event.get("subtask_id"),
                "attempt_index": event.get("attempt_index"),
                "failure_class": event.get("failure_class"),
            }
            if event.get("role"):
                payload_content["role"] = event.get("role")
            if event.get("result_summary"):
                payload_content["result_summary"] = event.get("result_summary")
            if event.get("error"):
                payload_content["error"] = event.get("error")
        elif event_type in {
            "shadow_clone_subagent_recovering",
            "shadow_clone_subagent_wake_sent",
            "shadow_clone_subagent_resumed",
            "shadow_clone_subagent_replacement_started",
            "shadow_clone_subagent_replacement_completed",
            "shadow_clone_subagent_recovery_exhausted",
        }:
            payload_content = {
                "subtask_id": event.get("subtask_id"),
                "role": event.get("role"),
                "attempt_index": event.get("attempt_index"),
                "failure_class": event.get("failure_class"),
                "message": event.get("message"),
                "recovery_mode": event.get("recovery_mode"),
                "recovery_phase": event.get("recovery_phase"),
                "handoff_summary": event.get("handoff_summary"),
                "replacement_context_id": event.get("replacement_context_id"),
            }
        elif event_type == "heartbeat":
            payload_content = {
                "pending": event.get("pending", 0),
            }
        elif event_type == "subagent_activity":
            payload_content = {
                "subtask_id": event.get("subtask_id"),
                "sequence": event.get("sequence"),
                "role": event.get("role"),
                "message_type": event.get("message_type"),
                "content": event.get("content", {}),
                "metadata": event.get("metadata", {}),
                "created_at": event.get("created_at"),
                "updated_at": event.get("updated_at"),
            }
        elif event_type in {"shadow_clone_aggregating", "shadow_clone_complete"}:
            payload_content = {}
        else:
            payload_content = {
                "event": event,
            }

        live_activity = event.get("live_activity")
        if isinstance(live_activity, dict):
            payload_content["live_activity"] = live_activity
        payload_content.update(
            {
                key: value
                for key, value in self._event_contract(event).items()
                if value is not None
            }
        )

        sse_payload = {
            "sequence": self._sequence,
            "message_id": str(uuid.uuid4()),
            "thread_id": self.thread_id,
            "type": message_type,
            "is_llm_message": is_llm_message,
            "content": json.dumps(payload_content, ensure_ascii=False),
            "metadata": json.dumps(
                {
                    "stream_status": stream_status,
                    "thread_run_id": thread_run_id,
                },
                ensure_ascii=False,
            ),
            "created_at": now,
            "updated_at": now,
        }
        self._sequence += 1
        return sse_payload
