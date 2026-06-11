"""Helpers for escalating fatal Shadow Clone sandbox failures."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Optional


FATAL_ERROR_CODES = frozenset(
    {
        "SHADOW_CLONE_SANDBOX_CLONE_RECOVERY_REQUIRED",
        "SHADOW_CLONE_SANDBOX_POINTER_DRIFT_BLOCKED",
        "SHADOW_CLONE_SANDBOX_REATTACH_FAILED",
        "SHADOW_CLONE_SANDBOX_RECREATE_FORBIDDEN",
        "SHADOW_CLONE_SANDBOX_REPLAN_REQUIRED",
    }
)

FATAL_ERROR_MARKERS = (
    "strict single-sandbox mode forbids creating a replacement sandbox",
    "shadow clone sandbox",
    "could not be reattached for run",
    "checkpoint recovery",
    "standby clone",
)

SANDBOX_NOT_FOUND_MARKERS = (
    'doesn\'t exist or you don\'t have access to it',
    "sandbox environment not accessible",
    "sandbox not found",
    "sandbox was not found",
)


@dataclass(slots=True)
class ShadowCloneFailureSignal:
    error_code: str
    detail: str
    recoverable: bool


class ShadowCloneSandboxFatalToolError(RuntimeError):
    def __init__(
        self,
        *,
        run_id: str,
        project_id: str,
        tool_name: str,
        detail: str,
        error_code: str,
        recoverable: bool,
        binding_state: str,
        recovery_kind: str,
    ) -> None:
        self.run_id = run_id
        self.project_id = project_id
        self.tool_name = tool_name
        self.detail = detail
        self.error_code = error_code
        self.recoverable = recoverable
        self.binding_state = binding_state
        self.recovery_kind = recovery_kind
        super().__init__(detail)


def _safe_json_loads(value: str) -> dict[str, Any]:
    try:
        parsed = json.loads(value)
    except Exception:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _extract_payload_text(payload: Any) -> str:
    if payload is None:
        return ""
    if isinstance(payload, dict):
        detail = payload.get("detail")
        if isinstance(detail, dict):
            detail_message = str(detail.get("message") or "").strip()
            if detail_message:
                return detail_message
        for key in ("error", "message", "detail", "output"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
            if isinstance(value, dict):
                nested = _extract_payload_text(value)
                if nested:
                    return nested
        return ""
    if isinstance(payload, str):
        text = payload.strip()
        if not text:
            return ""
        if text.startswith("{") and text.endswith("}"):
            nested = _extract_payload_text(_safe_json_loads(text))
            if nested:
                return nested
        return text
    return str(payload).strip()


def _extract_payload_error_code(payload: Any) -> str:
    if isinstance(payload, dict):
        detail = payload.get("detail")
        if isinstance(detail, dict):
            detail_code = str(detail.get("error_code") or "").strip()
            if detail_code:
                return detail_code
        for key in ("error_code", "code"):
            value = str(payload.get(key) or "").strip()
            if value:
                return value
        for key in ("error", "message", "detail", "output"):
            value = payload.get(key)
            if isinstance(value, dict):
                nested = _extract_payload_error_code(value)
                if nested:
                    return nested
            elif isinstance(value, str) and value.startswith("{") and value.endswith("}"):
                nested = _extract_payload_error_code(_safe_json_loads(value))
                if nested:
                    return nested
        return ""
    if isinstance(payload, str):
        text = payload.strip()
        if text.startswith("{") and text.endswith("}"):
            return _extract_payload_error_code(_safe_json_loads(text))
    return ""


def extract_shadow_clone_failure_signal(
    *,
    error: Optional[BaseException] = None,
    payload: Any = None,
) -> Optional[ShadowCloneFailureSignal]:
    detail = ""
    error_code = ""
    recoverable = False

    if error is not None:
        detail = str(error).strip()
        error_code = str(getattr(error, "error_code", "") or "").strip()
        recoverable = bool(getattr(error, "recoverable", False))

        if error_code in FATAL_ERROR_CODES and detail:
            return ShadowCloneFailureSignal(
                error_code=error_code,
                detail=detail,
                recoverable=recoverable,
            )

    payload_error_code = _extract_payload_error_code(payload)
    payload_detail = _extract_payload_text(payload)

    if payload_error_code in FATAL_ERROR_CODES and payload_detail:
        return ShadowCloneFailureSignal(
            error_code=payload_error_code,
            detail=payload_detail,
            recoverable=payload_error_code == "SHADOW_CLONE_SANDBOX_CLONE_RECOVERY_REQUIRED",
        )

    normalized_text = (detail or payload_detail or "").strip()
    normalized_lower = normalized_text.lower()
    if not normalized_lower:
        return None

    if "shadow clone sandbox" not in normalized_lower and not any(
        marker in normalized_lower for marker in SANDBOX_NOT_FOUND_MARKERS
    ):
        return None

    if (
        "standby clone" in normalized_lower
        or "checkpoint recovery" in normalized_lower
        or "shadow_clone_sandbox_clone_recovery_required" in normalized_lower
    ):
        return ShadowCloneFailureSignal(
            error_code="SHADOW_CLONE_SANDBOX_CLONE_RECOVERY_REQUIRED",
            detail=normalized_text,
            recoverable=True,
        )

    if any(marker in normalized_lower for marker in FATAL_ERROR_MARKERS) or any(
        marker in normalized_lower for marker in SANDBOX_NOT_FOUND_MARKERS
    ):
        return ShadowCloneFailureSignal(
            error_code="SHADOW_CLONE_SANDBOX_REATTACH_FAILED",
            detail=normalized_text,
            recoverable=False,
        )

    return None


async def maybe_raise_shadow_clone_fatal_tool_error(
    *,
    run_id: Optional[str],
    project_id: str,
    strict_sandbox: bool,
    tool_name: str,
    error: Optional[BaseException] = None,
    payload: Any = None,
) -> None:
    if not strict_sandbox or not run_id:
        return

    signal = extract_shadow_clone_failure_signal(error=error, payload=payload)
    if signal is None:
        return

    from .sandbox_lease import get_run_sandbox_lease, update_run_sandbox_lease

    lease = await get_run_sandbox_lease(run_id)
    standby_available = bool(str((lease or {}).get("standby_sandbox_id") or "").strip())
    binding_state = (
        "recovery_required"
        if signal.recoverable or standby_available
        else "lost"
    )
    recovery_kind = "clone" if binding_state == "recovery_required" else "replan"

    if lease is not None:
        current_binding_state = str(lease.get("binding_state") or "").strip().lower()
        if current_binding_state not in {"lost", "recovering", "recovery_required"}:
            await update_run_sandbox_lease(
                run_id,
                binding_state=binding_state,
                last_error=signal.detail,
            )
        elif not str(lease.get("last_error") or "").strip():
            await update_run_sandbox_lease(
                run_id,
                last_error=signal.detail,
            )

    raise ShadowCloneSandboxFatalToolError(
        run_id=run_id,
        project_id=project_id,
        tool_name=tool_name,
        detail=signal.detail,
        error_code=signal.error_code,
        recoverable=signal.recoverable or standby_available,
        binding_state=binding_state,
        recovery_kind=recovery_kind,
    )
