"""Post-run review pipeline for long-term memory recording."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from typing import Any, Sequence

from agentscope.formatter import OpenAIChatFormatter
from agentscope.message import Msg
from agentscope.model import OpenAIChatModel

from agentscope_integration.models.model_factory import ModelFactory
from services import redis
from utils.logger import logger

from .long_term import create_long_term_memory, load_ltm_settings_from_env
from .review_types import ReviewResult, ReviewSettings, RunMetrics

_REVIEW_TASKS: dict[str, asyncio.Task] = {}


def _env_flag(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _read_int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _read_float_env(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _normalize_terminal_status(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if normalized == "error":
        return "failed"
    if normalized in {"running", "completed", "failed", "stopped"}:
        return normalized
    return "running"


def _normalize_review_model_name(model_name: str) -> str:
    normalized = str(model_name or "").strip()
    lowered = normalized.lower()
    if lowered in {"minimax-m2.5", "minimax/m2.5", "minimax-m25"}:
        return "MiniMax-M2.5"
    return normalized


def _normalized_string(value: Any) -> str:
    return str(value or "").strip()


def _build_review_kv_cache_options() -> dict[str, Any]:
    provider_mode = _normalized_string(
        os.getenv("AGENTSCOPE_KV_CACHE_PROVIDER_MODE", "openrouter"),
    ).lower()
    enabled = _env_flag("AGENTSCOPE_KV_CACHE_ENABLED", True) and provider_mode == "openrouter"
    breakpoint_mode = _normalized_string(
        os.getenv("AGENTSCOPE_KV_CACHE_BREAKPOINT_MODE", "auto"),
    ).lower()
    if breakpoint_mode not in {"auto", "manual"}:
        breakpoint_mode = "auto"

    return {
        "enabled": enabled,
        "provider_mode": provider_mode or "openrouter",
        "breakpoint_mode": breakpoint_mode,
        "session_sticky": _env_flag("AGENTSCOPE_KV_CACHE_SESSION_STICKY", True),
        "session_ttl_seconds": max(
            0,
            _read_int_env("AGENTSCOPE_KV_CACHE_SESSION_TTL_SECONDS", 7200),
        ),
        "canonical_json": _env_flag("AGENTSCOPE_KV_CACHE_CANONICAL_JSON", True),
        "metrics_enabled": _env_flag("AGENTSCOPE_KV_CACHE_METRICS_ENABLED", True),
        "shadow_log_only": _env_flag("AGENTSCOPE_KV_CACHE_SHADOW_LOG_ONLY", False),
        "min_prefix_tokens": max(
            0,
            _read_int_env("AGENTSCOPE_KV_CACHE_MIN_PREFIX_TOKENS", 2048),
        ),
        "cache_contract_version": _normalized_string(
            os.getenv("AGENTSCOPE_KV_CACHE_CONTRACT_VERSION", "v1"),
        ).lower()
        or "v1",
    }


def _resolve_review_model_key(
    *,
    settings: ReviewSettings,
    run_model_key: str | None,
    cache_context: dict[str, str] | None,
) -> str:
    if settings.use_run_model:
        candidate = _normalized_string(run_model_key)
        if candidate:
            return candidate

        if cache_context:
            for key in ("model_key", "resolved_model_key"):
                candidate = _normalized_string(cache_context.get(key))
                if candidate:
                    return candidate

    return _normalized_string(settings.model_name)


def load_review_settings_from_env() -> ReviewSettings:
    """Load post-run review settings from environment variables."""

    model_name = str(
        os.getenv("AGENTSCOPE_LTM_REVIEW_MODEL", "minimax-m2.5"),
    ).strip() or "minimax-m2.5"
    return ReviewSettings(
        enabled=_env_flag("AGENTSCOPE_LTM_REVIEW_ENABLED", False),
        use_run_model=_env_flag("AGENTSCOPE_LTM_REVIEW_USE_RUN_MODEL", True),
        tool_failure_threshold=max(
            1,
            _read_int_env("AGENTSCOPE_LTM_REVIEW_TOOL_FAILURE_THRESHOLD", 3),
        ),
        min_responses=max(
            0,
            _read_int_env("AGENTSCOPE_LTM_REVIEW_MIN_RESPONSES", 5),
        ),
        confidence_threshold=min(
            1.0,
            max(
                0.0,
                _read_float_env("AGENTSCOPE_LTM_REVIEW_CONFIDENCE", 0.65),
            ),
        ),
        max_context_chars=max(
            1000,
            _read_int_env("AGENTSCOPE_LTM_REVIEW_MAX_CONTEXT_CHARS", 15000),
        ),
        review_timeout_seconds=max(
            5.0,
            _read_float_env("AGENTSCOPE_LTM_REVIEW_TIMEOUT", 60.0),
        ),
        model_name=model_name,
        lock_ttl_seconds=max(
            300,
            _read_int_env("AGENTSCOPE_LTM_REVIEW_LOCK_TTL_SECONDS", 86400),
        ),
        force_record_on_high_friction=_env_flag(
            "AGENTSCOPE_LTM_REVIEW_FORCE_RECORD_ON_HIGH_FRICTION",
            False,
        ),
    )


def _json_object(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            return {}
    return {}


def _text(value: Any, limit: int = 400) -> str:
    normalized = " ".join(str(value or "").split()).strip()
    if not normalized:
        return ""
    return normalized[:limit]


def _parse_iso(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        dt = datetime.fromisoformat(raw)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:
        return None


def _decode_responses(raw_entries: Sequence[Any]) -> list[dict]:
    decoded: list[dict] = []
    for raw in raw_entries or []:
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        if isinstance(raw, str):
            try:
                payload = json.loads(raw)
            except Exception:
                continue
        elif isinstance(raw, dict):
            payload = raw
        else:
            continue
        if isinstance(payload, dict):
            decoded.append(payload)
    return decoded


def _extract_cache_context(
    responses: Sequence[dict],
    *,
    thread_id: str,
) -> dict[str, str]:
    context = {
        "thread_id": _normalized_string(thread_id),
        "thread_run_id": "",
        "session_id": "",
        "model_key": "",
        "resolved_model_key": "",
    }

    for payload in reversed(list(responses or [])):
        metadata = _json_object(payload.get("metadata"))
        if not metadata:
            continue

        if not context["thread_run_id"]:
            context["thread_run_id"] = _normalized_string(metadata.get("thread_run_id"))
        if not context["session_id"]:
            context["session_id"] = _normalized_string(
                metadata.get("kv_cache_session_id")
                or metadata.get("cache_session_id")
                or metadata.get("prompt_cache_key"),
            )
        if False:  # epoch_id removed
            context["epoch_id"] = _normalized_string(
                metadata.get("cache_epoch_id")
                or metadata.get("epoch_id"),
            )
        if not context["model_key"]:
            context["model_key"] = _normalized_string(
                metadata.get("model_key")
                or metadata.get("review_model_key"),
            )
        if not context["resolved_model_key"]:
            context["resolved_model_key"] = _normalized_string(
                metadata.get("resolved_model_key"),
            )

        if context["session_id"] and context["model_key"]:
            break

    return context


def _extract_events(responses: Sequence[dict]) -> list[dict]:
    events: list[dict] = []
    for idx, payload in enumerate(responses or []):
        content = _json_object(payload.get("content"))
        metadata = _json_object(payload.get("metadata"))
        stream_status = str(metadata.get("stream_status") or "").strip().lower()
        event = {
            "index": idx,
            "type": str(payload.get("type") or "").strip().lower(),
            "status": _normalize_terminal_status(payload.get("status")),
            "created_at": str(payload.get("created_at") or ""),
            "stream_status": stream_status,
            "status_type": str(content.get("status_type") or "").strip().lower(),
            "function_name": str(content.get("function_name") or "").strip(),
            "message": _text(
                content.get("message")
                or content.get("result")
                or content.get("content")
                or payload.get("message")
                or "",
                limit=320,
            ),
            "tool_call_ids": [],
            "tool_result_error": False,
        }
        tool_calls = metadata.get("tool_calls")
        if isinstance(tool_calls, list):
            for item in tool_calls:
                if not isinstance(item, dict):
                    continue
                tool_call_id = str(item.get("id") or "").strip()
                if tool_call_id:
                    event["tool_call_ids"].append(tool_call_id)
                if not event["function_name"]:
                    function = item.get("function")
                    if isinstance(function, dict):
                        event["function_name"] = str(function.get("name") or "").strip()

        content_tool_call_id = str(content.get("tool_call_id") or "").strip()
        if content_tool_call_id:
            event["tool_call_ids"].append(content_tool_call_id)

        if not event["function_name"]:
            event["function_name"] = str(content.get("tool_name") or "").strip()

        if event["type"] == "tool":
            raw_result = content.get("result")
            result_obj = _json_object(raw_result)
            error_text = _text(result_obj.get("error"), limit=260)
            if not error_text and isinstance(raw_result, dict):
                error_text = _text(raw_result.get("error"), limit=260)
            if error_text:
                event["tool_result_error"] = True
                event["message"] = error_text
            elif isinstance(raw_result, str):
                lowered = raw_result.lower()
                if "\"error\"" in lowered or "command exited with code" in lowered:
                    event["tool_result_error"] = True

        events.append(event)
    return events


def extract_run_metrics(
    responses: Sequence[dict],
    *,
    final_status: str,
) -> RunMetrics:
    """Extract deterministic run metrics from one response stream."""

    events = _extract_events(responses)
    tool_call_ids: set[str] = set()
    tool_failures = 0
    max_consecutive_failures = 0
    consecutive_failures = 0
    tool_error_details: list[dict] = []
    timestamps: list[datetime] = []

    for event in events:
        created_at = _parse_iso(event.get("created_at"))
        if created_at:
            timestamps.append(created_at)

        for tool_call_id in event.get("tool_call_ids") or []:
            if tool_call_id:
                tool_call_ids.add(str(tool_call_id))

        status_type = str(event.get("status_type") or "")
        if event.get("type") == "tool":
            if event.get("tool_result_error"):
                tool_failures += 1
                consecutive_failures += 1
                max_consecutive_failures = max(max_consecutive_failures, consecutive_failures)
                if len(tool_error_details) < 32:
                    tool_error_details.append(
                        {
                            "index": event.get("index"),
                            "tool_name": event.get("function_name") or "unknown_tool",
                            "error": _text(event.get("message"), limit=260),
                        },
                    )
            else:
                consecutive_failures = 0
            continue

        if status_type == "tool_failed":
            tool_failures += 1
            consecutive_failures += 1
            max_consecutive_failures = max(max_consecutive_failures, consecutive_failures)
            if len(tool_error_details) < 32:
                tool_error_details.append(
                    {
                        "index": event.get("index"),
                        "tool_name": event.get("function_name") or "unknown_tool",
                        "error": _text(event.get("message"), limit=260),
                    },
                )
            continue
        if status_type == "tool_completed":
            consecutive_failures = 0
            continue

        if event.get("type") == "status" and event.get("status") in {"failed", "error"}:
            if len(tool_error_details) < 32:
                tool_error_details.append(
                    {
                        "index": event.get("index"),
                        "tool_name": "run_terminal_status",
                        "error": _text(event.get("message"), limit=260),
                    },
                )

    duration_seconds = 0.0
    if len(timestamps) >= 2:
        duration_seconds = max(
            0.0,
            (max(timestamps) - min(timestamps)).total_seconds(),
        )

    return RunMetrics(
        total_tool_calls=len(tool_call_ids),
        tool_failures=tool_failures,
        max_consecutive_failures=max_consecutive_failures,
        tool_error_details=tool_error_details,
        total_responses=len(responses or []),
        final_status=_normalize_terminal_status(final_status),
        duration_seconds=duration_seconds,
    )


def should_review(metrics: RunMetrics, settings: ReviewSettings) -> bool:
    """Heuristic gate to avoid LLM calls for smooth runs."""

    if metrics.final_status == "stopped":
        return False
    if metrics.final_status == "failed":
        return True
    if metrics.total_responses < settings.min_responses:
        return False
    if metrics.tool_failures >= settings.tool_failure_threshold:
        return True
    if metrics.max_consecutive_failures >= 2:
        return True
    return False


def _event_line(event: dict) -> str:
    parts = [f"#{event.get('index', -1)}", f"type={event.get('type') or 'unknown'}"]
    stream_status = str(event.get("stream_status") or "").strip()
    if stream_status:
        parts.append(f"stream={stream_status}")
    status = str(event.get("status") or "").strip()
    if status and status != "running":
        parts.append(f"status={status}")
    status_type = str(event.get("status_type") or "").strip()
    if status_type:
        parts.append(f"status_type={status_type}")
    function_name = str(event.get("function_name") or "").strip()
    if function_name:
        parts.append(f"tool={function_name}")
    if event.get("tool_result_error"):
        parts.append("tool_result_error=true")
    message = _text(event.get("message"), limit=240)
    line = " | ".join(parts)
    if message:
        line = f"{line} | msg={message}"
    return line


def build_review_context(
    responses: Sequence[dict],
    metrics: RunMetrics,
    max_chars: int,
) -> str:
    """Build compact failure-centric review context with bounded size."""

    max_chars = max(128, int(max_chars or 15000))
    events = _extract_events(responses)
    if not events:
        return ""

    failure_indices: list[int] = []
    terminal_indices: list[int] = []
    recovery_indices: list[int] = []

    for idx, event in enumerate(events):
        status_type = str(event.get("status_type") or "")
        if status_type == "tool_failed" or bool(event.get("tool_result_error")):
            failure_indices.append(idx)
        if event.get("type") == "status" and event.get("status") in {"completed", "failed", "stopped"}:
            terminal_indices.append(idx)

    for fail_idx in failure_indices:
        for probe in range(fail_idx + 1, min(len(events), fail_idx + 7)):
            next_status_type = str(events[probe].get("status_type") or "")
            if next_status_type == "tool_completed":
                recovery_indices.append(probe)
                break
            if events[probe].get("type") == "tool" and not events[probe].get("tool_result_error"):
                recovery_indices.append(probe)
                break

    selected_indices: set[int] = set()
    for idx in failure_indices:
        start = max(0, idx - 2)
        end = min(len(events), idx + 3)
        selected_indices.update(range(start, end))

    selected_indices.update(recovery_indices)
    selected_indices.update(terminal_indices[-2:])

    if not selected_indices:
        tail_count = min(12, len(events))
        selected_indices.update(range(len(events) - tail_count, len(events)))

    ordered_lines = [_event_line(events[idx]) for idx in sorted(selected_indices)]
    header = (
        f"Run status={metrics.final_status}; total_responses={metrics.total_responses}; "
        f"tool_calls={metrics.total_tool_calls}; tool_failures={metrics.tool_failures}; "
        f"max_consecutive_failures={metrics.max_consecutive_failures}; "
        f"duration_seconds={metrics.duration_seconds:.2f}"
    )
    context = f"{header}\n\n" + "\n".join(ordered_lines)
    if len(context) <= max_chars:
        return context

    trim_hint = f"[truncated {len(context) - max_chars} chars from head]\n"
    tail = context[-(max_chars - len(trim_hint)) :]
    return f"{trim_hint}{tail}"


def _extract_json_object_from_text(text: str) -> dict:
    raw = str(text or "").strip()
    if not raw:
        return {}

    if raw.startswith("```"):
        lines = raw.splitlines()
        if len(lines) >= 3:
            raw = "\n".join(lines[1:-1]).strip()

    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass

    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        try:
            parsed = json.loads(raw[start : end + 1])
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            return {}
    return {}


def _normalize_string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        text_value = _text(item, limit=4000)
        if text_value:
            out.append(text_value)
    return out


def _normalize_tool_memories(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, dict):
            serialized = json.dumps(item, ensure_ascii=False)
            serialized = _text(serialized, limit=4000)
            if serialized:
                out.append(serialized)
            continue
        text_value = _text(item, limit=4000)
        if text_value:
            out.append(text_value)
    return out


def _build_high_friction_fallback_result(
    *,
    metrics: RunMetrics,
    agent_run_id: str,
    review_result: ReviewResult,
) -> ReviewResult:
    top_errors = metrics.tool_error_details[:5]
    summarized_errors = []
    tool_memories: list[str] = []

    for detail in top_errors:
        tool_name = _text(detail.get("tool_name"), limit=80) or "unknown_tool"
        error_text = _text(detail.get("error"), limit=320) or "unknown_error"
        summarized_errors.append(f"{tool_name}: {error_text}")
        tool_memories.append(
            json.dumps(
                {
                    "tool_name": tool_name,
                    "error_signature": error_text,
                    "recovery_pattern": (
                        "After repeated failures, verify command/path assumptions and run a minimal "
                        "sanity check (pwd/ls) before retries."
                    ),
                    "success": False,
                },
                ensure_ascii=False,
            ),
        )

    if not summarized_errors:
        summarized_errors.append("repeated tool failures with limited diagnostics")

    summary = "; ".join(summarized_errors[:3])
    task_memory = (
        f"[high_friction_rule_override] run={agent_run_id} had {metrics.tool_failures} tool failures "
        f"(max_consecutive={metrics.max_consecutive_failures}). Error signatures: {summary}. "
        "Reusable strategy: switch to minimal validation commands, then retry with corrected inputs."
    )

    return ReviewResult(
        should_record=True,
        confidence=max(0.9, review_result.confidence),
        task_memories=[task_memory],
        tool_memories=tool_memories,
        reasons=[*review_result.reasons, "high_friction_rule_override"],
        raw_response=review_result.raw_response,
    )


def _safe_getattr(obj: Any, attr: str) -> Any:
    try:
        return getattr(obj, attr)
    except Exception:
        return None


async def _collect_final_chat_response(response: Any) -> Any:
    if response is None:
        return None
    aiter = _safe_getattr(response, "__aiter__")
    if callable(aiter):
        try:
            last = None
            async for item in response:
                last = item
            return last if last is not None else response
        except Exception as exc:
            logger.debug("Post-run review async response iteration fallback: %s", exc)
            return response
    return response


def _chat_response_to_text(response: Any) -> str:
    if response is None:
        return ""
    get_text_content = _safe_getattr(response, "get_text_content")
    if callable(get_text_content):
        try:
            text = get_text_content()
            return str(text or "").strip()
        except Exception:
            pass
    content = _safe_getattr(response, "content")
    if isinstance(content, list):
        lines: list[str] = []
        for block in content:
            if isinstance(block, dict):
                text = _text(block.get("text"), limit=10000)
                if text:
                    lines.append(text)
        if lines:
            return "\n".join(lines).strip()
    return _text(response, limit=12000)


def _build_review_user_prompt(metrics: RunMetrics, review_context: str) -> str:
    return (
        "Analyze this run and decide if reusable long-term memory should be recorded.\n\n"
        f"Run Metrics:\n"
        f"- final_status: {metrics.final_status}\n"
        f"- total_tool_calls: {metrics.total_tool_calls}\n"
        f"- tool_failures: {metrics.tool_failures}\n"
        f"- max_consecutive_failures: {metrics.max_consecutive_failures}\n"
        f"- total_responses: {metrics.total_responses}\n"
        f"- duration_seconds: {metrics.duration_seconds:.2f}\n\n"
        "Failure-Centric Context:\n"
        f"{review_context}\n\n"
        "Return ONLY valid JSON with keys:\n"
        "{\n"
        '  "should_record": boolean,\n'
        '  "confidence": number,\n'
        '  "reasons": [string],\n'
        '  "task_memories": [string],\n'
        '  "tool_memories": [string or object]\n'
        "}\n"
        "Rules:\n"
        "- Use should_record=false for smooth/low-learning runs.\n"
        "- task_memories should be concise, reusable strategy learnings.\n"
        "- tool_memories should capture failure signatures, recovery patterns, and reliable tool practices.\n"
    )


def _sync_review_model_cache_context(model: Any, cache_context: dict[str, str] | None) -> None:
    if not cache_context or not hasattr(model, "set_cache_context"):
        return
    thread_id = _normalized_string(cache_context.get("thread_id"))
    session_id = _normalized_string(cache_context.get("session_id"))

    try:
        model.set_cache_context(  # type: ignore[attr-defined]
            thread_id=thread_id,
            session_id=session_id,
        )
        return
    except TypeError:
        pass
    except Exception as exc:
        logger.debug("Failed to sync review cache context with session_id: %s", exc)
        return

    try:
        model.set_cache_context(  # type: ignore[attr-defined]
            thread_id=thread_id,
        )
    except Exception as exc:
        logger.debug("Failed to sync review cache context: %s", exc)


def _create_review_model_and_formatter(
    *,
    model_key_or_name: str,
) -> tuple[Any, Any]:
    candidate = _normalized_string(model_key_or_name)
    if not candidate:
        raise ValueError("Review model key is empty")

    kv_cache_options = _build_review_kv_cache_options()
    available = set(ModelFactory.get_available_models())
    if candidate in available:
        return ModelFactory.create(
            candidate,
            stream=False,
            kv_cache_options=kv_cache_options,
        )

    return ModelFactory.create_from_full_name(
        candidate,
        stream=False,
        kv_cache_options=kv_cache_options,
    )


async def _invoke_review_llm(
    *,
    review_context: str,
    metrics: RunMetrics,
    settings: ReviewSettings,
    run_model_key: str | None = None,
    cache_context: dict[str, str] | None = None,
) -> str:
    model = None
    formatter = None
    resolved_model_key = _resolve_review_model_key(
        settings=settings,
        run_model_key=run_model_key,
        cache_context=cache_context,
    )
    try:
        model, formatter = _create_review_model_and_formatter(
            model_key_or_name=resolved_model_key,
        )
        _sync_review_model_cache_context(model, cache_context)
    except Exception as exc:
        logger.warning(
            "Post-run review model-factory path unavailable (model=%s). Fallback to direct OpenAIChatModel. err=%s",
            resolved_model_key,
            exc,
        )

    if model is None or formatter is None:
        ltm_settings = load_ltm_settings_from_env()
        if not ltm_settings.api_key:
            raise RuntimeError(
                "Post-run review requires an API key in model envs or AGENTSCOPE_LTM_API_KEY.",
            )
        fallback_model_name = _normalize_review_model_name(
            _normalized_string(settings.model_name) or _normalized_string(ltm_settings.model_name),
        )
        if not fallback_model_name:
            raise RuntimeError("Post-run review model is empty after normalization.")
        model = OpenAIChatModel(
            model_name=fallback_model_name,
            api_key=ltm_settings.api_key,
            stream=False,
            client_kwargs={"base_url": ltm_settings.api_base},
        )
        formatter = OpenAIChatFormatter()

    formatted_messages = await formatter.format(
        msgs=[
            Msg(
                name="system",
                role="system",
                content=(
                    "You are a strict run reviewer for long-term memory curation. "
                    "Output must be valid JSON only."
                ),
            ),
            Msg(
                name="user",
                role="user",
                content=_build_review_user_prompt(metrics, review_context),
            ),
        ],
    )

    response = await model(
        messages=formatted_messages,
        tools=None,
        tool_choice="none",
    )
    final_response = await _collect_final_chat_response(response)
    return _chat_response_to_text(final_response)


async def generate_memories(
    review_context: str,
    metrics: RunMetrics,
    settings: ReviewSettings,
    *,
    run_model_key: str | None = None,
    cache_context: dict[str, str] | None = None,
) -> ReviewResult:
    """Call review model and parse structured review output."""

    raw_response = await _invoke_review_llm(
        review_context=review_context,
        metrics=metrics,
        settings=settings,
        run_model_key=run_model_key,
        cache_context=cache_context,
    )
    payload = _extract_json_object_from_text(raw_response)
    if not payload:
        return ReviewResult(
            should_record=False,
            confidence=0.0,
            raw_response=raw_response,
        )

    confidence = _safe_float(
        payload.get("confidence", payload.get("decision_confidence", 0.0)),
        default=0.0,
    )
    return ReviewResult(
        should_record=bool(payload.get("should_record", False)),
        confidence=max(0.0, min(1.0, confidence)),
        task_memories=_normalize_string_list(payload.get("task_memories")),
        tool_memories=_normalize_tool_memories(payload.get("tool_memories")),
        reasons=_normalize_string_list(payload.get("reasons")),
        raw_response=raw_response,
    )


async def _acquire_review_lock(agent_run_id: str, ttl_seconds: int) -> bool:
    lock_key = f"post_run_review_lock:{agent_run_id}"
    try:
        acquired = await redis.set(
            lock_key,
            datetime.now(timezone.utc).isoformat(),
            nx=True,
            ex=max(300, int(ttl_seconds or 86400)),
        )
        return bool(acquired)
    except Exception as exc:
        # Keep fail-open for task completion path.
        logger.warning(
            "Post-run review lock acquisition failed for run=%s, continue fail-open: %s",
            agent_run_id,
            exc,
        )
        return True


async def execute_post_run_review(
    *,
    responses_json: Sequence[Any],
    thread_id: str,
    agent_run_id: str,
    final_status: str,
    run_model_key: str | None = None,
    settings: ReviewSettings | None = None,
) -> None:
    """Execute one non-blocking post-run review flow."""

    cfg = settings or load_review_settings_from_env()
    if not cfg.enabled:
        return
    if not await _acquire_review_lock(agent_run_id, cfg.lock_ttl_seconds):
        logger.info(
            "Skip duplicate post-run review for run=%s (idempotency lock exists).",
            agent_run_id,
        )
        return

    decoded = _decode_responses(responses_json)
    cache_context = _extract_cache_context(decoded, thread_id=thread_id)
    metrics = extract_run_metrics(
        decoded,
        final_status=final_status,
    )
    if not should_review(metrics, cfg):
        logger.info(
            "Post-run review skipped for run=%s status=%s failures=%s responses=%s",
            agent_run_id,
            metrics.final_status,
            metrics.tool_failures,
            metrics.total_responses,
        )
        return

    review_context = build_review_context(
        decoded,
        metrics,
        cfg.max_context_chars,
    )
    if not review_context:
        logger.info("Post-run review has empty context, skip run=%s", agent_run_id)
        return

    try:
        review_result = await generate_memories(
            review_context=review_context,
            metrics=metrics,
            settings=cfg,
            run_model_key=run_model_key,
            cache_context=cache_context,
        )
    except Exception as exc:
        logger.warning("Post-run review model evaluation failed run=%s: %s", agent_run_id, exc)
        review_result = ReviewResult(
            should_record=False,
            confidence=0.0,
            reasons=["review_model_error"],
            raw_response="",
        )

    override_triggered = (
        cfg.force_record_on_high_friction
        and metrics.tool_failures >= cfg.tool_failure_threshold
        and metrics.final_status in {"completed", "failed"}
    )
    if not review_result.should_record and override_triggered:
        review_result = _build_high_friction_fallback_result(
            metrics=metrics,
            agent_run_id=agent_run_id,
            review_result=review_result,
        )
        logger.info(
            "Post-run review forcing record via high-friction override run=%s failures=%s",
            agent_run_id,
            metrics.tool_failures,
        )

    if not review_result.should_record:
        logger.info("Post-run review decided not to record run=%s", agent_run_id)
        return
    if review_result.confidence < cfg.confidence_threshold:
        logger.info(
            "Post-run review confidence too low run=%s confidence=%.3f threshold=%.3f",
            agent_run_id,
            review_result.confidence,
            cfg.confidence_threshold,
        )
        return

    memory_entries = [*review_result.task_memories, *review_result.tool_memories]
    if not memory_entries:
        logger.info(
            "Post-run review produced no memory entries for run=%s (after normalization).",
            agent_run_id,
        )
        return

    ltm_settings = load_ltm_settings_from_env()
    if not ltm_settings.enabled:
        logger.info("Post-run review skipped write because LTM is disabled run=%s", agent_run_id)
        return

    ltm = create_long_term_memory(
        thread_id=thread_id,
        settings=ltm_settings,
    )
    if ltm is None:
        logger.info("Post-run review has no LTM backend to write run=%s", agent_run_id)
        return

    await ltm.__aenter__()
    try:
        thinking = (
            f"post_run_review run={agent_run_id} status={metrics.final_status} "
            f"tool_failures={metrics.tool_failures} consecutive_failures={metrics.max_consecutive_failures}"
        )
        await ltm.record_to_memory(
            thinking=thinking,
            content=memory_entries,
        )
        logger.info(
            "Post-run review wrote memories run=%s task=%s tool=%s confidence=%.3f",
            agent_run_id,
            len(review_result.task_memories),
            len(review_result.tool_memories),
            review_result.confidence,
        )
    finally:
        await ltm.__aexit__(None, None, None)


async def _guarded_review(
    *,
    responses_json: Sequence[Any],
    thread_id: str,
    agent_run_id: str,
    final_status: str,
    run_model_key: str | None,
    settings: ReviewSettings,
) -> None:
    try:
        await asyncio.wait_for(
            execute_post_run_review(
                responses_json=responses_json,
                thread_id=thread_id,
                agent_run_id=agent_run_id,
                final_status=final_status,
                run_model_key=run_model_key,
                settings=settings,
            ),
            timeout=max(5.0, settings.review_timeout_seconds),
        )
    except asyncio.TimeoutError:
        logger.warning("Post-run review timed out for run=%s", agent_run_id)
    except Exception as exc:
        logger.warning("Post-run review failed for run=%s: %s", agent_run_id, exc)


def schedule_post_run_review(
    *,
    responses_json: Sequence[Any],
    thread_id: str,
    agent_run_id: str,
    final_status: str,
    run_model_key: str | None = None,
) -> None:
    """Schedule post-run review in fire-and-forget mode."""

    settings = load_review_settings_from_env()
    if not settings.enabled:
        return
    existing = _REVIEW_TASKS.get(agent_run_id)
    if existing and not existing.done():
        return

    task = asyncio.create_task(
        _guarded_review(
            responses_json=list(responses_json or []),
            thread_id=thread_id,
            agent_run_id=agent_run_id,
            final_status=final_status,
            run_model_key=run_model_key,
            settings=settings,
        ),
        name=f"post-run-review:{agent_run_id}",
    )
    _REVIEW_TASKS[agent_run_id] = task

    def _cleanup(done_task: asyncio.Task, *, run_id: str = agent_run_id) -> None:
        current = _REVIEW_TASKS.get(run_id)
        if current is done_task:
            _REVIEW_TASKS.pop(run_id, None)
        try:
            done_task.result()
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.warning("Post-run review task callback failure run=%s: %s", run_id, exc)

    task.add_done_callback(_cleanup)
