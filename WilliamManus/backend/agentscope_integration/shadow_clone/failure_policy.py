"""Failure classification and conservative retry policy for Shadow Clone."""

from __future__ import annotations

from typing import Optional

from .recovery_escalation import extract_shadow_clone_failure_signal


FAILURE_CLASS_SANDBOX_CONTINUITY = "sandbox_continuity"
FAILURE_CLASS_TIMEOUT = "timeout"
FAILURE_CLASS_PROVIDER_RESPONSE_PARSE = "provider_response_parse"
FAILURE_CLASS_PROVIDER_POLICY = "provider_policy"
FAILURE_CLASS_TOOLING = "tooling"
FAILURE_CLASS_TASK_EXECUTION = "task_execution"
FAILURE_CLASS_UNKNOWN = "unknown"

_RETRYABLE_FAILURE_CLASSES = {
    FAILURE_CLASS_TIMEOUT,
}

_RUNTIME_RECOVERABLE_FAILURE_CLASSES = {
    FAILURE_CLASS_TIMEOUT,
    FAILURE_CLASS_PROVIDER_RESPONSE_PARSE,
    FAILURE_CLASS_TASK_EXECUTION,
    FAILURE_CLASS_UNKNOWN,
}


def classify_failure_text(error_text: str | None) -> str:
    text = str(error_text or "").strip()
    if not text:
        return FAILURE_CLASS_UNKNOWN

    signal = extract_shadow_clone_failure_signal(payload=text)
    if signal is not None:
        return FAILURE_CLASS_SANDBOX_CONTINUITY

    normalized = text.lower()

    if "timed out" in normalized or normalized == "timeout":
        return FAILURE_CLASS_TIMEOUT

    if (
        "expecting value:" in normalized
        or "jsondecodeerror" in normalized
        or "failed to parse" in normalized
    ):
        return FAILURE_CLASS_PROVIDER_RESPONSE_PARSE

    if (
        "datainspectionfailed" in normalized
        or "moderation" in normalized
        or "provider policy" in normalized
        or "safety system" in normalized
        or "refus" in normalized
    ):
        return FAILURE_CLASS_PROVIDER_POLICY

    if (
        "html2pptx" in normalized
        or "cannot find module" in normalized
        or "module not found" in normalized
        or "no such file" in normalized
        or "does not exist" in normalized
        or "not found" in normalized
    ):
        return FAILURE_CLASS_TOOLING

    return FAILURE_CLASS_TASK_EXECUTION


def is_retryable_failure_class(failure_class: Optional[str]) -> bool:
    return str(failure_class or "").strip().lower() in _RETRYABLE_FAILURE_CLASSES


def is_runtime_recoverable_failure_class(failure_class: Optional[str]) -> bool:
    return (
        str(failure_class or "").strip().lower()
        in _RUNTIME_RECOVERABLE_FAILURE_CLASSES
    )
