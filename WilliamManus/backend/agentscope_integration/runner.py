"""
AgentScope Runner - Main Entry Point

This module provides the main runner for AgentScope-based agent execution.
It orchestrates the Orchestrator-Worker architecture and handles streaming output.

Key features:
- Orchestrator has access to ALL tools and handles most tasks directly
- Worker is only used for deep research tasks
- Memory compression is enabled for Orchestrator
"""

import json
import os
import re
from typing import Any, AsyncGenerator, Dict, List, Optional
from datetime import datetime, timezone

from agentscope.message import Base64Source, ImageBlock, Msg, TextBlock
from agentscope.pipeline import stream_printing_messages

from .models import DashScopeQwenNativeChatModel, GlobalFallbackChatModel, ModelFactory
from .agents import OrchestratorAgent, WorkerAgent
from .context_builder import ContextBuilder, ToolCallMemoryPolicy
from .tools import ToolkitAdapter
from .memory import (
    MessagesTableMemory,
    ReadOnlyDelegateLongTermMemory,
    create_long_term_memory,
    load_ltm_settings_from_env,
)
from .streaming import SSEAdapter
from .adapters import ThreadManagerAdapter
from .state import ResumeCoordinator, ResumeState

from utils.logger import logger
from utils.agent_run_context import set_agent_run_context, get_agent_run_context

# ── System-note detection helpers ──────────────────────────────────────
_SYSTEM_NOTE_PREFIX = "System note:"
_SYSTEM_NOTE_ESCALATION_PREFIX = "System: You have made"


def _block_field(block, field: str, default=None):
    """Read a field from an AgentScope content block.

    Content blocks may be TypedDicts (plain dicts at runtime) or objects,
    so we try dict .get() first, then fall back to getattr().
    """
    if isinstance(block, dict):
        return block.get(field, default)
    return getattr(block, field, default)


def _msg_contains_system_note(msg) -> bool:
    """Check whether an AgentScope Msg contains a System-note TextBlock."""
    if msg is None:
        return False
    content = _block_field(msg, "content")
    if isinstance(content, list):
        for b in content:
            if _block_field(b, "type") == "text":
                _text = _block_field(b, "text", "")
                if isinstance(_text, str) and (
                    _text.startswith(_SYSTEM_NOTE_PREFIX)
                    or _text.startswith(_SYSTEM_NOTE_ESCALATION_PREFIX)
                ):
                    return True
        return False
    # Single content block
    if _block_field(content, "type") == "text":
        _text = _block_field(content, "text", "")
        return isinstance(_text, str) and (
            _text.startswith(_SYSTEM_NOTE_PREFIX)
            or _text.startswith(_SYSTEM_NOTE_ESCALATION_PREFIX)
        )
    return False


def _is_internal_user_echo_sse(sse_msg: Dict[str, Any]) -> bool:
    """Return True for AgentScope-emitted user/control messages.

    The API already persists the real user's request before the background run
    starts. AgentScope's streaming pipeline may also yield the input Msg again
    between ReAct/tool rounds (including long-term-memory control messages).
    Forwarding those echoes to Redis/frontend makes them look like fresh user
    turns, pollutes DB history, and can cause the next context build to replay
    the original request mid-run.
    """
    if not isinstance(sse_msg, dict):
        return False
    if str(sse_msg.get("type") or "").lower() != "user":
        return False
    return not bool(sse_msg.get("is_llm_message"))


class AgentScopeRunner:
    """
    Main runner for AgentScope-based agent execution.

    This class:
    1. Creates and configures the Orchestrator-Worker agent system
    2. Handles streaming output conversion to SSE format
    3. Manages memory persistence to the existing messages table
    """

    def __init__(
        self,
        thread_id: str,
        project_id: str,
        model_key: str = "gemini-3-flash",
        worker_model_key: Optional[str] = None,
        reasoning_effort: Optional[str] = None,
        db_client=None,
        trace=None,
    ):
        """
        Initialize the AgentScope runner.

        Args:
            thread_id: Thread ID for this conversation
            project_id: Project ID for sandbox tools
            model_key: Model to use (e.g., "gemini-3-flash", "kimi-k2.5")
            worker_model_key: Optional model key override for Worker (deep research)
            db_client: Database client for memory persistence and tools
        """
        self.thread_id = thread_id
        self.project_id = project_id
        self.model_key = model_key
        self.reasoning_effort = reasoning_effort
        self.worker_model_key = (
            worker_model_key
            or os.environ.get("AGENTSCOPE_WORKER_MODEL")
            or os.environ.get("AGENTSCOPE_DEEPRESEARCH_MODEL")
        )
        self.db_client = db_client
        self._lf_trace = trace
        self._lf_orch_prompt_obj = None
        self._lf_worker_prompt_obj = None

        # Create thread manager adapter for tool classes
        self.thread_manager_adapter = ThreadManagerAdapter(db_client=db_client)

        # These will be initialized in setup()
        self.model = None
        self.formatter = None
        self.memory = None
        self.toolkit_adapter = None
        self.worker = None
        self.orchestrator = None
        self.long_term_memory = None
        self.worker_long_term_memory = None
        self.resume_coordinator: Optional[ResumeCoordinator] = None
        self._ltm_entered = False
        self._ltm_settings = load_ltm_settings_from_env()
        self._log_ltm_settings_summary()
        self._context_builder = ContextBuilder.from_env()
        context_settings = self._context_builder.settings

        self._drop_tool_call_only = False
        self._keep_tool_results = False
        self._retain_complete_tool_runs = context_settings.retain_complete_tool_runs
        self._enable_tool_history_summary = context_settings.enable_tool_history_summary
        self._tool_history_summary_max_runs = (
            context_settings.tool_history_summary_max_runs
        )
        self._tool_history_summary_max_chars = (
            context_settings.tool_history_summary_max_chars
        )
        self._global_fallback_model_key = os.environ.get(
            "AGENTSCOPE_GLOBAL_FALLBACK_MODEL",
            "",
        )
        self._qwen_primary_retries = self._read_non_negative_int_env(
            "AGENTSCOPE_QWEN_PRIMARY_RETRIES",
            default=2,
        )
        self._qwen_last_alternative_enabled = self._env_flag(
            "AGENTSCOPE_QWEN_LAST_ALTERNATIVE_ENABLED",
            default=True,
        )
        self._qwen_last_alternative_model_key = os.environ.get(
            "AGENTSCOPE_QWEN_LAST_ALTERNATIVE_MODEL",
            self._global_fallback_model_key or "glm-4.7",
        )
        self._qwen_disable_cross_model_fallback = self._env_flag(
            "AGENTSCOPE_QWEN_DISABLE_CROSS_MODEL_FALLBACK",
            default=True,
        )
        self._kv_cache_enabled = self._env_flag(
            "AGENTSCOPE_KV_CACHE_ENABLED",
            default=True,
        )
        self._kv_cache_provider_mode = (
            str(
                os.environ.get("AGENTSCOPE_KV_CACHE_PROVIDER_MODE", "openrouter"),
            )
            .strip()
            .lower()
        )
        self._kv_cache_breakpoint_mode = (
            str(
                os.environ.get("AGENTSCOPE_KV_CACHE_BREAKPOINT_MODE", "auto"),
            )
            .strip()
            .lower()
        )
        if self._kv_cache_breakpoint_mode not in {"auto", "manual"}:
            self._kv_cache_breakpoint_mode = "auto"
        self._kv_cache_session_sticky = self._env_flag(
            "AGENTSCOPE_KV_CACHE_SESSION_STICKY",
            default=True,
        )
        self._kv_cache_session_ttl_seconds = self._read_non_negative_int_env(
            "AGENTSCOPE_KV_CACHE_SESSION_TTL_SECONDS",
            default=7200,
        )
        self._kv_cache_canonical_json = context_settings.kv_cache_canonical_json
        self._kv_cache_metrics_enabled = self._env_flag(
            "AGENTSCOPE_KV_CACHE_METRICS_ENABLED",
            default=True,
        )
        self._kv_cache_shadow_log_only = self._env_flag(
            "AGENTSCOPE_KV_CACHE_SHADOW_LOG_ONLY",
            default=False,
        )
        self._kv_cache_min_prefix_tokens = self._read_non_negative_int_env(
            "AGENTSCOPE_KV_CACHE_MIN_PREFIX_TOKENS",
            default=2048,
        )
        self._kv_cache_contract_version = context_settings.kv_cache_contract_version
        self._memory_policy: Optional[ToolCallMemoryPolicy] = None
        self._multimodal_native_vision_enabled = self._env_flag(
            "AGENTSCOPE_MULTIMODAL_NATIVE_VISION_ENABLED",
            default=True,
        )
        self._multimodal_image_only = self._env_flag(
            "AGENTSCOPE_MULTIMODAL_IMAGE_ONLY",
            default=True,
        )
        self._multimodal_block_ocr_tools = self._env_flag(
            "AGENTSCOPE_MULTIMODAL_BLOCK_OCR_TOOLS",
            default=True,
        )
        self._preload_orchestrator_history = self._env_flag(
            "AGENTSCOPE_PRELOAD_ORCHESTRATOR_HISTORY",
            default=False,
        )
        self._multimodal_max_image_bytes = self._read_non_negative_int_env(
            "AGENTSCOPE_MULTIMODAL_MAX_IMAGE_BYTES",
            default=10 * 1024 * 1024,
        )
        self._qwen_image_dual_stage_enabled = self._env_flag(
            "AGENTSCOPE_QWEN_IMAGE_DUAL_STAGE_ENABLED",
            default=True,
        )
        self._qwen_image_grounding_max_chars = self._read_non_negative_int_env(
            "AGENTSCOPE_QWEN_IMAGE_GROUNDING_MAX_CHARS",
            default=3500,
        )
        self._qwen_second_visual_pass_enabled = self._env_flag(
            "AGENTSCOPE_QWEN_SECOND_VISUAL_PASS_ENABLED",
            default=True,
        )
        self._qwen_second_visual_pass_uncertainty_chars = (
            self._read_non_negative_int_env(
                "AGENTSCOPE_QWEN_SECOND_VISUAL_PASS_UNCERTAINTY_CHARS",
                default=24,
            )
        )
        self._initialized = False

    def _ltm_backend_hint(self) -> str:
        config_path = str(self._ltm_settings.reme_config_path or "").lower()
        if not config_path:
            return "default_reme_config(memory-likely)"
        if "qdrant" in config_path:
            return "qdrant_configured"
        return "custom_configured"

    def _log_ltm_settings_summary(self) -> None:
        logger.info(
            "[AgentScopeRunner] LTM settings enabled=%s attach_scope=%s mode=%s memories=%s "
            "fail_open=%s static_record=%s reme_config_path=%s backend_hint=%s workspace=%s",
            self._ltm_settings.enabled,
            self._ltm_settings.attach_scope,
            self._ltm_settings.control_mode,
            ",".join(self._ltm_settings.memories),
            self._ltm_settings.fail_open,
            self._ltm_settings.static_record_enabled,
            self._ltm_settings.reme_config_path or "<default>",
            self._ltm_backend_hint(),
            self._ltm_settings.global_workspace,
        )

    @staticmethod
    def _env_flag(name: str, default: bool) -> bool:
        value = os.environ.get(name)
        if value is None:
            return default
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    @staticmethod
    def _read_non_negative_int_env(name: str, default: int) -> int:
        value = os.environ.get(name)
        if value is None:
            return default
        try:
            return max(0, int(value))
        except ValueError:
            logger.warning(
                "[AgentScopeRunner] Invalid integer value for %s=%s. Using default=%s",
                name,
                value,
                default,
            )
            return default

    @staticmethod
    def _normalize_image_media_refs(
        raw_refs: Optional[List[Dict[str, Any]]],
    ) -> List[Dict[str, str]]:
        normalized: List[Dict[str, str]] = []
        if not raw_refs:
            return normalized

        for ref in raw_refs:
            if not isinstance(ref, dict):
                continue

            kind = str(ref.get("kind") or "").strip().lower()
            if kind != "image":
                continue

            path = str(ref.get("path") or "").strip()
            if not path.startswith("/workspace/"):
                continue

            mime_type = str(ref.get("mime_type") or "").strip().lower()
            if not mime_type.startswith("image/"):
                mime_type = "image/png"

            filename = str(ref.get("filename") or "").strip()
            if not filename:
                filename = os.path.basename(path) or "uploaded_image"

            normalized_ref: Dict[str, str] = {
                "kind": "image",
                "path": path,
                "mime_type": mime_type,
                "filename": filename,
            }

            sha256 = str(ref.get("sha256") or "").strip().lower()
            if sha256:
                normalized_ref["sha256"] = sha256

            normalized.append(normalized_ref)

        return normalized

    async def _build_user_content_blocks(
        self,
        final_user_message: str,
        image_media_refs: List[Dict[str, str]],
    ) -> List[Any]:
        content_blocks: List[Any] = []
        if final_user_message:
            content_blocks.append(TextBlock(type="text", text=final_user_message))

        if not image_media_refs:
            return content_blocks

        if not self.toolkit_adapter:
            raise RuntimeError("ToolkitAdapter is not initialized")

        for media_ref in image_media_refs:
            path = media_ref["path"]
            file_payload = await self.toolkit_adapter.read_file_base64(path)
            bytes_size = int(file_payload.get("bytes") or 0)
            if (
                self._multimodal_max_image_bytes > 0
                and bytes_size > self._multimodal_max_image_bytes
            ):
                raise RuntimeError(
                    "Uploaded image exceeds AGENTSCOPE_MULTIMODAL_MAX_IMAGE_BYTES "
                    f"({bytes_size} > {self._multimodal_max_image_bytes}): {path}",
                )

            base64_data = str(file_payload.get("base64") or "").strip()
            if not base64_data:
                raise RuntimeError(f"Failed to load uploaded image bytes from {path}")

            content_blocks.append(
                ImageBlock(
                    type="image",
                    source=Base64Source(
                        type="base64",
                        media_type=media_ref.get("mime_type", "image/png"),
                        data=base64_data,
                    ),
                ),
            )

        return content_blocks

    @staticmethod
    def _extract_json_object_from_text(text: str) -> Optional[Dict[str, Any]]:
        raw = str(text or "").strip()
        if not raw:
            return None

        candidates = [raw]
        fence_match = re.search(r"```json\s*(\{.*?\})\s*```", raw, re.S | re.I)
        if fence_match:
            candidates.append(fence_match.group(1).strip())

        for candidate in candidates:
            try:
                payload = json.loads(candidate)
                if isinstance(payload, dict):
                    return payload
            except Exception:
                continue
        return None

    @staticmethod
    def _block_value(block: Any, key: str, default: Any = None) -> Any:
        if isinstance(block, dict):
            return block.get(key, default)
        getter = getattr(block, "get", None)
        if callable(getter):
            try:
                return getter(key, default)
            except Exception:
                pass
        return getattr(block, key, default)

    def _chat_response_to_text(self, response: Any) -> str:
        blocks = getattr(response, "content", None)
        if not isinstance(blocks, (list, tuple)):
            return str(response or "").strip()

        texts: List[str] = []
        for block in blocks:
            block_type = str(self._block_value(block, "type", "") or "").strip().lower()
            if block_type == "text":
                text_value = str(self._block_value(block, "text", "") or "").strip()
                if text_value:
                    texts.append(text_value)
                continue

            if block_type == "thinking":
                think_value = str(
                    self._block_value(block, "thinking", "") or "",
                ).strip()
                if think_value:
                    texts.append(think_value)

        return "\n".join(texts).strip()

    async def _collect_final_chat_response(self, response: Any) -> Any:
        if hasattr(response, "__aiter__"):
            last = None
            async for chunk in response:
                last = chunk
            return last
        return response

    def _render_grounding_summary(
        self, payload: Dict[str, Any], fallback_text: str
    ) -> str:
        if not payload:
            return fallback_text

        ordered_keys = (
            ("scene_summary", "Scene Summary"),
            ("detected_text", "Detected Text"),
            ("layout_regions", "Layout Regions"),
            ("key_entities", "Key Entities"),
            ("uncertainties", "Uncertainties"),
            ("actionable_visual_clues", "Actionable Visual Clues"),
        )

        lines: List[str] = []
        for key, title in ordered_keys:
            value = payload.get(key)
            if value in (None, "", [], {}):
                continue
            if isinstance(value, list):
                value_text = "\n".join(
                    f"- {str(item).strip()}" for item in value if str(item).strip()
                )
            elif isinstance(value, dict):
                value_text = json.dumps(value, ensure_ascii=False)
            else:
                value_text = str(value).strip()
            if not value_text:
                continue
            lines.append(f"{title}:\n{value_text}")

        if not lines:
            return fallback_text
        return "\n\n".join(lines).strip()

    def _build_grounded_user_message(
        self,
        original_user_message: str,
        grounding_summary: str,
    ) -> str:
        grounded = str(grounding_summary or "").strip()
        if (
            self._qwen_image_grounding_max_chars > 0
            and len(grounded) > self._qwen_image_grounding_max_chars
        ):
            grounded = grounded[: self._qwen_image_grounding_max_chars]
        original = str(original_user_message or "").strip()
        return (
            "<vision-grounding>\n"
            "Use the following image observations as the primary visual facts for this turn.\n"
            f"{grounded}\n"
            "</vision-grounding>\n\n"
            "## Current User Request\n"
            f"{original}"
        ).strip()

    @staticmethod
    def _is_high_fidelity_visual_request(user_message: str) -> bool:
        normalized = str(user_message or "").strip().lower()
        if not normalized:
            return False
        markers = (
            "replicate",
            "recreate",
            "pixel",
            "pixel-perfect",
            "exact",
            "clone this",
            "clone the ui",
            "match this ui",
            "high fidelity",
            "复刻",
            "还原",
            "像素级",
            "界面",
            "网页",
        )
        return any(marker in normalized for marker in markers)

    def _has_grounding_uncertainty(self, grounding_payload: Dict[str, Any]) -> bool:
        structured = grounding_payload.get("structured")
        uncertainty_chars = 0
        if isinstance(structured, dict):
            uncertainties = structured.get("uncertainties")
            if isinstance(uncertainties, str):
                uncertainty_chars += len(uncertainties.strip())
            elif isinstance(uncertainties, list):
                uncertainty_chars += sum(
                    len(str(item).strip())
                    for item in uncertainties
                    if str(item).strip()
                )
            elif uncertainties not in (None, "", [], {}):
                uncertainty_chars += len(str(uncertainties).strip())

        if uncertainty_chars <= 0:
            summary = str(grounding_payload.get("summary") or "").strip().lower()
            uncertainty_chars = (
                64
                if any(
                    token in summary
                    for token in ("uncertain", "not sure", "可能", "不确定", "难以辨认")
                )
                else 0
            )

        return uncertainty_chars >= self._qwen_second_visual_pass_uncertainty_chars

    def _should_run_qwen_second_visual_pass(
        self,
        *,
        user_message: str,
        grounding_payload: Dict[str, Any],
    ) -> bool:
        if not self._qwen_second_visual_pass_enabled:
            return False
        if not self._is_high_fidelity_visual_request(user_message):
            return False
        return self._has_grounding_uncertainty(grounding_payload)

    @staticmethod
    def _merge_grounding_payloads(
        primary_payload: Dict[str, Any],
        refinement_payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        if not refinement_payload:
            return primary_payload

        primary_summary = str(primary_payload.get("summary") or "").strip()
        refinement_summary = str(refinement_payload.get("summary") or "").strip()
        merged_summary_parts = [
            part for part in (primary_summary, refinement_summary) if part
        ]
        merged_summary = "\n\n".join(merged_summary_parts).strip()

        merged_structured = refinement_payload.get("structured") or primary_payload.get(
            "structured"
        )
        merged_raw_text_parts = [
            str(primary_payload.get("raw_text") or "").strip(),
            str(refinement_payload.get("raw_text") or "").strip(),
        ]
        merged_raw_text = "\n\n".join(
            part for part in merged_raw_text_parts if part
        ).strip()

        merged_payload = dict(primary_payload)
        merged_payload["summary"] = merged_summary or primary_summary
        merged_payload["raw_text"] = merged_raw_text
        merged_payload["structured"] = merged_structured
        merged_payload["refined"] = True
        return merged_payload

    async def _run_qwen_image_grounding(
        self,
        *,
        user_message: str,
        image_media_refs: List[Dict[str, str]],
        detail_mode: bool = False,
        prior_summary: str = "",
    ) -> Dict[str, Any]:
        if not image_media_refs:
            return {}
        if self.model is None or self.formatter is None:
            raise RuntimeError("Runner model/formatter not initialized")

        if detail_mode:
            base_request = (
                str(user_message or "").strip()
                or "Please analyze the uploaded image(s)."
            )
            grounding_prompt = (
                "You are a vision grounding refinement module for downstream UI/code execution.\n"
                "Analyze the uploaded image(s) and return a JSON object with keys:\n"
                "scene_summary, detected_text, layout_regions, key_entities, uncertainties, actionable_visual_clues.\n"
                "Focus on implementation-critical details: spacing, typography hierarchy, colors, borders/radius,\n"
                "component states, alignment constraints, and responsive hints.\n"
                "Requirements:\n"
                "- Keep statements factual and image-grounded.\n"
                "- If uncertain, explicitly state the ambiguity and the likely alternatives under uncertainties.\n"
                "- Do not call tools and do not plan tool execution.\n"
                "- Prefer CSS-ready wording and measurable cues when possible.\n\n"
                f"Previous grounding summary (for refinement):\n{str(prior_summary or '').strip()}\n\n"
                f"User request:\n{base_request}"
            )
        else:
            grounding_prompt = (
                "You are a vision grounding module for downstream task execution.\n"
                "Analyze the uploaded image(s) carefully and output a JSON object with keys:\n"
                "scene_summary, detected_text, layout_regions, key_entities, uncertainties, actionable_visual_clues.\n"
                "Requirements:\n"
                "- Keep statements factual and image-grounded.\n"
                "- If uncertain, state it under uncertainties.\n"
                "- Do not call tools and do not plan tool execution.\n\n"
                "User request:\n"
                f"{str(user_message or '').strip() or 'Please analyze the uploaded image(s).'}"
            )

        content_blocks = await self._build_user_content_blocks(
            final_user_message=grounding_prompt,
            image_media_refs=image_media_refs,
        )
        formatted_messages = await self.formatter.format(
            msgs=[
                Msg(
                    name="system",
                    content=(
                        "You produce high-fidelity visual grounding from uploaded images. "
                        "Return only one JSON object."
                    ),
                    role="system",
                ),
                Msg(name="user", content=content_blocks, role="user"),
            ],
        )
        response = await self.model(
            messages=formatted_messages,
            tools=None,
            tool_choice="none",
        )
        final_response = await self._collect_final_chat_response(response)
        if final_response is None:
            return {}
        response_text = self._chat_response_to_text(final_response)
        payload = self._extract_json_object_from_text(response_text) or {}
        summary = self._render_grounding_summary(payload, response_text)
        return {
            "summary": summary,
            "raw_text": response_text,
            "structured": payload if payload else None,
        }

    def _should_use_qwen_image_dual_stage(
        self,
        *,
        use_native_multimodal: bool,
        image_media_refs: List[Dict[str, str]],
    ) -> bool:
        return bool(
            self._qwen_image_dual_stage_enabled
            and use_native_multimodal
            and self._is_qwen_model_key(self.model_key)
            and image_media_refs
        )

    def _resolve_tool_memory_policy(
        self, model_key: Optional[str]
    ) -> ToolCallMemoryPolicy:
        return self._context_builder.resolve_tool_memory_policy(model_key)

    def _uses_openai_memory_policy(self) -> bool:
        return self._context_builder.uses_openai_memory_policy(
            self._memory_policy,
        )

    @staticmethod
    def _is_qwen_model_key(model_key: str) -> bool:
        return ContextBuilder.is_qwen_model_key(model_key)

    def _effective_kv_cache_enabled(self) -> bool:
        return bool(
            self._kv_cache_enabled and self._kv_cache_provider_mode == "openrouter"
        )

    def _kv_cache_options(self) -> dict:
        return {
            "enabled": self._effective_kv_cache_enabled(),
            "provider_mode": self._kv_cache_provider_mode,
            "breakpoint_mode": self._kv_cache_breakpoint_mode,
            "session_sticky": self._kv_cache_session_sticky,
            "session_ttl_seconds": self._kv_cache_session_ttl_seconds,
            "canonical_json": self._kv_cache_canonical_json,
            "metrics_enabled": self._kv_cache_metrics_enabled,
            "shadow_log_only": self._kv_cache_shadow_log_only,
            "min_prefix_tokens": self._kv_cache_min_prefix_tokens,
            "cache_contract_version": self._kv_cache_contract_version,
        }

    @staticmethod
    def _sync_model_cache_context(model: object, thread_id: str) -> None:
        if not hasattr(model, "set_cache_context"):
            return
        try:
            model.set_cache_context(  # type: ignore[attr-defined]
                thread_id=thread_id,
            )
        except Exception as exc:
            logger.debug(
                "[AgentScopeRunner] Failed to sync model cache context: %s",
                exc,
            )

    def _wrap_with_global_fallback(self, model, primary_model_key: str):
        normalized_primary_key = str(primary_model_key or "").lower()
        fallback_model_key = (self._global_fallback_model_key or "").strip()
        primary_retries = 0
        fallback_on_stream_error_after_output = True

        if self._is_qwen_model_key(primary_model_key):
            primary_retries = self._qwen_primary_retries
            fallback_on_stream_error_after_output = False
            if self._qwen_disable_cross_model_fallback:
                fallback_model_key = ""
            elif self._qwen_last_alternative_enabled:
                fallback_model_key = str(
                    self._qwen_last_alternative_model_key or "",
                ).strip()
            else:
                fallback_model_key = ""

            logger.info(
                "[AgentScopeRunner] Qwen fallback policy: retries=%s disable_cross_model_fallback=%s last_alternative_enabled=%s last_alternative=%s",
                primary_retries,
                self._qwen_disable_cross_model_fallback,
                self._qwen_last_alternative_enabled,
                fallback_model_key or "disabled",
            )

        # DeepSeek official API fallback: prefer deepseek-v4-flash-max (same
        # thinking format, same reasoning_effort=max, no reasoning_content
        # stripping needed).  Only applies when no explicit fallback is set.
        # Exclude OpenRouter-routed DeepSeek keys (those don't use the native
        # API and may not have DEEPSEEK_API_KEY configured).
        if (
            not fallback_model_key
            and "deepseek" in normalized_primary_key
            and not normalized_primary_key.startswith("openrouter")
        ):
            fallback_model_key = "deepseek-v4-flash-max"
            logger.info(
                "[AgentScopeRunner] DeepSeek fallback policy: defaulting to %s",
                fallback_model_key,
            )

        if fallback_model_key and normalized_primary_key == fallback_model_key.lower():
            fallback_model_key = ""

        if not fallback_model_key and primary_retries <= 0:
            return model

        logger.info(
            "[AgentScopeRunner] Global fallback configured: %s -> %s (retries=%s)",
            primary_model_key,
            fallback_model_key or "disabled",
            primary_retries,
        )
        return GlobalFallbackChatModel(
            primary_model=model,
            primary_model_key=primary_model_key,
            fallback_model_key=fallback_model_key,
            fallback_factory=lambda stream: ModelFactory.create(
                fallback_model_key,
                stream=stream,
                reasoning_effort=self.reasoning_effort,
                kv_cache_options=self._kv_cache_options(),
                trace=self._lf_trace,
            )[0],
            primary_max_retries=primary_retries,
            fallback_on_stream_error_after_output=(
                fallback_on_stream_error_after_output
            ),
        )

    async def setup(self):
        """
        Initialize all components.

        This is called lazily on first run to allow async initialization.
        Note: Worker memory is created in run() because it needs thread_run_id.
        """
        if self._initialized:
            return

        _lf_span = None
        if self._lf_trace:
            try:
                _lf_span = self._lf_trace.start_observation(
                    name="agent_setup",
                    as_type="span",
                    input={"model_key": self.model_key, "thread_id": self.thread_id},
                )
            except Exception:
                pass

        logger.info("[AgentScopeRunner] Setting up with model: %s", self.model_key)

        self._memory_policy = self._resolve_tool_memory_policy(self.model_key)
        self._drop_tool_call_only = self._memory_policy.drop_tool_call_only
        self._keep_tool_results = self._memory_policy.keep_tool_results
        logger.info(
            "[AgentScopeRunner] Tool-call memory policy: format=%s keep_tool_results=%s "
            "drop_tool_call_only=%s retain_complete_tool_runs=%s "
            "enable_tool_history_summary=%s "
            "kv_cache_enabled=%s kv_cache_breakpoint_mode=%s "
            "kv_cache_session_sticky=%s kv_cache_shadow_log_only=%s "
            "kv_cache_min_prefix_tokens=%s kv_cache_contract_version=%s",
            self._memory_policy.api_format,
            self._keep_tool_results,
            self._drop_tool_call_only,
            self._retain_complete_tool_runs,
            self._enable_tool_history_summary,
            self._effective_kv_cache_enabled(),
            self._kv_cache_breakpoint_mode,
            self._kv_cache_session_sticky,
            self._kv_cache_shadow_log_only,
            self._kv_cache_min_prefix_tokens,
            self._kv_cache_contract_version,
        )
        logger.info(
            "[AgentScopeRunner] Qwen image dual-stage: enabled=%s grounding_max_chars=%s "
            "second_visual_pass_enabled=%s second_visual_uncertainty_chars=%s",
            self._qwen_image_dual_stage_enabled,
            self._qwen_image_grounding_max_chars,
            self._qwen_second_visual_pass_enabled,
            self._qwen_second_visual_pass_uncertainty_chars,
        )

        # Create model and formatter for Orchestrator
        kv_cache_options = self._kv_cache_options()
        # Fetch Langfuse prompt objects for link-to-traces (best-effort)
        try:
            from agentscope_integration.prompts.orchestrator_prompt import (
                get_orchestrator_prompt_object,
            )
            from agentscope_integration.prompts.worker_prompt import (
                get_worker_prompt_object,
            )

            self._lf_orch_prompt_obj = get_orchestrator_prompt_object()
            self._lf_worker_prompt_obj = get_worker_prompt_object()
        except Exception as _e:
            logger.warning(f"[Runner] Failed to fetch Langfuse prompt objects: {_e}")
        self.model, self.formatter = ModelFactory.create(
            self.model_key,
            reasoning_effort=self.reasoning_effort,
            kv_cache_options=kv_cache_options,
            trace=self._lf_trace,
            prompt=self._lf_orch_prompt_obj,
        )
        if self._is_qwen_model_key(self.model_key) and not isinstance(
            self.model,
            DashScopeQwenNativeChatModel,
        ):
            raise RuntimeError(
                "Qwen3.5 must use DashScope-native model wrapper. "
                f"Got {self.model.__class__.__name__} for model_key={self.model_key}.",
            )
        self.model = self._wrap_with_global_fallback(
            self.model,
            self.model_key,
        )
        # Always build a dedicated worker model instance so its _lf_prompt_obj
        # points at worker-system. Without this, worker LLM calls would link
        # to orchestrator-system when model_key == worker_model_key.
        _effective_worker_key = self.worker_model_key or self.model_key
        self.worker_model, self.worker_formatter = ModelFactory.create(
            _effective_worker_key,
            reasoning_effort=self.reasoning_effort,
            kv_cache_options=kv_cache_options,
            trace=self._lf_trace,
            prompt=self._lf_worker_prompt_obj,
        )
        if self.worker_model_key and self.worker_model_key != self.model_key:
            if self._is_qwen_model_key(self.worker_model_key) and not isinstance(
                self.worker_model,
                DashScopeQwenNativeChatModel,
            ):
                raise RuntimeError(
                    "Worker Qwen3.5 model must use DashScope-native model wrapper. "
                    f"Got {self.worker_model.__class__.__name__} for model_key={self.worker_model_key}.",
                )
            self.worker_model = self._wrap_with_global_fallback(
                self.worker_model,
                self.worker_model_key,
            )
            logger.info(
                "[AgentScopeRunner] Worker model override: %s",
                self.worker_model_key,
            )

        # Create Orchestrator Memory - PostgreSQL based
        if self.db_client:
            self.orchestrator_memory = MessagesTableMemory(
                db_client=self.db_client,
                **self._context_builder.build_orchestrator_memory_kwargs(
                    thread_id=self.thread_id,
                    project_id=self.project_id,
                    model_key=self.model_key,
                ),
            )
            self.resume_coordinator = ResumeCoordinator(
                db_client=self.db_client,
                thread_id=self.thread_id,
                project_id=self.project_id,
            )
        else:
            # Fallback for testing without database
            from agentscope.memory import InMemoryMemory

            self.orchestrator_memory = InMemoryMemory()

        # Create toolkit adapter with all tools
        # Pass the thread_manager_adapter so tools can access the database
        self.toolkit_adapter = ToolkitAdapter(
            project_id=self.project_id,
            thread_id=self.thread_id,
            thread_manager=self.thread_manager_adapter,
            db_client=self.db_client,
            trace=self._lf_trace,
        )

        # Worker will be created in run() with thread_run_id for proper filtering
        from agentscope.memory import InMemoryMemory

        worker_long_term_memory = None

        orchestrator_long_term_memory = None
        if self._ltm_settings.enabled:
            logger.info(
                "[AgentScopeRunner] LTM bootstrap start config_path=%s backend_hint=%s",
                self._ltm_settings.reme_config_path or "<default>",
                self._ltm_backend_hint(),
            )
            if self._ltm_settings.attach_scope != "orchestrator":
                logger.info(
                    "[AgentScopeRunner] LTM enabled but attach_scope=%s is unsupported in v1. "
                    "Proceeding without LTM attachment.",
                    self._ltm_settings.attach_scope,
                )
            else:
                try:
                    self.long_term_memory = create_long_term_memory(
                        thread_id=self.thread_id,
                        settings=self._ltm_settings,
                    )
                    if self.long_term_memory:
                        await self.long_term_memory.__aenter__()
                        self._ltm_entered = True
                        orchestrator_long_term_memory = self.long_term_memory
                        worker_long_term_memory = ReadOnlyDelegateLongTermMemory(
                            self.long_term_memory,
                        )
                        logger.info(
                            "[AgentScopeRunner] LTM sidecar attached to Orchestrator mode=%s memories=%s "
                            "config_path=%s backend_hint=%s",
                            self._ltm_settings.control_mode,
                            ",".join(self._ltm_settings.memories),
                            self._ltm_settings.reme_config_path or "<default>",
                            self._ltm_backend_hint(),
                        )
                    else:
                        logger.info(
                            "[AgentScopeRunner] LTM enabled but no memories were created from current config.",
                        )
                except Exception as exc:
                    if self._ltm_settings.fail_open:
                        logger.warning(
                            "[AgentScopeRunner] LTM initialization failed in fail-open mode: %s "
                            "(config_path=%s backend_hint=%s)",
                            exc,
                            self._ltm_settings.reme_config_path or "<default>",
                            self._ltm_backend_hint(),
                        )
                        self.long_term_memory = None
                        worker_long_term_memory = None
                        self._ltm_entered = False
                    else:
                        raise

        self.worker_long_term_memory = worker_long_term_memory

        self.worker = WorkerAgent(
            model=self.worker_model,
            formatter=self.worker_formatter,
            toolkit=self.toolkit_adapter.get_toolkit(),
            memory=InMemoryMemory(),
            long_term_memory=self.worker_long_term_memory,
            long_term_memory_mode="static_control",
        )

        self.orchestrator = OrchestratorAgent(
            model=self.model,
            formatter=self.formatter,
            worker=self.worker,
            full_toolkit=self.toolkit_adapter.get_toolkit(),
            memory=self.orchestrator_memory,
            long_term_memory=orchestrator_long_term_memory,
            long_term_memory_mode=self._ltm_settings.control_mode,
            resume_coordinator=self.resume_coordinator,
        )

        self._initialized = True
        logger.info("[AgentScopeRunner] Setup complete")

        # Auto Compact: initialise manager and attach post_reasoning hook
        self._init_auto_compact()

        if _lf_span:
            try:
                _lf_span.update(output={"status": "ok", "model_key": self.model_key})
                _lf_span.end()
            except Exception:
                pass

    def _init_auto_compact(self) -> None:
        """Create AutoCompactManager and register post_reasoning hook."""
        settings = self._context_builder.settings
        if not settings.enable_auto_compact:
            self._auto_compact_manager = None
            return

        from agentscope_integration.compaction.auto_compact import (
            AutoCompactConfig,
            AutoCompactManager,
        )
        import os

        cfg = AutoCompactConfig(
            enabled=settings.enable_auto_compact,
            trigger_tokens=settings.auto_compact_trigger_tokens,
            tail_turns=settings.auto_compact_tail_turns,
            compact_model_key=settings.auto_compact_model_key,
            shadow_mode=settings.auto_compact_shadow_mode,
        )
        self._auto_compact_manager = AutoCompactManager(config=cfg)
        self._auto_compact_tokens = 0

        # Attach post_reasoning hook to trigger auto-compact check
        mgr = self._auto_compact_manager
        memory = self.orchestrator_memory
        model_factory = ModelFactory.create

        # CompactAgent handles its own formatting; pass a simple factory
        def _fmt_factory(model_key):
            return None  # CompactAgent falls back to raw dict messages

        async def _auto_compact_hook(agent, kwargs, output):
            # post_reasoning receives (self, kwargs, output)
            msg = (
                output
                if output is not None
                else (kwargs.get("msg") if isinstance(kwargs, dict) else None)
            )
            if msg is None:
                return None
            # Estimate prompt tokens and cached tokens from metadata.
            prompt_tokens = 0
            cached_tokens = 0
            meta = getattr(msg, "metadata", None)
            if isinstance(meta, dict):
                prompt_tokens = int(
                    meta.get("kv_cache_prompt_tokens", 0)
                    or meta.get("prompt_tokens", 0)
                    or meta.get("input_tokens", 0)
                    or 0
                )
                cached_tokens = int(meta.get("kv_cache_cached_input_tokens", 0) or 0)
            # Fallback: estimate from messages in memory (char // 3).
            if prompt_tokens <= 0:
                try:
                    msgs_fb = await memory.get_memory()
                    total_chars = sum(
                        len(m.content if isinstance(m.content, str) else str(m.content))
                        for m in (msgs_fb or [])
                        if hasattr(m, "content")
                    )
                    prompt_tokens = max(1, total_chars // 3)
                    cached_tokens = 0  # no cache info in fallback
                except Exception:
                    prompt_tokens = 16_000
            if prompt_tokens <= 0:
                return None

            # Track NEW tokens (actual context growth, not KV-cached overhead).
            # DeepSeek KV cache hits 97%+ so prompt_tokens ≈ 30k but new content
            # is typically 500-1500 tokens. Accumulating prompt_tokens would
            # falsely trigger compaction every 3 rounds.
            new_tokens = max(0, prompt_tokens - cached_tokens)

            self._auto_compact_tokens = prompt_tokens

            # Check if compaction needed (uses cumulative NEW tokens)
            if not mgr.should_compact(new_tokens):
                return None

            # Run compaction
            try:
                # Gather messages from memory
                msgs = await memory.get_memory()
                rows = [
                    {
                        "role": m.role if hasattr(m, "role") else "user",
                        "type": (
                            getattr(m, "type", m.role) if hasattr(m, "type") else m.role
                        ),
                        "content": (
                            m.content if isinstance(m.content, str) else str(m.content)
                        ),
                        "id": getattr(m, "id", None),
                        "metadata": getattr(m, "metadata", {}),
                        "tool_name": (
                            m.content[0].get("name", "")
                            if isinstance(m.content, list)
                            and m.content
                            and isinstance(m.content[0], dict)
                            else ""
                        ),
                    }
                    for m in msgs
                ]

                summary = await mgr.compact(
                    messages=rows,
                    current_tokens=prompt_tokens,
                    memory=memory,
                    model_factory=model_factory,
                    formatter_factory=_fmt_factory,
                )

                if summary:
                    logger.info(
                        "[AgentScopeRunner] Auto-compact completed: "
                        "%d chars summary",
                        len(summary),
                    )
            except Exception:
                logger.warning(
                    "[AgentScopeRunner] Auto-compact hook failed",
                    exc_info=True,
                )
            return None

        self.orchestrator.agent.register_instance_hook(
            hook_type="post_reasoning",
            hook_name="auto_compact",
            hook=_auto_compact_hook,
        )
        logger.info(
            "[AgentScopeRunner] Auto Compact hook registered "
            "(trigger=%d tokens, model=%s, shadow=%s)",
            cfg.trigger_tokens,
            cfg.compact_model_key,
            cfg.shadow_mode,
        )

    async def _close_long_term_memory(self) -> None:
        if not self.long_term_memory or not self._ltm_entered:
            return
        try:
            await self.long_term_memory.__aexit__(None, None, None)
        except Exception as exc:
            logger.warning(
                "[AgentScopeRunner] Failed to close long-term memory: %s", exc
            )
        finally:
            self._ltm_entered = False
            self.long_term_memory = None
            self.worker_long_term_memory = None

    async def close(self) -> None:
        """Close optional sidecar resources."""
        await self._close_long_term_memory()

    async def run(
        self,
        user_message: str,
        thread_run_id: str,
        user_media_refs: Optional[List[Dict[str, Any]]] = None,
        resume_strategy: str = "auto",
        resume_window_minutes: int = 1440,
    ) -> AsyncGenerator[dict, None]:
        """
        Run the agent and yield SSE-formatted streaming output.

        Args:
            user_message: The user's message/request
            user_media_refs: Optional uploaded media references from events.content.parts
            thread_run_id: Run ID for this execution
            resume_strategy: Resume strategy (auto|force_resume|fresh)
            resume_window_minutes: Resume lookup window in minutes

        Yields:
            Dict in SSE format compatible with the frontend
        """
        # Ensure setup is complete
        await self.setup()

        # Set agent run context for streaming tools (write_file, etc.)
        existing_agent_run_id, _ = get_agent_run_context()
        if existing_agent_run_id:
            set_agent_run_context(
                existing_agent_run_id,
                self.thread_id,
                model_name=self.model_key,
            )
            logger.info(
                "[AgentScopeRunner] Refreshed run context model to resolved key for run=%s model=%s",
                existing_agent_run_id,
                self.model_key,
            )
        else:
            set_agent_run_context(
                thread_run_id,
                self.thread_id,
                model_name=self.model_key,
            )
            logger.info(
                "[AgentScopeRunner] Initialized run context model for thread_run_id=%s model=%s",
                thread_run_id,
                self.model_key,
            )

        logger.info("[AgentScopeRunner] Starting run for thread %s", self.thread_id)

        image_media_refs = self._normalize_image_media_refs(user_media_refs)
        use_native_multimodal = bool(
            self._multimodal_native_vision_enabled
            and self._multimodal_image_only
            and self._is_qwen_model_key(self.model_key)
            and image_media_refs
        )
        use_qwen_image_dual_stage = self._should_use_qwen_image_dual_stage(
            use_native_multimodal=use_native_multimodal,
            image_media_refs=image_media_refs,
        )
        if image_media_refs and not use_native_multimodal:
            logger.info(
                "[AgentScopeRunner] Image media refs detected but native multimodal path is disabled for model=%s",
                self.model_key,
            )
        if use_qwen_image_dual_stage:
            logger.info(
                "[AgentScopeRunner] Qwen image dual-stage mode enabled for thread=%s run=%s (images=%s)",
                self.thread_id,
                thread_run_id,
                len(image_media_refs),
            )

        objective_fingerprint = None
        resume_state: Optional[ResumeState] = None
        resume_hint = ""

        if self.resume_coordinator:
            objective_fingerprint = self.resume_coordinator.build_objective_fingerprint(
                user_message,
            )
            if resume_strategy != "fresh":
                allow_latest_without_fingerprint = (
                    self.resume_coordinator.is_resume_intent(
                        user_message,
                    )
                )
                resume_state = await self.resume_coordinator.load_latest_state(
                    objective_fingerprint,
                    resume_window_minutes=resume_window_minutes,
                    exclude_run_id=thread_run_id,
                    allow_latest_without_fingerprint=allow_latest_without_fingerprint,
                )
                if resume_state:
                    resume_hint = self.resume_coordinator.build_resume_hint(
                        resume_state
                    )
                elif resume_strategy == "force_resume":
                    raise RuntimeError(
                        "resume_strategy=force_resume but no resumable checkpoint was found.",
                    )

        if self.orchestrator and hasattr(self.orchestrator, "set_resume_context"):
            self.orchestrator.set_resume_context(
                thread_run_id=thread_run_id,
                objective_fingerprint=objective_fingerprint,
                resume_strategy=resume_strategy,
                resume_state=resume_state,
            )

        logger.info(
            "[AgentScopeRunner] Resume resolution: strategy=%s hit=%s fingerprint=%s",
            resume_strategy,
            bool(resume_state),
            objective_fingerprint,
        )

        # Verify Orchestrator memory can load history (for debugging multi-turn issues)
        if (
            self._preload_orchestrator_history
            and self.db_client
            and hasattr(self, "orchestrator_memory")
            and self.orchestrator_memory
        ):
            try:
                history = await self.orchestrator_memory.get_memory()
                logger.info(
                    "[AgentScopeRunner] Orchestrator memory pre-loaded %d messages from history",
                    len(history),
                )
            except Exception as exc:
                logger.warning(
                    "[AgentScopeRunner] Failed to pre-load orchestrator memory: %s",
                    exc,
                )
        self._sync_model_cache_context(
            self.model,
            thread_id=self.thread_id,
        )
        self._sync_model_cache_context(
            self.worker_model,
            thread_id=self.thread_id,
        )

        # Create Worker Memory - PostgreSQL based with thread_run_id filter
        if self.db_client:
            worker_memory = MessagesTableMemory(
                db_client=self.db_client,
                **self._context_builder.build_worker_memory_kwargs(
                    thread_id=self.thread_id,
                    project_id=self.project_id,
                    thread_run_id=thread_run_id,
                    model_key=self.worker_model_key or self.model_key,
                ),
            )
            self.worker.agent.memory = worker_memory

        # Update Orchestrator memory thread_run_id for saving new messages
        if hasattr(self, "orchestrator_memory") and self.orchestrator_memory:
            self.orchestrator_memory._save_thread_run_id = thread_run_id

        if self.toolkit_adapter and hasattr(
            self.toolkit_adapter, "set_multimodal_guard_context"
        ):
            self.toolkit_adapter.set_multimodal_guard_context(
                has_image_media_refs=bool(image_media_refs),
                enforce_ocr_block=bool(
                    use_native_multimodal and self._multimodal_block_ocr_tools
                ),
                image_media_refs=image_media_refs,
            )

        final_user_message = user_message
        if resume_hint:
            final_user_message = (
                f"{resume_hint}\n\n" "## Current User Request\n" f"{user_message}"
            )

        vision_grounding_payload: Dict[str, Any] = {}
        if use_qwen_image_dual_stage:
            try:
                vision_grounding_payload = await self._run_qwen_image_grounding(
                    user_message=user_message,
                    image_media_refs=image_media_refs,
                )
                if self._should_run_qwen_second_visual_pass(
                    user_message=user_message,
                    grounding_payload=vision_grounding_payload,
                ):
                    try:
                        refinement_payload = await self._run_qwen_image_grounding(
                            user_message=user_message,
                            image_media_refs=image_media_refs,
                            detail_mode=True,
                            prior_summary=str(
                                vision_grounding_payload.get("summary") or "",
                            ),
                        )
                        vision_grounding_payload = self._merge_grounding_payloads(
                            vision_grounding_payload,
                            refinement_payload,
                        )
                        logger.info(
                            "[AgentScopeRunner] Qwen second visual pass applied "
                            "(has_refinement=%s summary_chars=%s)",
                            bool(refinement_payload),
                            len(str(vision_grounding_payload.get("summary") or "")),
                        )
                    except Exception as refine_exc:
                        logger.warning(
                            "[AgentScopeRunner] Qwen second visual pass failed; keep first pass grounding: %s",
                            refine_exc,
                        )
                grounding_summary = str(
                    vision_grounding_payload.get("summary") or "",
                ).strip()
                if grounding_summary:
                    final_user_message = self._build_grounded_user_message(
                        original_user_message=final_user_message,
                        grounding_summary=grounding_summary,
                    )
                logger.info(
                    "[AgentScopeRunner] Qwen image grounding completed (summary_chars=%s structured=%s)",
                    len(grounding_summary),
                    bool(vision_grounding_payload.get("structured")),
                )
            except Exception as exc:
                logger.warning(
                    "[AgentScopeRunner] Qwen image grounding failed, fallback to single-stage run: %s",
                    exc,
                )
                use_qwen_image_dual_stage = False

        stage_b_image_media_refs = image_media_refs
        if use_qwen_image_dual_stage:
            # Stage B relies on grounded visual facts and keeps tools enabled.
            # Avoid re-attaching raw images to prevent repeated tool-oriented detours.
            stage_b_image_media_refs = []

        qwen_stream_route_mode = ""
        if self._is_qwen_model_key(self.model_key):
            if use_qwen_image_dual_stage and image_media_refs:
                qwen_stream_route_mode = "stage_b_dashscope_execution"
            elif use_native_multimodal and stage_b_image_media_refs:
                qwen_stream_route_mode = "native_multimodal_tool_execution"
            else:
                qwen_stream_route_mode = "text_dashscope_execution"

        route_metadata: Dict[str, Any] = {}
        if qwen_stream_route_mode:
            route_metadata["qwen_stage"] = "B"
            route_metadata["qwen_route_mode"] = qwen_stream_route_mode
            route_metadata["qwen_endpoint_family"] = "multimodal_conversation"
            logger.info(
                "[AgentScopeRunner] Qwen execution route selected for streaming: %s (endpoint_family=%s)",
                qwen_stream_route_mode,
                route_metadata["qwen_endpoint_family"],
            )

        # Create SSE adapter after route decision so metadata carries stage mode.
        sse_adapter = SSEAdapter(
            self.thread_id,
            thread_run_id,
            route_metadata=route_metadata,
        )

        if use_native_multimodal and stage_b_image_media_refs:
            user_content = await self._build_user_content_blocks(
                final_user_message=final_user_message,
                image_media_refs=stage_b_image_media_refs,
            )
        else:
            user_content = final_user_message

        user_msg = Msg(name="user", content=user_content, role="user")
        msg_metadata: Dict[str, Any] = {}
        if image_media_refs:
            msg_metadata["media_refs"] = image_media_refs
        if vision_grounding_payload:
            msg_metadata["vision_mode"] = "qwen_native_dual_stage"
            msg_metadata["vision_stage"] = "execution"
            msg_metadata["vision_grounding"] = vision_grounding_payload
        if msg_metadata:
            user_msg.metadata = {
                **(getattr(user_msg, "metadata", {}) or {}),
                **msg_metadata,
            }

        # Disable console output (we handle streaming ourselves)
        self.orchestrator.set_console_output_enabled(False)
        self.worker.set_console_output_enabled(False)

        try:
            # Run with streaming
            _lf_run_span = None
            if self._lf_trace:
                try:
                    _lf_run_span = self._lf_trace.start_observation(
                        name="orchestrator_run",
                        as_type="span",
                        input={
                            "thread_run_id": thread_run_id,
                            "model_key": self.model_key,
                        },
                    )
                except Exception:
                    pass

            try:
                _system_note_seen = False
                _tool_block_seen = False
                _thinking_block_seen = False
                async for msg, is_last in stream_printing_messages(
                    agents=[self.orchestrator.get_agent(), self.worker.get_agent()],
                    coroutine_task=self.orchestrator(user_msg),
                ):
                    if not _system_note_seen and _msg_contains_system_note(msg):
                        _system_note_seen = True
                    if not _tool_block_seen and msg is not None:
                        _content = _block_field(msg, "content")
                        if isinstance(_content, list):
                            _tool_block_seen = any(
                                _block_field(b, "type") == "tool_use" for b in _content
                            )
                            if not _thinking_block_seen:
                                _thinking_block_seen = any(
                                    _block_field(b, "type") == "thinking"
                                    for b in _content
                                )
                    sse_msg = sse_adapter.convert(msg, is_last)
                    if _is_internal_user_echo_sse(sse_msg):
                        logger.debug(
                            "[AgentScopeRunner] Suppressed internal user/control "
                            "echo from AgentScope stream for thread=%s run=%s",
                            self.thread_id,
                            thread_run_id,
                        )
                        continue
                    yield sse_msg

                if _thinking_block_seen and not _tool_block_seen:
                    logger.warning(
                        "[AgentScopeRunner] Thinking-only block(s) detected "
                        "in agent output without accompanying tool calls — "
                        "the model may have been truncated or stuck in "
                        "reasoning. Post-completion zero-tool-call guard "
                        "will downgrade status if no tools were executed. "
                        "thread=%s",
                        self.thread_id,
                    )

                if _system_note_seen and not _tool_block_seen:
                    logger.warning(
                        "[AgentScopeRunner] System-note TextBlock detected "
                        "in agent output without accompanying tool calls — "
                        "the empty-tool-call → stop path may have fired. "
                        "If this occurs after the openrouter_model fix, "
                        "investigate further. thread=%s",
                        self.thread_id,
                    )
            finally:
                if _lf_run_span:
                    try:
                        _lf_run_span.update(output={"status": "completed"})
                        _lf_run_span.end()
                    except Exception:
                        pass

            logger.info(
                "[AgentScopeRunner] Run completed for thread %s", self.thread_id
            )

        except Exception as exc:
            logger.error("[AgentScopeRunner] Run failed: %s", exc)

            error_msg = {
                "sequence": sse_adapter.sequence,
                "message_id": None,
                "thread_id": self.thread_id,
                "type": "assistant",
                "is_llm_message": True,
                "content": json.dumps(
                    {
                        "role": "assistant",
                        "content": f"An error occurred: {str(exc)}",
                    }
                ),
                "metadata": json.dumps(
                    {
                        "stream_status": "error",
                        "thread_run_id": thread_run_id,
                        "error": str(exc),
                    }
                ),
                "created_at": datetime.now(timezone.utc).isoformat(),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            yield error_msg
            raise

    async def run_simple(
        self,
        user_message: str,
    ) -> str:
        """
        Run the agent and return the final response (non-streaming).

        This is useful for testing or when streaming is not needed.

        Args:
            user_message: The user's message/request

        Returns:
            The final response text
        """
        await self.setup()

        if self.toolkit_adapter and hasattr(
            self.toolkit_adapter, "set_multimodal_guard_context"
        ):
            self.toolkit_adapter.set_multimodal_guard_context(
                has_image_media_refs=False,
                enforce_ocr_block=False,
                image_media_refs=None,
            )

        user_msg = Msg(
            name="user",
            content=user_message,
            role="user",
        )

        self.orchestrator.set_console_output_enabled(False)
        self.worker.set_console_output_enabled(False)

        result = await self.orchestrator(user_msg)
        return (
            result.get_text_content()
            if hasattr(result, "get_text_content")
            else str(result)
        )
