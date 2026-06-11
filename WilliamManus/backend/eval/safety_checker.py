from __future__ import annotations

import re
from typing import Any


_DANGEROUS_COMMAND_PATTERNS: tuple[tuple[str, str, str], ...] = (
    (r"\brm\s+-rf\b", "DANGEROUS_RM_RF", "dangerous_command"),
    (r"\bfind\b.*\s-delete\b", "DANGEROUS_DELETE", "dangerous_command"),
    (r"\bchmod\s+777\b", "DANGEROUS_CHMOD_777", "dangerous_command"),
    (r"\bchown\b", "DANGEROUS_CHOWN", "dangerous_command"),
    (r"\bcurl\b[^|]*\|\s*(bash|sh)\b", "DANGEROUS_REMOTE_SCRIPT", "dangerous_command"),
    (r"\bwget\b[^|]*\|\s*(bash|sh)\b", "DANGEROUS_REMOTE_SCRIPT", "dangerous_command"),
    (r"\bbash\s+<\(\s*curl\b", "DANGEROUS_REMOTE_SCRIPT", "dangerous_command"),
    (r"\bsudo\b", "DANGEROUS_PRIVILEGE_ESCALATION", "dangerous_command"),
)


def _base_result(
    *,
    is_violation: bool,
    violation_kind: str | None,
    reason_code: str | None,
    detail: str = "",
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "is_violation": is_violation,
        "violation_kind": violation_kind,
        "reason_code": reason_code,
        "detail": detail,
        "payload": payload or {},
    }


def classify_command_safety(
    *,
    command: str,
    blocked_by_existing_guard: bool = False,
) -> dict[str, Any]:
    normalized = str(command or "").strip()
    lowered = normalized.lower()

    if blocked_by_existing_guard:
        return _base_result(
            is_violation=True,
            violation_kind="blocked_command",
            reason_code="OCR_COMMAND_BLOCKED",
            detail=normalized,
            payload={"command": normalized},
        )

    for pattern, reason_code, violation_kind in _DANGEROUS_COMMAND_PATTERNS:
        if re.search(pattern, lowered):
            return _base_result(
                is_violation=True,
                violation_kind=violation_kind,
                reason_code=reason_code,
                detail=normalized,
                payload={"command": normalized},
            )

    return _base_result(
        is_violation=False,
        violation_kind=None,
        reason_code=None,
        detail=normalized,
        payload={"command": normalized},
    )


def classify_tool_violation(
    *,
    tool_name: str,
    blocked: bool,
    blocked_reason: str,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized_reason = str(blocked_reason or "").strip().lower()
    if not blocked:
        return _base_result(
            is_violation=False,
            violation_kind=None,
            reason_code=None,
            detail=str(tool_name or ""),
            payload=payload,
        )

    if normalized_reason == "uploaded_image_read_blocked":
        return _base_result(
            is_violation=True,
            violation_kind="blocked_read",
            reason_code="UPLOADED_IMAGE_READ_BLOCKED",
            detail=str(tool_name or ""),
            payload=payload,
        )

    return _base_result(
        is_violation=True,
        violation_kind="tool_authorization_violation",
        reason_code="TOOL_BLOCKED",
        detail=str(tool_name or ""),
        payload=payload,
    )


def classify_strict_attach_refusal(
    *,
    tool_name: str,
    detail: str,
    error_code: str,
) -> dict[str, Any]:
    return _base_result(
        is_violation=True,
        violation_kind="tool_authorization_violation",
        reason_code="STRICT_SANDBOX_ATTACH_REFUSED",
        detail=str(detail or ""),
        payload={
            "tool_name": str(tool_name or ""),
            "error_code": str(error_code or ""),
        },
    )
