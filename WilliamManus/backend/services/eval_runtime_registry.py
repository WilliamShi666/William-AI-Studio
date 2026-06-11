from __future__ import annotations

from dataclasses import dataclass, field
from threading import Lock
from typing import Any

from utils.logger import logger


@dataclass
class EvalRuntimeRecord:
    trace_id: str
    model_generations: list[dict[str, Any]] = field(default_factory=list)
    tool_events: list[dict[str, Any]] = field(default_factory=list)
    safety_events: list[dict[str, Any]] = field(default_factory=list)
    ttft_seconds: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


_REGISTRY_LOCK = Lock()
_REGISTRY: dict[str, EvalRuntimeRecord] = {}


def _normalize_run_id(run_id: str | None) -> str:
    normalized = str(run_id or "").strip()
    if not normalized:
        raise ValueError("run_id is required")
    return normalized


def start_eval_run(
    *,
    run_id: str,
    trace_id: str,
    metadata: dict[str, Any] | None = None,
) -> EvalRuntimeRecord:
    normalized_run_id = _normalize_run_id(run_id)
    record = EvalRuntimeRecord(
        trace_id=str(trace_id or normalized_run_id).strip() or normalized_run_id,
        metadata=dict(metadata or {}),
    )
    with _REGISTRY_LOCK:
        _REGISTRY[normalized_run_id] = record
    return record


def update_eval_run_metadata(run_id: str, **metadata: Any) -> None:
    normalized_run_id = _normalize_run_id(run_id)
    with _REGISTRY_LOCK:
        record = _REGISTRY.get(normalized_run_id)
        if record is None:
            return
        for key, value in metadata.items():
            if value is None:
                continue
            record.metadata[str(key)] = value


def record_model_generation(
    run_id: str,
    *,
    model: str,
    input_tokens: int,
    output_tokens: int,
) -> None:
    normalized_run_id = _normalize_run_id(run_id)
    payload = {
        "model": str(model or ""),
        "input_tokens": max(0, int(input_tokens or 0)),
        "output_tokens": max(0, int(output_tokens or 0)),
    }
    with _REGISTRY_LOCK:
        record = _REGISTRY.get(normalized_run_id)
        if record is None:
            logger.warning(
                "[eval_runtime_registry] record_model_generation dropped: no eval run registered for run_id=%s",
                normalized_run_id,
            )
            return
        record.model_generations.append(payload)


def record_tool_event(
    run_id: str,
    *,
    tool_name: str,
    success: bool,
) -> None:
    normalized_run_id = _normalize_run_id(run_id)
    payload = {
        "tool_name": str(tool_name or "").strip(),
        "success": bool(success),
    }
    with _REGISTRY_LOCK:
        record = _REGISTRY.get(normalized_run_id)
        if record is None:
            logger.warning(
                "[eval_runtime_registry] record_tool_event dropped: no eval run registered for run_id=%s tool=%s",
                normalized_run_id,
                str(tool_name or "").strip(),
            )
            return
        record.tool_events.append(payload)


def record_safety_event(
    run_id: str,
    *,
    tool_name: str,
    safety: dict[str, Any],
) -> None:
    normalized_run_id = _normalize_run_id(run_id)
    payload = {
        "tool_name": str(tool_name or "").strip(),
        "safety": dict(safety or {}),
    }
    with _REGISTRY_LOCK:
        record = _REGISTRY.get(normalized_run_id)
        if record is None:
            return
        record.safety_events.append(payload)


def record_ttft_seconds(run_id: str, value: float | int | None) -> None:
    normalized_run_id = _normalize_run_id(run_id)
    if value is None:
        return
    numeric_value = float(value)
    if numeric_value < 0:
        return
    with _REGISTRY_LOCK:
        record = _REGISTRY.get(normalized_run_id)
        if record is None or record.ttft_seconds is not None:
            return
        record.ttft_seconds = numeric_value


def pop_eval_run(run_id: str) -> EvalRuntimeRecord | None:
    normalized_run_id = _normalize_run_id(run_id)
    with _REGISTRY_LOCK:
        return _REGISTRY.pop(normalized_run_id, None)
