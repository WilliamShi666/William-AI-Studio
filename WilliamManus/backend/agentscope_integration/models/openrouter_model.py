"""
OpenRouter Chat Model for AgentScope Integration

This module provides a custom OpenAI-compatible model that handles
OpenRouter's reasoning token format (which differs from DeepSeek's format).

OpenRouter returns reasoning in:
- Non-streaming: response.choices[].message.reasoning
- Streaming: response.choices[].delta.reasoning_content (same as DeepSeek)

AgentScope's OpenAIChatModel expects `reasoning_content` (DeepSeek format),
so we need to handle the `reasoning` field for non-streaming responses.
"""

from datetime import datetime
import asyncio
import hashlib
import json
from copy import deepcopy
from typing import List, Type, AsyncGenerator, Any, Dict
from collections import OrderedDict

from openai import BadRequestError, InternalServerError, APIError
from pydantic import BaseModel
from agentscope.model import OpenAIChatModel
from agentscope.model._model_response import ChatResponse, ChatUsage
from agentscope.message import (
    ThinkingBlock,
    TextBlock,
    ToolUseBlock,
    AudioBlock,
    Base64Source,
)

from services.eval_runtime_registry import record_model_generation, record_ttft_seconds
from services.eval_runtime_registry import record_model_generation
from utils.agent_run_context import get_agent_run_context
from utils.logger import logger
from agentscope_integration.utils.tool_message_sanitizer import (
    sanitize_tool_message_sequence,
)
from agentscope_integration.utils.tool_arg_merge import (
    extract_partial_write_file_input as shared_extract_partial_write_file_input,
    merge_tool_arguments as shared_merge_tool_arguments,
    merge_write_file_arguments,
    sanitize_write_file_input,
)
from agentscope_integration.models.openrouter_reasoning_formatter import (
    OPENROUTER_REASONING_DETAILS_KEY,
)

GEMINI_FLASH_MODEL_NAME = "google/gemini-3-flash-preview"
GLM_MODEL_NAME = "z-ai/glm-4.7"
OPENROUTER_FALLBACK_MODEL_NAME = GLM_MODEL_NAME
MOONSHOT_FALLBACK_MODEL_NAME = GLM_MODEL_NAME


def _json_loads_with_repair(text: str) -> dict:
    """Load JSON with repair for malformed strings.

    Patterned after Claude Code's safeParseJSON: returns {} on failure but
    logs the failure so empty/malformed tool inputs can be tracked.
    """
    import json

    if not text or not text.strip():
        # Empty input — the model sent a tool call with no arguments.
        # This is the same path Claude Code takes: safeParseJSON("") → null → {}.
        logger.warning(
            "[OpenRouterChatModel] Empty tool-call arguments received "
            "(json_loads_empty_or_null=true raw_chars=%d)",
            len(text),
        )
        return {}

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Try to repair common issues
        try:
            import re

            # Remove trailing commas
            text = re.sub(r",\s*}", "}", text)
            text = re.sub(r",\s*]", "]", text)
            return json.loads(text)
        except Exception:
            # Try json_repair for structural fixes (unescaped newlines, etc.)
            try:
                from json_repair import repair_json

                return json.loads(repair_json(text))
            except Exception:
                logger.warning(
                    "[OpenRouterChatModel] JSON parse failure for tool-call arguments "
                    "(json_loads_repair_failed=true raw_preview=%r)",
                    text[:200],
                )
                return {}


# ── Diagnostic metrics counters (module-level for easy querying) ────
_empty_tool_call_total: int = 0
_empty_tool_call_by_tool: dict[str, int] = {}
_circuit_breaker_activations: int = 0
_empty_tooluse_blocks_emitted: int = 0


def get_empty_tool_call_stats() -> dict:
    """Return current empty-tool-call diagnostic counters."""
    return {
        "empty_tool_call_total": _empty_tool_call_total,
        "empty_tool_call_by_tool": dict(_empty_tool_call_by_tool),
        "circuit_breaker_activations": _circuit_breaker_activations,
        "empty_tooluse_blocks_emitted": _empty_tooluse_blocks_emitted,
    }


# Static fallback for tool required params.
# The primary source of truth is now the shared registry populated by
# toolkit_adapter._register_tool from function signatures.  This fallback
# ensures basic coverage during early import / before tools are registered.
_REQUIRED_TOOL_PARAMS_FALLBACK: dict[str, list[str]] = {
    "execute_command": ["command"],
    "write_file": ["path", "content"],
    "read_file": ["path"],
    "edit_file": ["path"],
    "create_tasks": ["section_title", "task_contents"],
}


def _get_required_params(tool_name: str | None) -> list[str]:
    """Return required parameter names for *tool_name*.

    Reads from the shared toolkit_adapter registry first (populated from
    function signatures at tool registration time).  Falls back to the
    static dictionary when the registry has not been populated yet.
    """
    name = str(tool_name or "")
    try:
        from agentscope_integration.tools.toolkit_adapter import (
            get_tool_required_params,
        )

        registered = get_tool_required_params(name)
        if registered:
            return registered
    except Exception:
        pass
    return _REQUIRED_TOOL_PARAMS_FALLBACK.get(name, [])


def _report_empty_tool_input(tool_name: str | None, parsed_input: dict) -> None:
    """Log and emit a trace event when a tool call has an empty or near-empty input.

    Claude Code logs ``tengu_tool_input_json_parse_fail`` events in the equivalent path;
    this serves the same purpose — making empty-tool-call failures visible in logs/traces
    so they can be tracked, alerted on, and correlated with model/provider behavior.
    """
    name = str(tool_name or "unknown")
    required = _get_required_params(name)
    missing = [
        p
        for p in required
        if not (isinstance(parsed_input, dict) and parsed_input.get(p))
    ]
    if not missing:
        return
    global _empty_tool_call_total, _empty_tool_call_by_tool
    _empty_tool_call_total += 1
    _empty_tool_call_by_tool[name] = _empty_tool_call_by_tool.get(name, 0) + 1
    logger.warning(
        "[OpenRouterChatModel] Tool call with empty/missing required params "
        "(tool_name=%s missing_params=%s parsed_keys=%s)",
        name,
        missing,
        sorted(parsed_input.keys()),
    )


def _merge_tool_arguments(previous: str, incoming: str) -> str:
    """Merge provider tool-call argument chunks supporting delta and cumulative modes."""
    if not incoming and not previous:
        logger.warning(
            "[OpenRouterChatModel] _merge_tool_arguments: "
            "both previous and incoming arguments are empty"
        )
    return shared_merge_tool_arguments(previous, incoming)


def _is_write_file_tool_name(name: Any) -> bool:
    normalized = str(name or "").replace("-", "_").lower()
    return normalized in {"write_file", "writefile"} or (
        "write" in normalized and "file" in normalized
    )


def _extract_partial_write_file_input(raw_arguments: str) -> dict[str, Any]:
    """Extract partial file path/content from an incomplete JSON argument string."""
    partial = shared_extract_partial_write_file_input(raw_arguments)
    if not partial:
        return {}

    normalized: dict[str, Any] = {}
    path_value = partial.get("path")
    if isinstance(path_value, str):
        normalized["file_path"] = path_value
    content_value = partial.get("content")
    if isinstance(content_value, str):
        normalized["content"] = content_value

    return normalized


def _extract_openrouter_reasoning_details(obj: Any) -> Any | None:
    """Read OpenRouter reasoning_details from model messages or delta objects."""
    if obj is None:
        return None

    value = (
        obj.get("reasoning_details")
        if isinstance(obj, dict)
        else getattr(
            obj,
            "reasoning_details",
            None,
        )
    )
    if value is not None:
        return deepcopy(value)

    provider_fields = (
        obj.get("provider_specific_fields")
        if isinstance(obj, dict)
        else getattr(obj, "provider_specific_fields", None)
    )
    if isinstance(provider_fields, dict):
        value = provider_fields.get("reasoning_details")
        if value is not None:
            return deepcopy(value)

    return None


class OpenRouterChatModel(OpenAIChatModel):
    """
    OpenRouter-compatible chat model that handles reasoning tokens.

    This extends AgentScope's OpenAIChatModel to properly handle
    OpenRouter's `reasoning` field in non-streaming responses.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self._lf_trace = kwargs.pop("trace", None)
        self._lf_prompt_obj = kwargs.pop("prompt", None)
        kv_cache_options = kwargs.pop("kv_cache_options", None)
        client_kwargs = kwargs.get("client_kwargs") or {}
        if isinstance(client_kwargs, dict):
            self._configured_base_url = str(
                client_kwargs.get("base_url", ""),
            ).lower()
        else:
            self._configured_base_url = ""
        if not isinstance(kv_cache_options, dict):
            kv_cache_options = {}
        self._kv_cache_enabled = bool(kv_cache_options.get("enabled", False))
        self._kv_cache_provider_mode = (
            str(
                kv_cache_options.get("provider_mode", "openrouter"),
            )
            .strip()
            .lower()
        )
        self._kv_cache_breakpoint_mode = (
            str(
                kv_cache_options.get("breakpoint_mode", "auto"),
            )
            .strip()
            .lower()
        )
        self._kv_cache_session_sticky = bool(
            kv_cache_options.get("session_sticky", True),
        )
        self._kv_cache_session_ttl_seconds = int(
            kv_cache_options.get("session_ttl_seconds", 7200) or 0,
        )
        self._kv_cache_metrics_enabled = bool(
            kv_cache_options.get("metrics_enabled", True),
        )
        self._kv_cache_shadow_log_only = bool(
            kv_cache_options.get("shadow_log_only", False),
        )
        self._kv_cache_min_prefix_tokens = int(
            kv_cache_options.get("min_prefix_tokens", 2048) or 0,
        )
        self._kv_cache_contract_version = (
            str(
                kv_cache_options.get("cache_contract_version", "v1"),
            )
            .strip()
            .lower()
            or "v1"
        )
        self._kv_cache_context: Dict[str, str] = {
            "thread_id": "",
            "session_id": "",
        }
        self._kv_cache_session_id = ""

        # CacheBoundaryHook — detects system/tool drift that breaks KV cache
        self._cache_boundary_enabled = bool(
            kv_cache_options.get("boundary_check", True),
        )
        self._cache_boundary: Any = None
        if self._cache_boundary_enabled:
            from agentscope_integration.cache.cache_boundary import (
                CacheBoundaryHook,
            )

            self._cache_boundary = CacheBoundaryHook()

        super().__init__(*args, **kwargs)

    def set_cache_context(
        self,
        *,
        thread_id: str = "",
        session_id: str = "",
    ) -> None:
        self._kv_cache_context = {
            "thread_id": str(thread_id or "").strip(),
            "session_id": str(session_id or "").strip(),
        }
        self._kv_cache_session_id = self._derive_cache_session_id()

    def _derive_cache_session_id(self) -> str:
        explicit = self._kv_cache_context.get("session_id", "")
        if explicit:
            return explicit
        if not self._kv_cache_session_sticky:
            return ""
        thread_id = self._kv_cache_context.get("thread_id", "")
        if not thread_id:
            return ""
        digest = hashlib.sha1(thread_id.encode("utf-8")).hexdigest()  # nosec B324
        return digest[:24]

    @staticmethod
    def _coerce_int(value: Any) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return 0

    def _build_kv_cache_runtime_kwargs(
        self,
        kwargs: dict[str, Any],
    ) -> tuple[dict[str, Any], bool]:
        runtime_kwargs = dict(kwargs)
        if not self._kv_cache_enabled or self._kv_cache_provider_mode != "openrouter":
            return runtime_kwargs, False

        session_id = self._kv_cache_session_id or self._derive_cache_session_id()
        if self._kv_cache_shadow_log_only:
            logger.info(
                "[OpenRouterChatModel] KV-cache shadow mode: session=%s breakpoint_mode=%s min_prefix_tokens=%s",
                session_id or "none",
                self._kv_cache_breakpoint_mode,
                self._kv_cache_min_prefix_tokens,
            )
            return runtime_kwargs, False

        extra_headers = runtime_kwargs.get("extra_headers")
        if not isinstance(extra_headers, dict):
            extra_headers = {}
        else:
            extra_headers = dict(extra_headers)

        extra_body = runtime_kwargs.get("extra_body")
        if not isinstance(extra_body, dict):
            extra_body = {}
        else:
            extra_body = dict(extra_body)

        cache_control: Dict[str, Any] = {}
        if self._kv_cache_breakpoint_mode:
            cache_control["breakpoint_mode"] = self._kv_cache_breakpoint_mode
        if self._kv_cache_session_ttl_seconds > 0:
            cache_control["session_ttl_seconds"] = self._kv_cache_session_ttl_seconds
        if self._kv_cache_min_prefix_tokens > 0:
            cache_control["min_prefix_tokens"] = self._kv_cache_min_prefix_tokens
        if self._kv_cache_contract_version:
            cache_control["contract_version"] = self._kv_cache_contract_version

        if cache_control:
            extra_body["cache_control"] = cache_control
        if session_id:
            extra_body["prompt_cache_key"] = session_id
            extra_headers["x-agentscope-cache-session"] = session_id

        if extra_body:
            runtime_kwargs["extra_body"] = extra_body
        if extra_headers:
            runtime_kwargs["extra_headers"] = extra_headers
        return runtime_kwargs, bool(extra_body or extra_headers)

    @staticmethod
    def _is_kv_cache_payload_error(error: Exception) -> bool:
        lowered = str(error).lower()
        markers = (
            "prompt_cache_key",
            "cache_control",
            "x-agentscope-cache-session",
            "unknown parameter",
            "extra_body",
        )
        return any(marker in lowered for marker in markers)

    @staticmethod
    def _strip_kv_cache_runtime_kwargs(
        kwargs: dict[str, Any],
    ) -> dict[str, Any]:
        sanitized = dict(kwargs)
        extra_body = sanitized.get("extra_body")
        if isinstance(extra_body, dict):
            clean_extra_body = dict(extra_body)
            clean_extra_body.pop("cache_control", None)
            clean_extra_body.pop("prompt_cache_key", None)
            if clean_extra_body:
                sanitized["extra_body"] = clean_extra_body
            else:
                sanitized.pop("extra_body", None)
        extra_headers = sanitized.get("extra_headers")
        if isinstance(extra_headers, dict):
            clean_headers = {
                key: value
                for key, value in extra_headers.items()
                if str(key).lower() != "x-agentscope-cache-session"
            }
            if clean_headers:
                sanitized["extra_headers"] = clean_headers
            else:
                sanitized.pop("extra_headers", None)
        return sanitized

    def _extract_kv_cache_telemetry(self, usage_obj: Any) -> dict[str, Any]:
        if usage_obj is None:
            return {}

        prompt_tokens = self._coerce_int(getattr(usage_obj, "prompt_tokens", 0))
        completion_tokens = self._coerce_int(
            getattr(usage_obj, "completion_tokens", 0),
        )
        prompt_token_details = getattr(usage_obj, "prompt_tokens_details", None)
        cached_input_tokens = 0
        if isinstance(prompt_token_details, dict):
            cached_input_tokens = self._coerce_int(
                prompt_token_details.get("cached_tokens", 0),
            )
        elif prompt_token_details is not None:
            cached_input_tokens = self._coerce_int(
                getattr(prompt_token_details, "cached_tokens", 0),
            )
        if cached_input_tokens <= 0:
            cached_input_tokens = self._coerce_int(
                getattr(usage_obj, "cached_tokens", 0),
            )

        telemetry = {
            "kv_cache_session_id": self._kv_cache_session_id or "",
            "kv_cache_prompt_tokens": prompt_tokens,
            "kv_cache_completion_tokens": completion_tokens,
            "kv_cache_cached_input_tokens": max(0, cached_input_tokens),
            "kv_cache_enabled": self._kv_cache_enabled,
            "kv_cache_breakpoint_mode": self._kv_cache_breakpoint_mode,
            "kv_cache_contract_version": self._kv_cache_contract_version,
        }
        if prompt_tokens > 0:
            telemetry["kv_cache_hit_rate"] = round(
                max(0, cached_input_tokens) / max(1, prompt_tokens),
                6,
            )
        return telemetry

    def _merge_metadata_with_kv_cache(
        self,
        metadata: dict[str, Any] | None,
        usage_obj: Any,
    ) -> dict[str, Any] | None:
        if not self._kv_cache_metrics_enabled:
            return metadata
        telemetry = self._extract_kv_cache_telemetry(usage_obj)
        if not telemetry:
            return metadata
        merged = dict(metadata or {})
        merged.update(telemetry)
        logger.info(
            "[OpenRouterChatModel] KV-cache telemetry model=%s prompt_tokens=%s cached_input_tokens=%s hit_rate=%s session=%s",
            self.model_name,
            merged.get("kv_cache_prompt_tokens", 0),
            merged.get("kv_cache_cached_input_tokens", 0),
            merged.get("kv_cache_hit_rate", 0),
            merged.get("kv_cache_session_id", "") or "none",
        )
        return merged

    @staticmethod
    def _merge_reasoning_details_metadata(
        metadata: dict[str, Any] | None,
        reasoning_details: Any,
    ) -> dict[str, Any] | None:
        if reasoning_details is None:
            return metadata
        merged = dict(metadata or {})
        merged[OPENROUTER_REASONING_DETAILS_KEY] = deepcopy(reasoning_details)
        return merged

    @staticmethod
    def _structured_model_field_names(
        structured_model: Type[BaseModel] | None,
    ) -> list[str]:
        if structured_model is None:
            return []
        model_fields = getattr(structured_model, "model_fields", None)
        if isinstance(model_fields, dict):
            return [str(name) for name in model_fields.keys()]
        legacy_fields = getattr(structured_model, "__fields__", None)
        if isinstance(legacy_fields, dict):
            return [str(name) for name in legacy_fields.keys()]
        return []

    @staticmethod
    def _coerce_structured_field_value(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            return value
        if isinstance(value, (dict, list, tuple, set)):
            import json

            try:
                return json.dumps(value, ensure_ascii=False, sort_keys=True)
            except Exception:
                return str(value)
        return str(value)

    def _normalize_structured_metadata(
        self,
        structured_model: Type[BaseModel] | None,
        *,
        parsed_obj: Any = None,
        raw_text: str = "",
        log_missing: bool = True,
    ) -> dict[str, Any] | None:
        if structured_model is None:
            if isinstance(parsed_obj, dict):
                return parsed_obj
            if raw_text:
                repaired = _json_loads_with_repair(raw_text)
                if isinstance(repaired, dict):
                    return repaired
            return None

        metadata: dict[str, Any] = {}
        source = "none"

        if parsed_obj is not None:
            source = "parsed"
            try:
                if isinstance(parsed_obj, BaseModel):
                    metadata = parsed_obj.model_dump()
                elif hasattr(parsed_obj, "model_dump") and callable(
                    parsed_obj.model_dump,
                ):
                    metadata = parsed_obj.model_dump()
                elif hasattr(parsed_obj, "dict") and callable(parsed_obj.dict):
                    metadata = parsed_obj.dict()
                elif isinstance(parsed_obj, dict):
                    metadata = dict(parsed_obj)
            except Exception as exc:
                logger.warning(
                    "[OpenRouterChatModel] Failed to serialize parsed structured output (%s): %s",
                    structured_model.__name__,
                    exc,
                )
                metadata = {}

        if not isinstance(metadata, dict):
            metadata = {}

        if not metadata and raw_text:
            source = "raw_text"
            repaired = _json_loads_with_repair(raw_text)
            metadata = repaired if isinstance(repaired, dict) else {}

        field_names = self._structured_model_field_names(structured_model)
        if not field_names:
            return metadata or None

        normalized = dict(metadata)
        missing_fields: list[str] = []
        for field_name in field_names:
            if field_name not in normalized:
                normalized[field_name] = ""
                missing_fields.append(field_name)
            normalized[field_name] = self._coerce_structured_field_value(
                normalized.get(field_name),
            )

        if missing_fields and log_missing:
            logger.warning(
                "[OpenRouterChatModel] Structured output missing %d/%d fields for %s; backfilled keys=%s source=%s",
                len(missing_fields),
                len(field_names),
                structured_model.__name__,
                missing_fields,
                source,
            )

        return normalized

    @staticmethod
    def _is_gemini_parts_error(error: Exception) -> bool:
        return "must include at least one parts field" in str(error).lower()

    def _is_gemini_model(self) -> bool:
        return "gemini" in (self.model_name or "").lower()

    @staticmethod
    def _is_retriable_api_error(error: Exception) -> bool:
        status_code = getattr(error, "status_code", None)
        if status_code is None:
            response = getattr(error, "response", None)
            status_code = getattr(response, "status_code", None)

        if status_code is None:
            return True

        return status_code >= 500 or status_code in {408, 409, 429}

    def _get_base_url(self) -> str:
        if self._configured_base_url:
            return self._configured_base_url

        client = getattr(self, "client", None)
        base_url = getattr(client, "base_url", None)
        if base_url is not None:
            return str(base_url).lower()

        return ""

    def _fallback_provider_model_name(self) -> str:
        base_url = self._get_base_url()
        if "moonshot.cn" in base_url:
            return MOONSHOT_FALLBACK_MODEL_NAME
        if "api.ppio.com" in base_url:
            return self.model_name
        if "dashscope.aliyuncs.com" in base_url:
            return self.model_name
        if "volces.com" in base_url or "volcengine" in base_url:
            return self.model_name
        return OPENROUTER_FALLBACK_MODEL_NAME

    def _summarize_messages(
        self,
        messages: list[dict[str, Any]],
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        summary = []
        for msg in messages[:limit]:
            content = msg.get("content")
            tool_calls = msg.get("tool_calls") or []
            info = {
                "role": msg.get("role"),
                "tool_calls": len(tool_calls),
            }
            if content is None:
                info["content"] = "none"
            elif isinstance(content, str):
                info["content"] = f"str:{len(content)}"
            elif isinstance(content, list):
                non_empty_text = 0
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        if str(block.get("text") or "").strip():
                            non_empty_text += 1
                info["content"] = f"list:{len(content)} text:{non_empty_text}"
            else:
                info["content"] = f"{type(content).__name__}"
            summary.append(info)
        if len(messages) > limit:
            summary.append({"truncated": len(messages) - limit})
        return summary

    def _sanitize_gemini_messages(
        self,
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        dropped = 0
        fixed = 0
        sanitized: list[dict[str, Any]] = []

        for msg in messages:
            content = msg.get("content")
            tool_calls = msg.get("tool_calls")

            if tool_calls and (content is None or content == []):
                dropped += 1
                continue

            if content is None or content == []:
                msg = dict(msg)
                msg["content"] = [{"type": "text", "text": "(empty message)"}]
                fixed += 1
            elif isinstance(content, str):
                if not content.strip():
                    msg = dict(msg)
                    msg["content"] = "(empty message)"
                    fixed += 1
            elif isinstance(content, list):
                cleaned = []
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "text":
                        text = block.get("text") or ""
                        if text.strip():
                            cleaned.append(block)
                    else:
                        cleaned.append(block)
                if not cleaned:
                    msg = dict(msg)
                    msg["content"] = [{"type": "text", "text": "(empty message)"}]
                    fixed += 1
                elif cleaned != content:
                    msg = dict(msg)
                    msg["content"] = cleaned
                    fixed += 1

            if msg.get("role") == "tool" and isinstance(msg.get("content"), str):
                if not msg["content"].strip():
                    msg["content"] = "(tool returned no output)"
                    fixed += 1

            sanitized.append(msg)

        if dropped:
            logger.warning(
                "[OpenRouterChatModel] Dropped %s tool_call-only messages for Gemini",
                dropped,
            )
        if fixed:
            logger.warning(
                "[OpenRouterChatModel] Normalized %s empty-content messages for Gemini",
                fixed,
            )

        if not sanitized:
            sanitized = [
                {
                    "role": "user",
                    "content": [{"type": "text", "text": "(empty message)"}],
                }
            ]
            logger.warning(
                "[OpenRouterChatModel] Inserted fallback message for empty Gemini prompt",
            )

        return sanitized

    def _sanitize_tool_message_sequence(
        self,
        messages: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Sanitize tool-call/message pairing for OpenAI-compatible providers."""
        sanitized, stats = sanitize_tool_message_sequence(messages)

        if stats.has_changes():
            logger.warning(
                "[OpenRouterChatModel] Dropped %s malformed tool messages and %s orphan tool messages, "
                "stripped %s unmatched assistant tool calls, dropped %s tool_call-only assistant messages",
                stats.dropped_empty_tool_messages,
                stats.dropped_orphan_tool_messages,
                stats.stripped_unmatched_assistant_tool_calls,
                stats.dropped_tool_call_only_assistant_messages,
            )

        return sanitized

    def _validate_reasoning_content_continuity(
        self,
        messages: list[dict[str, Any]],
    ) -> None:
        """Validate reasoning_content continuity for DeepSeek thinking mode.

        DeepSeek requires that assistant messages with tool_calls in a
        thinking-mode conversation must carry reasoning_content in ALL
        subsequent requests.  Missing it produces a 400 error.
        """
        generate_kwargs = getattr(self, "generate_kwargs", None) or {}
        if isinstance(generate_kwargs, dict):
            extra_body = generate_kwargs.get("extra_body", {})
            if isinstance(extra_body, dict):
                thinking_config = extra_body.get("thinking", {})
                if isinstance(thinking_config, dict):
                    thinking_enabled = (
                        str(thinking_config.get("type", "")).lower() == "enabled"
                    )
                else:
                    thinking_enabled = False
            else:
                thinking_enabled = False
        else:
            thinking_enabled = False

        if not thinking_enabled:
            return

        for idx, msg in enumerate(messages):
            if not isinstance(msg, dict):
                continue
            if msg.get("role") != "assistant":
                continue
            if not msg.get("tool_calls"):
                continue
            if not msg.get("reasoning_content"):
                model_name = getattr(self, "model_name", "") or ""
                provider_hint = (
                    "thinking-mode"
                    if "deepseek" in str(model_name).lower()
                    else "thinking-mode"
                )
                logger.warning(
                    "[OpenRouterChatModel] Thinking mode (%s): assistant message at "
                    "index %d has tool_calls but no reasoning_content. "
                    "This will cause a 400 error from the API provider. "
                    "Message keys: %s",
                    model_name,
                    idx,
                    sorted(msg.keys()),
                )

    def _end_lf_generation(
        self, generation: Any, res: Any = None, error: str | None = None
    ) -> None:
        """End a Langfuse generation with usage data from ChatResponse."""
        if generation is None:
            return
        try:
            end_kwargs: dict[str, Any] = {}
            input_t = 0
            output_t = 0
            if error:
                end_kwargs["status_message"] = error
                end_kwargs["level"] = "ERROR"
            if res is not None:
                usage = getattr(res, "usage", None)
                if usage is not None:
                    # AgentScope ChatUsage uses input_tokens/output_tokens
                    # OpenAI uses prompt_tokens/completion_tokens — try both
                    input_t = self._coerce_int(
                        getattr(usage, "input_tokens", 0)
                    ) or self._coerce_int(getattr(usage, "prompt_tokens", 0))
                    output_t = self._coerce_int(
                        getattr(usage, "output_tokens", 0)
                    ) or self._coerce_int(getattr(usage, "completion_tokens", 0))
                    if input_t or output_t:
                        end_kwargs["usage_details"] = {
                            "input": input_t,
                            "output": output_t,
                        }
                        active_run_id = get_agent_run_context()[0]
                        if active_run_id:
                            record_model_generation(
                                active_run_id,
                                model=self.model_name,
                                input_tokens=input_t,
                                output_tokens=output_t,
                            )
                    response_time = getattr(usage, "time", None)
                    if response_time is not None:
                        run_id_for_ttft = get_agent_run_context()[0]
                        if run_id_for_ttft:
                            record_ttft_seconds(run_id_for_ttft, response_time)
                content = getattr(res, "content", None)
                if content is not None:
                    end_kwargs["output"] = str(content)[:500]
                end_kwargs["model"] = self.model_name
            if end_kwargs:
                generation.update(**end_kwargs)
            generation.end()
        except Exception:
            pass

    def _wrap_streaming_response(
        self,
        res: Any,
        safe_messages: list[dict[str, Any]],
        tools: list[dict] | None,
        tool_choice: str | None,
        structured_model: Type[BaseModel] | None,
        kwargs: dict[str, Any],
        generation: Any = None,
    ) -> ChatResponse | AsyncGenerator[ChatResponse, None]:
        if not self.stream or not hasattr(res, "__aiter__"):
            self._end_lf_generation(generation, res)
            return res

        base_call = super(OpenRouterChatModel, self).__call__

        async def _call_non_stream_with_fallback():
            original_stream = self.stream
            original_model = self.model_name
            try:
                self.stream = False
                try:
                    return await base_call(
                        safe_messages,
                        tools=tools,
                        tool_choice=tool_choice,
                        structured_model=structured_model,
                        **kwargs,
                    )
                except (InternalServerError, APIError) as error:
                    if isinstance(error, APIError) and not self._is_retriable_api_error(
                        error
                    ):
                        raise

                    fallback_model = self._fallback_provider_model_name()
                    logger.warning(
                        "[OpenRouterChatModel] Non-stream retry failed; fallback to %s: %s",
                        fallback_model,
                        error,
                    )
                    if original_model != fallback_model:
                        self.model_name = fallback_model
                        return await base_call(
                            safe_messages,
                            tools=tools,
                            tool_choice=tool_choice,
                            structured_model=structured_model,
                            **kwargs,
                        )
                    raise
            finally:
                self.stream = original_stream
                self.model_name = original_model

        async def _stream_with_retry():
            last_chunk = None
            try:
                async for chunk in res:
                    last_chunk = chunk
                    yield chunk
            except (InternalServerError, APIError) as error:
                if isinstance(error, APIError) and not self._is_retriable_api_error(
                    error
                ):
                    raise

                logger.warning(
                    "[OpenRouterChatModel] Streaming error; retrying non-stream: %s",
                    error,
                )
                logger.warning(
                    "[OpenRouterChatModel] Message summary: %s",
                    self._summarize_messages(safe_messages),
                )
                fallback = await _call_non_stream_with_fallback()
                last_chunk = fallback
                yield fallback
            finally:
                self._end_lf_generation(generation, last_chunk)

        return _stream_with_retry()

    async def __call__(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        tool_choice: str | None = None,
        structured_model: Type[BaseModel] | None = None,
        **kwargs: Any,
    ) -> ChatResponse | AsyncGenerator[ChatResponse, None]:
        # Langfuse generation tracking
        generation = None
        if self._lf_trace:
            try:
                gen_kwargs = {
                    "name": "llm_call",
                    "model": self.model_name,
                    "model_parameters": {"stream": self.stream},
                }
                if self._lf_prompt_obj is not None:
                    gen_kwargs["prompt"] = self._lf_prompt_obj
                gen_kwargs["as_type"] = "generation"
                generation = self._lf_trace.start_observation(**gen_kwargs)
            except Exception:
                pass

        safe_messages = messages
        if self._is_gemini_model():
            safe_messages = self._sanitize_gemini_messages(messages)

        safe_messages = self._sanitize_tool_message_sequence(safe_messages)
        self._validate_reasoning_content_continuity(safe_messages)
        runtime_kwargs, cache_payload_applied = self._build_kv_cache_runtime_kwargs(
            kwargs,
        )

        # CacheBoundaryHook: detect system/tool drift before API call
        if self._cache_boundary is not None:
            system_hash = self._cache_boundary.hash_system_messages(
                safe_messages,
            )
            tool_hash = self._cache_boundary.hash_tools(tools)
            ok, warnings = self._cache_boundary.check(system_hash, tool_hash)
            if not self._cache_boundary._frozen:
                self._cache_boundary.freeze(system_hash, tool_hash)
            for w in warnings:
                logger.warning("[OpenRouterChatModel] %s", w)

        try:
            res = await super().__call__(
                safe_messages,
                tools=tools,
                tool_choice=tool_choice,
                structured_model=structured_model,
                **runtime_kwargs,
            )
            return self._wrap_streaming_response(
                res,
                safe_messages,
                tools,
                tool_choice,
                structured_model,
                runtime_kwargs,
                generation=generation,
            )
        except InternalServerError as error:
            logger.warning(
                "[OpenRouterChatModel] InternalServerError; retrying once: %s",
                error,
            )
            await asyncio.sleep(0.5)
            original_model = self.model_name
            try:
                res = await super().__call__(
                    safe_messages,
                    tools=tools,
                    tool_choice=tool_choice,
                    structured_model=structured_model,
                    **runtime_kwargs,
                )
            except (InternalServerError, APIError) as retry_error:
                if isinstance(
                    retry_error, APIError
                ) and not self._is_retriable_api_error(retry_error):
                    raise

                fallback_model = self._fallback_provider_model_name()
                logger.warning(
                    "[OpenRouterChatModel] Retry failed; fallback to %s: %s",
                    fallback_model,
                    retry_error,
                )
                if original_model != fallback_model:
                    self.model_name = fallback_model
                    res = await super().__call__(
                        safe_messages,
                        tools=tools,
                        tool_choice=tool_choice,
                        structured_model=structured_model,
                        **runtime_kwargs,
                    )
                else:
                    raise
            finally:
                self.model_name = original_model

            return self._wrap_streaming_response(
                res,
                safe_messages,
                tools,
                tool_choice,
                structured_model,
                runtime_kwargs,
                generation=generation,
            )
        except APIError as error:
            if not self._is_retriable_api_error(error):
                raise

            logger.warning(
                "[OpenRouterChatModel] APIError; retrying once: %s",
                error,
            )
            await asyncio.sleep(0.5)
            original_model = self.model_name
            try:
                res = await super().__call__(
                    safe_messages,
                    tools=tools,
                    tool_choice=tool_choice,
                    structured_model=structured_model,
                    **runtime_kwargs,
                )
            except (InternalServerError, APIError) as retry_error:
                if isinstance(
                    retry_error, APIError
                ) and not self._is_retriable_api_error(retry_error):
                    raise

                fallback_model = self._fallback_provider_model_name()
                logger.warning(
                    "[OpenRouterChatModel] Retry failed; fallback to %s: %s",
                    fallback_model,
                    retry_error,
                )
                if original_model != fallback_model:
                    self.model_name = fallback_model
                    res = await super().__call__(
                        safe_messages,
                        tools=tools,
                        tool_choice=tool_choice,
                        structured_model=structured_model,
                        **runtime_kwargs,
                    )
                else:
                    raise
            finally:
                self.model_name = original_model

            return self._wrap_streaming_response(
                res,
                safe_messages,
                tools,
                tool_choice,
                structured_model,
                runtime_kwargs,
                generation=generation,
            )
        except BadRequestError as error:
            if cache_payload_applied and self._is_kv_cache_payload_error(error):
                logger.warning(
                    "[OpenRouterChatModel] KV cache control payload rejected by provider; retrying without cache payload: %s",
                    error,
                )
                runtime_kwargs = self._strip_kv_cache_runtime_kwargs(runtime_kwargs)
                res = await super().__call__(
                    safe_messages,
                    tools=tools,
                    tool_choice=tool_choice,
                    structured_model=structured_model,
                    **runtime_kwargs,
                )
                return self._wrap_streaming_response(
                    res,
                    safe_messages,
                    tools,
                    tool_choice,
                    structured_model,
                    runtime_kwargs,
                    generation=generation,
                )

            if not (self._is_gemini_model() and self._is_gemini_parts_error(error)):
                raise

            logger.warning(
                "[OpenRouterChatModel] Gemini parts error; message summary: %s",
                self._summarize_messages(safe_messages),
            )

            original_model = self.model_name
            fallback_model = GEMINI_FLASH_MODEL_NAME
            if original_model != fallback_model:
                logger.warning(
                    "[OpenRouterChatModel] Gemini parts error; retrying with fallback model %s",
                    fallback_model,
                )
            else:
                logger.warning(
                    "[OpenRouterChatModel] Gemini parts error; retrying with %s",
                    fallback_model,
                )

            try:
                if original_model != fallback_model:
                    self.model_name = fallback_model
                res = await super().__call__(
                    safe_messages,
                    tools=tools,
                    tool_choice=tool_choice,
                    structured_model=structured_model,
                    **runtime_kwargs,
                )
                return self._wrap_streaming_response(
                    res,
                    safe_messages,
                    tools,
                    tool_choice,
                    structured_model,
                    runtime_kwargs,
                    generation=generation,
                )
            finally:
                self.model_name = original_model

    def _parse_openai_completion_response(
        self,
        start_datetime: datetime,
        response,  # ChatCompletion
        structured_model: Type[BaseModel] | None = None,
    ) -> ChatResponse:
        """
        Parse OpenAI/OpenRouter chat completion response.

        Overrides the parent method to handle OpenRouter's `reasoning` field
        in addition to DeepSeek's `reasoning_content` field.
        """
        content_blocks: List[TextBlock | ToolUseBlock | ThinkingBlock | AudioBlock] = []
        metadata: dict | None = None

        if response.choices:
            choice = response.choices[0]
            metadata = self._merge_reasoning_details_metadata(
                metadata,
                _extract_openrouter_reasoning_details(choice.message)
                or _extract_openrouter_reasoning_details(choice),
            )

            # Handle OpenRouter's `reasoning` field (different from DeepSeek's `reasoning_content`)
            reasoning = getattr(choice.message, "reasoning", None)
            if reasoning:
                content_blocks.append(
                    ThinkingBlock(
                        type="thinking",
                        thinking=reasoning,
                    ),
                )

            # Also check for DeepSeek's `reasoning_content` field (for compatibility)
            reasoning_content = getattr(choice.message, "reasoning_content", None)
            if reasoning_content and not reasoning:  # Don't duplicate if both exist
                content_blocks.append(
                    ThinkingBlock(
                        type="thinking",
                        thinking=reasoning_content,
                    ),
                )

            if choice.message.content:
                content_blocks.append(
                    TextBlock(
                        type="text",
                        text=choice.message.content,
                    ),
                )

            if structured_model:
                metadata = self._normalize_structured_metadata(
                    structured_model,
                    parsed_obj=getattr(choice.message, "parsed", None),
                    raw_text=getattr(choice.message, "content", "") or "",
                    log_missing=True,
                )

            if choice.message.audio:
                media_type = self.generate_kwargs.get("audio", {}).get(
                    "format",
                    "mp3",
                )
                content_blocks.append(
                    AudioBlock(
                        type="audio",
                        source=Base64Source(
                            data=choice.message.audio.data,
                            media_type=f"audio/{media_type}",
                            type="base64",
                        ),
                    ),
                )

            for tool_call in choice.message.tool_calls or []:
                tool_name = tool_call.function.name
                raw_args = tool_call.function.arguments or "{}"
                parsed_input = _json_loads_with_repair(raw_args)

                # ── Emit inline feedback for empty/missing-param tool calls so
                # the model sees the guidance, but DO NOT skip the ToolUseBlock.
                # The pre-execution gate in toolkit_adapter will catch empty
                # calls locally (no sandbox round-trip) and return an error
                # ToolResponse, keeping the ReAct loop running naturally.
                required_params = _get_required_params(tool_name)
                _empty_already_reported = False
                if required_params:
                    missing = [
                        p
                        for p in required_params
                        if not (isinstance(parsed_input, dict) and parsed_input.get(p))
                    ]
                    if missing:
                        _report_empty_tool_input(tool_name, parsed_input)
                        _empty_already_reported = True
                        missing_names = ", ".join(repr(p) for p in missing)
                        content_blocks.append(
                            TextBlock(
                                type="text",
                                text=(
                                    f"[System note: You called '{tool_name}' without "
                                    f"required parameters ({missing_names}). "
                                    f"Tool call was not executed. "
                                    f"Please provide all required arguments.]"
                                ),
                            )
                        )
                        global _empty_tooluse_blocks_emitted
                        _empty_tooluse_blocks_emitted += 1
                        # FALL THROUGH — still emit ToolUseBlock so the ReAct
                        # loop sees a tool call to execute and continues.

                if not _empty_already_reported:
                    _report_empty_tool_input(tool_name, parsed_input)
                content_blocks.append(
                    ToolUseBlock(
                        type="tool_use",
                        id=tool_call.id,
                        name=tool_name,
                        input=parsed_input,
                    ),
                )

        usage = None
        raw_usage = None
        if response.usage:
            raw_usage = response.usage
            usage = ChatUsage(
                input_tokens=response.usage.prompt_tokens,
                output_tokens=response.usage.completion_tokens,
                time=(datetime.now() - start_datetime).total_seconds(),
            )
        metadata = self._merge_metadata_with_kv_cache(metadata, raw_usage)

        return ChatResponse(
            content=content_blocks,
            usage=usage,
            metadata=metadata,
        )

    async def _parse_openai_stream_response(
        self,
        start_datetime: datetime,
        response,  # AsyncStream
        structured_model: Type[BaseModel] | None = None,
    ) -> AsyncGenerator[ChatResponse, None]:
        """
        Parse OpenAI/OpenRouter streaming response.

        Handles both OpenRouter's `reasoning` and DeepSeek's `reasoning_content`
        in streaming deltas.
        """
        usage, res = None, None
        raw_usage = None
        text = ""
        thinking = ""
        audio = ""
        tool_calls = OrderedDict()
        metadata: dict | None = None
        reasoning_details = None
        _thinking_warning_logged = False
        contents: List[TextBlock | ToolUseBlock | ThinkingBlock | AudioBlock] = []

        async with response as stream:
            async for item in stream:
                if structured_model:
                    if item.type != "chunk":
                        continue
                    chunk = item.chunk
                else:
                    chunk = item

                if chunk.usage:
                    raw_usage = chunk.usage
                    usage = ChatUsage(
                        input_tokens=chunk.usage.prompt_tokens,
                        output_tokens=chunk.usage.completion_tokens,
                        time=(datetime.now() - start_datetime).total_seconds(),
                    )

                if not chunk.choices:
                    if usage and contents:
                        metadata = self._merge_metadata_with_kv_cache(
                            metadata,
                            raw_usage,
                        )
                        res = ChatResponse(
                            content=contents,
                            usage=usage,
                            metadata=metadata,
                        )
                        yield res
                    continue

                choice = chunk.choices[0]
                reasoning_details = (
                    _extract_openrouter_reasoning_details(choice.delta)
                    or _extract_openrouter_reasoning_details(choice)
                    or _extract_openrouter_reasoning_details(chunk)
                    or reasoning_details
                )

                # Handle both OpenRouter's `reasoning` and DeepSeek's `reasoning_content`
                # OpenRouter streaming uses `reasoning` in delta
                # DeepSeek streaming uses `reasoning_content` in delta
                reasoning_chunk = (
                    getattr(choice.delta, "reasoning", None)
                    or getattr(choice.delta, "reasoning_content", None)
                    or ""
                )
                thinking += reasoning_chunk

                # ── Thinking-content early-warning detection ────────────
                # Detect when the model's internal monologue signals a
                # tool-calling failure pattern.  Logged as WARNING for
                # post-hoc analysis; not injected mid-generation to avoid
                # disrupting the model's autoregressive flow.
                if reasoning_chunk and not _thinking_warning_logged:
                    lower_r = reasoning_chunk.lower()
                    if any(
                        phrase in lower_r
                        for phrase in (
                            "malformed tool call",
                            "stuck in a loop",
                            "keep making",
                        )
                    ):
                        _thinking_warning_logged = True
                        logger.warning(
                            "[OpenRouterChatModel] Thinking-content pattern "
                            "suggests tool-calling failure: %r",
                            reasoning_chunk[:200],
                        )

                text += getattr(choice.delta, "content", None) or ""

                if (
                    hasattr(choice.delta, "audio")
                    and choice.delta.audio
                    and "data" in choice.delta.audio
                ):
                    audio += choice.delta.audio["data"]
                if (
                    hasattr(choice.delta, "audio")
                    and choice.delta.audio
                    and "transcript" in choice.delta.audio
                ):
                    text += choice.delta.audio["transcript"]

                for tool_call in getattr(choice.delta, "tool_calls", None) or []:
                    call_index = getattr(tool_call, "index", None)
                    if call_index is None:
                        continue

                    function = getattr(tool_call, "function", None)
                    incoming_arguments = getattr(function, "arguments", None)
                    incoming_name = getattr(function, "name", None)
                    if incoming_arguments is not None and not isinstance(
                        incoming_arguments,
                        str,
                    ):
                        try:
                            incoming_arguments = json.dumps(
                                incoming_arguments,
                                ensure_ascii=False,
                            )
                        except Exception:
                            incoming_arguments = str(incoming_arguments)

                    if call_index not in tool_calls:
                        tool_calls[call_index] = {
                            "type": "tool_use",
                            "id": getattr(tool_call, "id", None),
                            "name": incoming_name,
                            "input": incoming_arguments or "",
                        }
                        continue

                    existing = tool_calls[call_index]
                    if not existing.get("id") and getattr(tool_call, "id", None):
                        existing["id"] = tool_call.id
                    if not existing.get("name") and incoming_name:
                        existing["name"] = incoming_name
                    if incoming_arguments is not None:
                        tool_name = existing.get("name") or incoming_name
                        if _is_write_file_tool_name(tool_name):
                            existing["input"] = merge_write_file_arguments(
                                existing.get("input", ""),
                                incoming_arguments,
                            )
                        else:
                            existing["input"] = _merge_tool_arguments(
                                existing.get("input", ""),
                                incoming_arguments,
                            )

                contents = []

                if thinking:
                    contents.append(
                        ThinkingBlock(
                            type="thinking",
                            thinking=thinking,
                        ),
                    )

                if audio:
                    media_type = self.generate_kwargs.get("audio", {}).get(
                        "format",
                        "wav",
                    )
                    contents.append(
                        AudioBlock(
                            type="audio",
                            source=Base64Source(
                                data=audio,
                                media_type=f"audio/{media_type}",
                                type="base64",
                            ),
                        ),
                    )

                if text:
                    contents.append(
                        TextBlock(
                            type="text",
                            text=text,
                        ),
                    )

                    if structured_model:
                        metadata = self._normalize_structured_metadata(
                            structured_model,
                            parsed_obj=getattr(choice.delta, "parsed", None),
                            raw_text=text,
                            log_missing=False,
                        )

                for tool_call in tool_calls.values():
                    # Claude Code pattern: accumulate tool args as strings
                    # during streaming, parse JSON only when the block is complete.
                    # Skip intermediate chunks with empty/partial arguments —
                    # the ToolUseBlock will be yielded when arguments arrive
                    # in a later delta, or in the post-stream final yield.
                    raw_input = tool_call.get("input") or ""
                    if not raw_input.strip():
                        continue
                    parsed_input = _json_loads_with_repair(raw_input)
                    tool_name = tool_call.get("name")

                    raw_arguments_text = str(raw_input or "")
                    arguments_source = "parsed_input"
                    raw_arguments_hash = ""

                    if _is_write_file_tool_name(tool_name):
                        strict_parsed = None
                        try:
                            strict_loaded = json.loads(raw_arguments_text)
                            if isinstance(strict_loaded, dict):
                                strict_parsed = strict_loaded
                        except Exception:
                            strict_parsed = None

                        raw_arguments_hash = hashlib.sha1(
                            raw_arguments_text.encode("utf-8"),
                        ).hexdigest()[:12]
                        repaired_input = sanitize_write_file_input(
                            strict_parsed or {},
                            raw_arguments=raw_input,
                        )
                        if repaired_input and strict_parsed is not None:
                            parsed_input = repaired_input
                            arguments_source = "final_payload"
                            logger.debug(
                                "Recovered write_file tool arguments from stream chunk",
                                tool_name=tool_name,
                                recovered_keys=sorted(repaired_input.keys()),
                                raw_length=len(raw_input),
                            )
                        elif repaired_input:
                            parsed_input = repaired_input
                            arguments_source = "raw_preview"
                            logger.debug(
                                "Recovered preview write_file tool arguments from raw stream chunk",
                                tool_name=tool_name,
                                recovered_keys=sorted(repaired_input.keys()),
                                raw_length=len(raw_input),
                            )
                        elif raw_input:
                            parsed_input = _extract_partial_write_file_input(raw_input)
                            if parsed_input:
                                arguments_source = "raw_preview"
                                logger.debug(
                                    "Recovered partial write_file tool arguments from stream chunk",
                                    tool_name=tool_name,
                                    recovered_keys=sorted(parsed_input.keys()),
                                    raw_length=len(raw_input),
                                )
                            else:
                                arguments_source = "raw_passthrough"
                                logger.debug(
                                    "Unable to parse write_file tool arguments from stream chunk",
                                    tool_name=tool_name,
                                    raw_length=len(raw_input),
                                    raw_preview=raw_input[:180],
                                )

                    # Skip mid-stream yield if required params are missing.
                    # For write_file this gate must run *after* the streaming
                    # repair above: incomplete large payloads are normal, but a
                    # recovered snapshot with only {"content": ...} must not
                    # become an executable ToolUseBlock that the ReAct loop can
                    # reject repeatedly until the harness times out.
                    required_mid = _get_required_params(tool_name)
                    if required_mid:
                        missing_mid = [
                            p
                            for p in required_mid
                            if not (
                                isinstance(parsed_input, dict)
                                and parsed_input.get(p)
                            )
                        ]
                        if missing_mid:
                            continue

                    _report_empty_tool_input(tool_name, parsed_input)
                    tool_block = ToolUseBlock(
                        type=tool_call["type"],
                        id=tool_call.get("id"),
                        name=tool_name,
                        input=parsed_input,
                    )
                    if _is_write_file_tool_name(tool_name):
                        tool_block["raw_arguments"] = raw_arguments_text
                        tool_block["arguments_source"] = arguments_source
                        tool_block["raw_arguments_hash"] = raw_arguments_hash

                    contents.append(
                        tool_block,
                    )
                    tool_call["_yielded"] = True

                if not contents:
                    continue

                metadata = self._merge_reasoning_details_metadata(
                    metadata,
                    reasoning_details,
                )
                metadata = self._merge_metadata_with_kv_cache(metadata, raw_usage)
                res = ChatResponse(
                    content=contents,
                    usage=usage,
                    metadata=metadata,
                )
                yield res

            # ── Post-stream final tool-call yield ──────────────────────
            # Claude Code pattern: during streaming, tool arguments are accumulated
            # as strings; ToolUseBlocks are only parsed and yielded once the stream
            # ends (equivalent to Anthropic's ``content_block_stop``).  Here we do
            # the same for the OpenAI protocol: any tool calls whose arguments never
            # arrived during streaming (e.g. empty deltas from the model) are yielded
            # at the end so the toolkit-adapter guards can reject them with a clear
            # error instead of silently dropping them.
            #
            # For write_file tool calls, apply the same argument-recovery logic as
            # the mid-stream path (sanitize + partial extraction) so that truncated
            # long-content JSON is not silently converted to empty {}.
            remaining_tool_blocks = []
            empty_consecutive_count: dict[str, int] = {}
            for tc in tool_calls.values():
                # Skip tool calls already yielded in mid-stream path
                if tc.get("_yielded"):
                    continue
                raw_input = tc.get("input") or ""
                tname = tc.get("name")
                raw_arguments_text = str(raw_input or "")

                # ── Emit inline feedback for empty/missing-param tool calls.
                # For write_file, keep malformed calls feedback-only: DeepSeek
                # can stream large file snapshots that contain content but not
                # path, and executing those caused timeout-scale retry floods.
                #
                # For other tools, keep the ToolUseBlock after the warning so
                # the toolkit adapter's local pre-execution gate can return a
                # MISSING_REQUIRED_PARAMS ToolResponse.  If we emit only text,
                # ReAct treats the message as final assistant content and the
                # model never receives tool-result feedback to repair from.
                required_params = _get_required_params(tname)
                _empty_already_reported = False
                if required_params:
                    parsed_for_gate = _json_loads_with_repair(raw_input)
                    missing = [p for p in required_params if not parsed_for_gate.get(p)]
                    if missing:
                        _report_empty_tool_input(tname, parsed_for_gate)
                        _empty_already_reported = True
                        missing_names = ", ".join(repr(p) for p in missing)
                        empty_consecutive_count[tname] = (
                            empty_consecutive_count.get(tname, 0) + 1
                        )
                        remaining_tool_blocks.append(
                            TextBlock(
                                type="text",
                                text=(
                                    f"[System note: You called '{tname}' without "
                                    f"required parameters ({missing_names}). "
                                    f"Tool call was not executed. "
                                    f"Please provide all required arguments.]"
                                ),
                            ),
                        )
                        global _empty_tooluse_blocks_emitted
                        _empty_tooluse_blocks_emitted += 1
                        if _is_write_file_tool_name(tname):
                            continue
                    else:
                        # Valid non-empty call resets the counter for this tool
                        empty_consecutive_count.pop(tname, None)

                parsed = _json_loads_with_repair(raw_input)

                # ── write_file argument recovery (mirrors mid-stream lines) ──
                if _is_write_file_tool_name(tname):
                    if not parsed and raw_input.strip():
                        repaired_input = sanitize_write_file_input(
                            {},
                            raw_arguments=raw_input,
                        )
                        if repaired_input:
                            parsed = repaired_input
                        else:
                            parsed = (
                                _extract_partial_write_file_input(raw_input) or parsed
                            )

                if not _empty_already_reported:
                    _report_empty_tool_input(tname, parsed)
                tb = ToolUseBlock(
                    type=tc.get("type", "tool_use"),
                    id=tc.get("id"),
                    name=tname,
                    input=parsed,
                )
                if _is_write_file_tool_name(tname):
                    tb["raw_arguments"] = raw_arguments_text
                remaining_tool_blocks.append(tb)

            # ── Circuit breaker: escalate when 3+ consecutive empty calls ─
            CIRCUIT_BREAKER_THRESHOLD = 3
            for tool_name, count in empty_consecutive_count.items():
                if count >= CIRCUIT_BREAKER_THRESHOLD:
                    global _circuit_breaker_activations
                    _circuit_breaker_activations += 1
                    logger.warning(
                        "[OpenRouterChatModel] Circuit breaker: "
                        "tool=%s consecutive_empty_calls=%d",
                        tool_name,
                        count,
                    )
                    escalation = TextBlock(
                        type="text",
                        text=(
                            f"[System: You have made {count} consecutive "
                            f"empty calls to '{tool_name}'. "
                            f"This indicates a tool-calling failure pattern. "
                            f"Verify your next call includes ALL required "
                            f"parameters before emitting. "
                            f"Consider using a different approach entirely.]"
                        ),
                    )
                    remaining_tool_blocks.append(escalation)

            if remaining_tool_blocks:
                # Build final contents including accumulated thinking and text.
                # Without this, the post-stream yield strips the ThinkingBlock
                # from the last chunk, and the agent's msg.content overwrite
                # loses reasoning_content — causing DeepSeek 400 errors.
                final_contents: list = []
                if thinking:
                    final_contents.append(
                        ThinkingBlock(type="thinking", thinking=thinking),
                    )
                final_contents.extend(remaining_tool_blocks)
                if text:
                    final_contents.append(TextBlock(type="text", text=text))
                metadata = self._merge_metadata_with_kv_cache(metadata, raw_usage)
                res = ChatResponse(
                    content=final_contents,
                    usage=usage,
                    metadata=metadata,
                )
                yield res
