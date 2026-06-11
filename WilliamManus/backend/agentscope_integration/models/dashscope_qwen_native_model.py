"""DashScope native chat model extensions for Qwen3.5 models."""

from __future__ import annotations

import hashlib
import json
import os
import warnings
from datetime import datetime
from typing import Any, AsyncGenerator, Literal, Type

from agentscope.model import DashScopeChatModel
from agentscope.model._dashscope_model import (
    HTTPStatus,
    _create_tool_from_base_model,
    _json_loads_with_repair,
    collections as dashscope_collections,
    giter,
)
from agentscope.model._model_response import ChatResponse, ChatUsage
from agentscope.message import ThinkingBlock, ToolUseBlock, TextBlock
from agentscope.tracing import trace_llm
from pydantic import BaseModel

from utils.logger import logger
from agentscope_integration.utils.tool_arg_merge import (
    is_write_file_tool_name,
    merge_write_file_arguments,
    merge_stream_text,
    merge_tool_arguments,
    sanitize_write_file_input,
)
from agentscope_integration.utils.tool_message_sanitizer import (
    sanitize_tool_message_sequence,
)


class DashScopeQwenNativeChatModel(DashScopeChatModel):
    """Qwen3.5 wrapper that keeps DashScope-only routing with explicit stage modes."""

    DEFAULT_DASHSCOPE_NATIVE_BASE_URL = "https://dashscope.aliyuncs.com/api/v1"

    def __init__(
        self,
        model_name: str,
        api_key: str,
        stream: bool = True,
        enable_thinking: bool | None = None,
        generate_kwargs: dict[str, Any] | None = None,
        base_http_api_url: str | None = None,
        **kwargs: Any,
    ) -> None:
        self._configured_base_http_api_url = (
            str(base_http_api_url or "").strip() or None
        )
        self._dashscope_base_log_emitted = False
        super().__init__(
            model_name=model_name,
            api_key=api_key,
            stream=stream,
            enable_thinking=enable_thinking,
            generate_kwargs=generate_kwargs,
            base_http_api_url=base_http_api_url,
            **kwargs,
        )

    @staticmethod
    def _is_qwen35_model(model_name: str) -> bool:
        normalized = str(model_name or "").lower()
        return "qwen3.5-plus" in normalized or "qwen3.5-397b-a17b" in normalized

    @staticmethod
    def _env_flag(name: str, default: bool) -> bool:
        value = os.environ.get(name)
        if value is None:
            return default
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    @staticmethod
    def _messages_contain_image_blocks(messages: list[dict[str, Any]]) -> bool:
        for message in messages:
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if not isinstance(content, list):
                continue
            for item in content:
                if not isinstance(item, dict):
                    continue
                block_type = str(item.get("type") or "").strip().lower()
                if block_type == "image":
                    return True
        return False

    @staticmethod
    def _is_http_url(value: str) -> bool:
        normalized = str(value or "").strip().lower()
        return normalized.startswith("http://") or normalized.startswith("https://")

    @staticmethod
    def _is_dashscope_compatible_mode_url(value: str) -> bool:
        return "compatible-mode" in str(value or "").strip().lower()

    def _resolve_expected_dashscope_base_url(self) -> tuple[str, str]:
        strict_native = self._env_flag(
            "AGENTSCOPE_QWEN_DASHSCOPE_STRICT_NATIVE",
            default=True,
        )
        candidates = [
            ("model_config", self._configured_base_http_api_url),
            ("DASHSCOPE_BASE_URL", os.environ.get("DASHSCOPE_BASE_URL")),
            (
                "DASHSCOPE_NATIVE_BASE_URL",
                os.environ.get("DASHSCOPE_NATIVE_BASE_URL"),
            ),
            (
                "DASHSCOPE_HTTP_BASE_URL",
                os.environ.get("DASHSCOPE_HTTP_BASE_URL"),
            ),
            ("DASHSCOPE_API_BASE", os.environ.get("DASHSCOPE_API_BASE")),
        ]

        for source, candidate in candidates:
            normalized = str(candidate or "").strip()
            if not normalized:
                continue
            if self._is_dashscope_compatible_mode_url(normalized):
                if strict_native:
                    logger.warning(
                        "[DashScopeQwenNativeChatModel] Ignoring DashScope base from %s because it points to compatible-mode: %s",
                        source,
                        normalized,
                    )
                    continue
                return normalized, source
            if not self._is_http_url(normalized):
                logger.warning(
                    "[DashScopeQwenNativeChatModel] Ignoring non-http(s) DashScope base from %s: %s",
                    source,
                    normalized,
                )
                continue
            return normalized, source

        return self.DEFAULT_DASHSCOPE_NATIVE_BASE_URL, "default_native"

    def _ensure_dashscope_base_url(self) -> tuple[str, str, bool]:
        import dashscope

        expected_base, base_source = self._resolve_expected_dashscope_base_url()
        current_base = str(getattr(dashscope, "base_http_api_url", "") or "").strip()
        healed = False
        if current_base != expected_base:
            dashscope.base_http_api_url = expected_base
            healed = True
            if current_base:
                logger.warning(
                    "[DashScopeQwenNativeChatModel] Recovered DashScope base URL before request "
                    "(from=%s to=%s source=%s)",
                    current_base,
                    expected_base,
                    base_source,
                )

        if not self._dashscope_base_log_emitted:
            self._dashscope_base_log_emitted = True
            logger.info(
                "[DashScopeQwenNativeChatModel] DashScope base resolved for model=%s "
                "(base=%s source=%s strict_native=%s openai_base_set=%s)",
                self.model_name,
                expected_base,
                base_source,
                self._env_flag("AGENTSCOPE_QWEN_DASHSCOPE_STRICT_NATIVE", default=True),
                bool(str(os.environ.get("OPENAI_BASE_URL") or "").strip()),
            )

        return expected_base, base_source, healed

    def _resolve_qwen_route_mode(
        self,
        *,
        messages: list[dict[str, Any]],
        has_tools: bool,
    ) -> str:
        has_images = self._messages_contain_image_blocks(messages)
        native_tool_calls_enabled = self._env_flag(
            "AGENTSCOPE_QWEN_NATIVE_MULTIMODAL_TOOL_CALLS_ENABLED",
            default=False,
        )
        execution_route = str(
            os.environ.get(
                "AGENTSCOPE_QWEN_TOOL_EXECUTION_ROUTE",
                "dashscope_generation",
            ),
        ).strip().lower() or "dashscope_generation"

        if has_images and not has_tools:
            return "stage_a_native_grounding"

        if has_images and has_tools:
            if native_tool_calls_enabled:
                return "native_multimodal_tool_execution"
            raise RuntimeError(
                "Qwen image+tools execution is disabled to avoid unstable path-only "
                "tool-call streams. Enable AGENTSCOPE_QWEN_IMAGE_DUAL_STAGE_ENABLED "
                "or explicitly set AGENTSCOPE_QWEN_NATIVE_MULTIMODAL_TOOL_CALLS_ENABLED=true.",
            )

        if has_tools and execution_route == "dashscope_generation":
            return "stage_b_dashscope_execution"

        if has_tools:
            logger.warning(
                "[DashScopeQwenNativeChatModel] Unknown AGENTSCOPE_QWEN_TOOL_EXECUTION_ROUTE=%s; "
                "fallback to DashScope generation execution",
                execution_route,
            )
            return "stage_b_dashscope_execution"

        return "text_dashscope_execution"

    async def _with_first_chunk_log(
        self,
        response_stream: AsyncGenerator[ChatResponse, None],
    ) -> AsyncGenerator[ChatResponse, None]:
        """Emit a single diagnostic log once stream chunks start flowing."""
        first_chunk = True
        async for chunk in response_stream:
            if first_chunk:
                first_chunk = False
                logger.info(
                    "[DashScopeQwenNativeChatModel] First stream chunk received for model=%s",
                    self.model_name,
                )
            yield chunk

    @staticmethod
    def _chunk_has_terminal_reason(choice_payload: Any) -> bool:
        """Return whether provider chunk marks stream termination."""
        finish_reason = None
        if isinstance(choice_payload, dict):
            finish_reason = choice_payload.get("finish_reason")
        else:
            finish_reason = getattr(choice_payload, "finish_reason", None)
        normalized = str(finish_reason or "").strip().lower()
        return normalized not in {"", "none", "null"}

    async def _parse_dashscope_stream_response(
        self,
        start_datetime: datetime,
        response: AsyncGenerator[Any, None],
        structured_model: Type[BaseModel] | None = None,
    ) -> AsyncGenerator[ChatResponse, Any]:
        """Parse DashScope stream while handling delta/cumulative tool-call chunks."""
        acc_content = ""
        acc_thinking_content = ""
        acc_tool_calls = dashscope_collections.defaultdict(dict)
        metadata = None

        async for chunk in giter(response):
            if chunk.status_code != HTTPStatus.OK:
                raise RuntimeError(
                    f"Failed to get response from API: {chunk}",
                )

            choice_payload = chunk.output.choices[0]
            message = choice_payload.message
            is_stream_terminal = self._chunk_has_terminal_reason(choice_payload)

            reasoning_chunk = message.get("reasoning_content")
            if isinstance(reasoning_chunk, str):
                acc_thinking_content += reasoning_chunk

            if isinstance(message.content, str):
                acc_content += message.content
            elif isinstance(message.content, list):
                for item in message.content:
                    if isinstance(item, dict) and "text" in item:
                        acc_content += item["text"]

            for tool_call in message.get("tool_calls", []):
                index = tool_call.get("index", 0)
                existing = acc_tool_calls[index]

                incoming_id = tool_call.get("id")
                if isinstance(incoming_id, str) and incoming_id:
                    existing["id"] = merge_stream_text(
                        existing.get("id", ""),
                        incoming_id,
                    )

                function_payload = tool_call.get("function")
                if not isinstance(function_payload, dict):
                    continue

                incoming_name = function_payload.get("name")
                if isinstance(incoming_name, str) and incoming_name:
                    existing["name"] = merge_stream_text(
                        existing.get("name", ""),
                        incoming_name,
                    )

                incoming_arguments = function_payload.get("arguments")
                if incoming_arguments is None:
                    continue

                if isinstance(incoming_arguments, str):
                    normalized_arguments = incoming_arguments
                else:
                    try:
                        normalized_arguments = json.dumps(
                            incoming_arguments,
                            ensure_ascii=False,
                        )
                    except Exception:
                        normalized_arguments = str(incoming_arguments)

                merged_tool_name = str(
                    existing.get("name")
                    or incoming_name
                    or "",
                )
                if is_write_file_tool_name(merged_tool_name):
                    existing["arguments"] = merge_write_file_arguments(
                        existing.get("arguments", ""),
                        normalized_arguments,
                    )
                else:
                    existing["arguments"] = merge_tool_arguments(
                        existing.get("arguments", ""),
                        normalized_arguments,
                    )

            content_blocks: list[ThinkingBlock | TextBlock | ToolUseBlock] = []
            if acc_thinking_content:
                content_blocks.append(
                    ThinkingBlock(
                        type="thinking",
                        thinking=acc_thinking_content,
                    ),
                )

            if acc_content:
                content_blocks.append(
                    TextBlock(
                        type="text",
                        text=acc_content,
                    ),
                )

            for tool_call in acc_tool_calls.values():
                tool_name = str(tool_call.get("name", "") or "")
                raw_arguments = tool_call.get("arguments", "{}") or "{}"
                repaired_input: dict[str, Any] = {}
                payload_stage = "parsed_input"
                raw_arguments_text = str(raw_arguments or "")
                args_hash = hashlib.sha1(raw_arguments_text.encode("utf-8")).hexdigest()[:12]

                if is_write_file_tool_name(tool_name):
                    strict_parsed: dict[str, Any] | None = None
                    try:
                        strict_loaded = json.loads(raw_arguments_text)
                        if isinstance(strict_loaded, dict):
                            strict_parsed = strict_loaded
                    except Exception:
                        strict_parsed = None

                    unknown_keys: list[str] = []
                    if strict_parsed is not None:
                        repaired_input = sanitize_write_file_input(
                            strict_parsed,
                            raw_arguments=raw_arguments_text,
                        )
                        payload_stage = "final_payload"
                        unknown_keys = [
                            key
                            for key in strict_parsed.keys()
                            if key
                            not in {
                                "path",
                                "file_path",
                                "target_file",
                                "file",
                                "content",
                                "file_contents",
                                "append",
                                "overwrite",
                                "mkdir_p",
                                "create_dirs",
                                "recursive",
                                "force",
                                "encoding",
                                "newline",
                                "charset",
                                "mode",
                                "permissions",
                            }
                        ]
                    else:
                        # Keep intermediate write chunks in a raw-preview path; do not run lossy repair on every chunk.
                        repaired_input = sanitize_write_file_input(
                            {},
                            raw_arguments=raw_arguments_text,
                        )
                        payload_stage = "raw_preview"
                        if is_stream_terminal:
                            repaired_candidate = _json_loads_with_repair(raw_arguments_text)
                            if isinstance(repaired_candidate, dict):
                                repaired_input = sanitize_write_file_input(
                                    repaired_candidate,
                                    raw_arguments=raw_arguments_text,
                                )
                                payload_stage = "final_repair"
                                unknown_keys = [
                                    key
                                    for key in repaired_candidate.keys()
                                    if key
                                    not in {
                                        "path",
                                        "file_path",
                                        "target_file",
                                        "file",
                                        "content",
                                        "file_contents",
                                        "append",
                                        "overwrite",
                                        "mkdir_p",
                                        "create_dirs",
                                        "recursive",
                                        "force",
                                        "encoding",
                                        "newline",
                                        "charset",
                                        "mode",
                                        "permissions",
                                    }
                                ]

                    logger.info(
                        "[DashScopeQwenNativeChatModel] write_file chunk normalized "
                        "(tool_call_id=%s raw_len=%s hash=%s stage=%s terminal=%s path=%s content_len=%s)",
                        tool_call.get("id", ""),
                        len(raw_arguments_text),
                        args_hash,
                        payload_stage,
                        is_stream_terminal,
                        repaired_input.get("path"),
                        len(str(repaired_input.get("content", ""))),
                    )
                    if unknown_keys:
                        logger.debug(
                            "[DashScopeQwenNativeChatModel] Sanitized write_file payload "
                            "(unknown_keys=%s, stage=%s, raw_len=%s, content_len=%s)",
                            unknown_keys[:4],
                            payload_stage,
                            len(raw_arguments_text),
                            len(str(repaired_input.get("content", ""))),
                        )
                else:
                    repaired_candidate = _json_loads_with_repair(raw_arguments)
                    if isinstance(repaired_candidate, dict):
                        repaired_input = repaired_candidate

                tool_block = ToolUseBlock(
                    type="tool_use",
                    id=tool_call.get("id", ""),
                    name=tool_name,
                    input=repaired_input,
                )
                if is_write_file_tool_name(tool_name):
                    # Preserve raw accumulated arguments for downstream guard diagnostics.
                    tool_block["raw_arguments"] = raw_arguments_text
                    tool_block["arguments_source"] = payload_stage
                    tool_block["raw_arguments_hash"] = args_hash
                content_blocks.append(tool_block)

                if structured_model:
                    metadata = repaired_input

            usage = None
            if chunk.usage:
                usage = ChatUsage(
                    input_tokens=chunk.usage.input_tokens,
                    output_tokens=chunk.usage.output_tokens,
                    time=(datetime.now() - start_datetime).total_seconds(),
                )

            parsed_chunk = ChatResponse(
                content=content_blocks,
                usage=usage,
                metadata=metadata,
            )
            yield parsed_chunk

    @staticmethod
    async def _enrich_stream_metadata(
        stream: AsyncGenerator,
    ) -> AsyncGenerator:
        """Wrap a ChatResponse stream to enrich the final chunk's
        metadata with kv_cache_prompt_tokens for the auto-compact hook."""
        last_chunk = None
        async for chunk in stream:
            if last_chunk is not None:
                yield last_chunk
            last_chunk = chunk
        if last_chunk is not None:
            yield DashScopeQwenNativeChatModel._enrich_metadata_with_usage(last_chunk)

    @staticmethod
    def _enrich_metadata_with_usage(parsed_response: Any) -> Any:
        """Merge usage.input_tokens into response metadata as
        kv_cache_prompt_tokens so the auto-compact hook can read
        the real input token count (mirrors OpenRouterChatModel).
        """
        usage = getattr(parsed_response, "usage", None)
        if usage is None:
            return parsed_response
        input_tokens = getattr(usage, "input_tokens", 0) or 0
        if input_tokens <= 0:
            return parsed_response
        metadata = dict(getattr(parsed_response, "metadata", None) or {})
        metadata["kv_cache_prompt_tokens"] = input_tokens
        from dataclasses import replace
        return replace(parsed_response, metadata=metadata)

    @trace_llm
    async def __call__(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict] | None = None,
        tool_choice: Literal["auto", "none", "required"] | str | None = None,
        structured_model: Type[BaseModel] | None = None,
        **kwargs: Any,
    ) -> ChatResponse | AsyncGenerator[ChatResponse, None]:
        # Keep default DashScope model behavior for non-Qwen3.5 models.
        if not self._is_qwen35_model(self.model_name):
            return await super().__call__(
                messages=messages,
                tools=tools,
                tool_choice=tool_choice,
                structured_model=structured_model,
                **kwargs,
            )

        import dashscope

        safe_messages, stats = sanitize_tool_message_sequence(messages)
        if stats.has_changes():
            logger.warning(
                "[DashScopeQwenNativeChatModel] Sanitized tool message sequence before request "
                "(dropped_empty_tool_messages=%s, dropped_orphan_tool_messages=%s, "
                "stripped_unmatched_assistant_tool_calls=%s, dropped_tool_call_only_assistant_messages=%s)",
                stats.dropped_empty_tool_messages,
                stats.dropped_orphan_tool_messages,
                stats.stripped_unmatched_assistant_tool_calls,
                stats.dropped_tool_call_only_assistant_messages,
            )

        normalized_tool_choice = (
            str(tool_choice).strip().lower() if isinstance(tool_choice, str) else tool_choice
        )
        if normalized_tool_choice == "none":
            tools = None

        has_tool_request = bool(tools) or bool(structured_model)
        if isinstance(normalized_tool_choice, str):
            has_tool_request = has_tool_request or normalized_tool_choice not in {"", "none"}
        elif normalized_tool_choice is not None:
            has_tool_request = True

        route_mode = self._resolve_qwen_route_mode(
            messages=safe_messages,
            has_tools=has_tool_request,
        )
        endpoint_family = "multimodal_conversation"
        resolved_base, base_source, base_healed = self._ensure_dashscope_base_url()
        logger.info(
            "[DashScopeQwenNativeChatModel] Route mode=%s endpoint_family=%s model=%s stream=%s tools=%s structured=%s has_images=%s base=%s base_source=%s base_healed=%s",
            route_mode,
            endpoint_family,
            self.model_name,
            self.stream,
            bool(tools),
            bool(structured_model),
            self._messages_contain_image_blocks(safe_messages),
            resolved_base,
            base_source,
            base_healed,
        )

        request_kwargs = {
            "messages": safe_messages,
            "model": self.model_name,
            "stream": self.stream,
            **self.generate_kwargs,
            **kwargs,
            "result_format": "message",
            "incremental_output": self.stream,
        }

        if tools:
            request_kwargs["tools"] = self._format_tools_json_schemas(tools)

        if tool_choice:
            if tool_choice in ["any", "required"]:
                warnings.warn(
                    f"'{tool_choice}' is not supported by DashScope API. "
                    "It will be converted to 'auto'.",
                    DeprecationWarning,
                )
                tool_choice = "auto"

            self._validate_tool_choice(tool_choice, tools)
            request_kwargs["tool_choice"] = self._format_tool_choice(tool_choice)

        if (
            self.enable_thinking is not None
            and "enable_thinking" not in request_kwargs
        ):
            request_kwargs["enable_thinking"] = self.enable_thinking

        if structured_model:
            if tools or tool_choice:
                logger.warning(
                    "structured_model is provided. Both 'tools' and "
                    "'tool_choice' parameters will be overridden and "
                    "ignored. The model will only perform structured output "
                    "generation without calling any other tools.",
                )
            format_tool = _create_tool_from_base_model(structured_model)
            request_kwargs["tools"] = self._format_tools_json_schemas([format_tool])
            request_kwargs["tool_choice"] = self._format_tool_choice(
                format_tool["function"]["name"],
            )

        logger.info(
            "[DashScopeQwenNativeChatModel] Starting AioMultiModalConversation call for model=%s stream=%s",
            self.model_name,
            self.stream,
        )
        start_datetime = datetime.now()
        response = await dashscope.AioMultiModalConversation.call(
            api_key=self.api_key,
            **request_kwargs,
        )
        logger.info(
            "[DashScopeQwenNativeChatModel] AioMultiModalConversation returned for model=%s stream=%s",
            self.model_name,
            self.stream,
        )

        if self.stream:
            parsed_stream = self._parse_dashscope_stream_response(
                start_datetime,
                response,
                structured_model,
            )
            enriched_stream = self._enrich_stream_metadata(
                parsed_stream,
            )
            return self._with_first_chunk_log(
                enriched_stream,
            )

        parsed_response = await self._parse_dashscope_generation_response(
            start_datetime,
            response,
            structured_model,
        )
        parsed_response = self._enrich_metadata_with_usage(
            parsed_response,
        )
        return parsed_response
