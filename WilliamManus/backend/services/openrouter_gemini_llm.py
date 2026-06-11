from __future__ import annotations

import hashlib
import json
from typing import Any, AsyncGenerator, Iterable, Optional

from google.adk.models.lite_llm import (
    LiteLlm,
    _append_fallback_user_content_if_missing,
    _get_completion_inputs,
    _model_response_to_generate_content_response,
    _normalize_ollama_chat_messages,
)
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.genai import types
from litellm import ChatCompletionMessageToolCall
from litellm.types.utils import Function

from utils.logger import logger


class OpenRouterGeminiLlm(LiteLlm):
    """
    Gemini 3 wrapper for OpenRouter that preserves thought signatures.

    OpenRouter returns `reasoning_details` for tool calls. We must store it
    on the response part and replay it on subsequent assistant tool-call
    messages, otherwise multi-round tool calls can stall.
    """

    def __init__(
        self,
        model: str,
        *,
        reasoning_effort: Optional[str] = None,
        **kwargs: Any,
    ) -> None:
        extra_body = dict(kwargs.pop("extra_body", {}) or {})
        if reasoning_effort:
            extra_body["reasoning"] = {"effort": reasoning_effort}
        if extra_body:
            kwargs["extra_body"] = extra_body
        super().__init__(model=model, **kwargs)

    async def generate_content_async(
        self, llm_request: LlmRequest, stream: bool = False
    ) -> AsyncGenerator[LlmResponse, None]:
        self._maybe_append_user_content(llm_request)
        _append_fallback_user_content_if_missing(llm_request)

        effective_model = llm_request.model or self.model
        messages, tools, response_format, generation_params = (
            await _get_completion_inputs(llm_request, effective_model)
        )
        messages = _normalize_ollama_chat_messages(
            messages,
            model=effective_model,
            custom_llm_provider=self._additional_args.get("custom_llm_provider"),
        )

        assistant_signatures = _assistant_signatures_from_contents(
            llm_request.contents
        )
        if assistant_signatures:
            logger.debug(
                "OpenRouter Gemini thought signatures: %s",
                _signature_cache_summary(assistant_signatures),
            )
        assistant_index = 0
        for message in messages:
            role = _message_role(message)
            if role != "assistant":
                continue
            signature_raw = (
                assistant_signatures[assistant_index]
                if assistant_index < len(assistant_signatures)
                else None
            )
            signature = _deserialize_signature(signature_raw)
            if signature is not None:
                _set_reasoning_details(message, signature)
                logger.info(
                    "Injected reasoning_details for assistant idx=%d (sig=%s)",
                    assistant_index,
                    _signature_fingerprint(signature),
                )
            assistant_index += 1

        completion_args = {
            "model": effective_model,
            "messages": messages,
            "tools": tools,
            "response_format": response_format,
        }
        completion_args.update(self._additional_args)
        if generation_params:
            completion_args.update(generation_params)

        if stream:
            completion_args["stream"] = True
            response_stream = await self.llm_client.acompletion(**completion_args)
            accumulated_text = ""
            tool_calls_accumulator: dict[int, dict[str, Any]] = {}
            finish_reason = None
            usage_payload = None
            captured_signature = None

            async for chunk in response_stream:
                chunk_signature = _extract_reasoning_details(chunk)
                if chunk_signature is not None:
                    captured_signature = chunk_signature

                usage_payload = _get_attr(chunk, "usage") or usage_payload
                choices = _get_attr(chunk, "choices") or []
                if not choices:
                    continue

                choice = choices[0]
                finish_reason = _get_attr(choice, "finish_reason") or finish_reason
                delta = _get_attr(choice, "delta") or _get_attr(choice, "message")
                if not delta:
                    continue

                reasoning_text = _extract_reasoning_text(delta)
                if reasoning_text:
                    for text in reasoning_text:
                        yield LlmResponse(
                            content=types.Content(
                                role="model",
                                parts=[types.Part(text=text, thought=True)],
                            ),
                            partial=True,
                            model_version=effective_model,
                        )

                delta_text = _extract_delta_text(delta)
                if delta_text:
                    accumulated_text += delta_text
                    yield LlmResponse(
                        content=types.Content(
                            role="model",
                            parts=[types.Part(text=delta_text)],
                        ),
                        partial=True,
                        model_version=effective_model,
                    )

                tool_call_deltas = _extract_tool_call_deltas(delta)
                for tool_delta in tool_call_deltas:
                    _merge_tool_call_delta(tool_calls_accumulator, tool_delta)

            final_tool_calls = _build_tool_calls(tool_calls_accumulator)
            if accumulated_text or final_tool_calls:
                final_response = _build_final_response(
                    effective_model,
                    accumulated_text,
                    final_tool_calls,
                    finish_reason,
                    usage_payload,
                )
                llm_response = _model_response_to_generate_content_response(final_response)

                if captured_signature is not None:
                    signature_bytes = _serialize_signature(captured_signature)
                    attached_index = _attach_signature_to_response(
                        llm_response, signature_bytes
                    )
                    if attached_index is not None:
                        logger.info(
                            "Attached thought_signature to response part idx=%d",
                            attached_index,
                        )
                    logger.info(
                        "Captured reasoning_details (sig=%s)",
                        _signature_fingerprint(captured_signature),
                    )
                else:
                    logger.debug(
                        "No reasoning_details found in OpenRouter response stream."
                    )

                yield llm_response
            return

        response = await self.llm_client.acompletion(**completion_args)
        llm_response = _model_response_to_generate_content_response(response)

        new_signature = _extract_reasoning_details(response)
        if new_signature is not None:
            signature_bytes = _serialize_signature(new_signature)
            attached_index = _attach_signature_to_response(
                llm_response, signature_bytes
            )
            if attached_index is not None:
                logger.info(
                    "Attached thought_signature to response part idx=%d",
                    attached_index,
                )
            logger.info(
                "Captured reasoning_details (sig=%s)",
                _signature_fingerprint(new_signature),
            )
        else:
            logger.debug("No reasoning_details found in OpenRouter response.")

        yield llm_response


def _assistant_signatures_from_contents(
    contents: Iterable[types.Content],
) -> list[Optional[bytes]]:
    signatures: list[Optional[bytes]] = []
    for content in contents or []:
        if content.role not in ("model", "assistant"):
            continue
        signature = None
        for part in content.parts or []:
            if part.thought_signature is not None:
                signature = part.thought_signature
                break
        signatures.append(signature)
    return signatures


def _message_role(message: Any) -> Optional[str]:
    if isinstance(message, dict):
        return message.get("role")
    return getattr(message, "role", None)


def _get_attr(obj: Any, name: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(name)
    return getattr(obj, name, None)


def _extract_delta_text(delta: Any) -> Optional[str]:
    if isinstance(delta, dict):
        text = delta.get("content") or delta.get("text")
    else:
        text = getattr(delta, "content", None) or getattr(delta, "text", None)
    if isinstance(text, list):
        return "".join(str(item) for item in text if item is not None)
    if text is not None:
        return str(text)
    return None


def _extract_reasoning_text(delta: Any) -> list[str]:
    candidates = []
    if isinstance(delta, dict):
        candidates.append(delta.get("reasoning"))
        candidates.append(delta.get("reasoning_content"))
        candidates.append(delta.get("thought"))
    else:
        candidates.append(getattr(delta, "reasoning", None))
        candidates.append(getattr(delta, "reasoning_content", None))
        candidates.append(getattr(delta, "thought", None))

    results = []
    for item in candidates:
        if not item or isinstance(item, bool):
            continue
        if isinstance(item, list):
            results.extend(
                [str(part) for part in item if part and not isinstance(part, bool)]
            )
        else:
            results.append(str(item))
    return results


def _extract_tool_call_deltas(delta: Any) -> list[dict[str, Any]]:
    if isinstance(delta, dict):
        tool_calls = delta.get("tool_calls")
        function_call = delta.get("function_call")
    else:
        tool_calls = getattr(delta, "tool_calls", None)
        function_call = getattr(delta, "function_call", None)

    if tool_calls:
        return list(tool_calls)

    if function_call:
        return [{
            "index": 0,
            "id": function_call.get("id") if isinstance(function_call, dict) else getattr(function_call, "id", None),
            "type": "function",
            "function": function_call,
        }]

    return []


def _merge_tool_call_delta(
    accumulator: dict[int, dict[str, Any]],
    delta: dict[str, Any],
) -> None:
    index = delta.get("index", 0) or 0
    existing = accumulator.get(index, {
        "id": delta.get("id"),
        "type": delta.get("type", "function"),
        "function": {"name": "", "arguments": ""},
    })

    if delta.get("id"):
        existing["id"] = delta["id"]
    if delta.get("type"):
        existing["type"] = delta["type"]

    func_delta = delta.get("function") or {}
    if isinstance(func_delta, dict):
        name = func_delta.get("name")
        arguments = func_delta.get("arguments")
    else:
        name = getattr(func_delta, "name", None)
        arguments = getattr(func_delta, "arguments", None)
        if arguments is None:
            arguments = getattr(func_delta, "args", None)

    if name:
        existing["function"]["name"] = name
    if arguments:
        if isinstance(arguments, dict):
            arguments = json.dumps(arguments, ensure_ascii=False)
        existing["function"]["arguments"] += str(arguments)

    accumulator[index] = existing


def _build_tool_calls(
    accumulator: dict[int, dict[str, Any]]
) -> list[ChatCompletionMessageToolCall]:
    if not accumulator:
        return []
    tool_calls: list[ChatCompletionMessageToolCall] = []
    for idx in sorted(accumulator.keys()):
        data = accumulator[idx]
        func = data.get("function") or {}
        if not isinstance(func, dict):
            func = {}
        tool_calls.append(
            ChatCompletionMessageToolCall(
                id=data.get("id"),
                type=data.get("type") or "function",
                function=Function(
                    name=func.get("name"),
                    arguments=func.get("arguments"),
                ),
            )
        )
    return tool_calls


class _ResponseShim(dict):
    """Dict-like response with attribute access for LiteLLM converters."""

    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__(payload)
        self.model = payload.get("model")
        self.choices = payload.get("choices")
        self.usage = payload.get("usage")


def _build_final_response(
    model: str,
    content: str,
    tool_calls: list[ChatCompletionMessageToolCall],
    finish_reason: Optional[str],
    usage: Optional[Any],
) -> _ResponseShim:
    message: dict[str, Any] = {
        "role": "assistant",
        "content": content or "",
    }
    if tool_calls:
        message["tool_calls"] = tool_calls

    payload: dict[str, Any] = {
        "choices": [{
            "message": message,
            "finish_reason": finish_reason or "stop",
        }],
        "model": model,
    }
    if usage:
        payload["usage"] = usage
    return _ResponseShim(payload)


def _set_reasoning_details(message: Any, signature: Any) -> None:
    if isinstance(message, dict):
        message["reasoning_details"] = signature
    else:
        setattr(message, "reasoning_details", signature)


def _serialize_signature(signature: Any) -> Optional[bytes]:
    if signature is None:
        return None
    if isinstance(signature, (bytes, bytearray)):
        return bytes(signature)
    if isinstance(signature, str):
        return signature.encode("utf-8")
    try:
        return json.dumps(signature, ensure_ascii=False).encode("utf-8")
    except (TypeError, ValueError):
        return str(signature).encode("utf-8")


def _deserialize_signature(signature_raw: Any) -> Optional[Any]:
    if signature_raw is None:
        return None
    if isinstance(signature_raw, (bytes, bytearray)):
        try:
            text = signature_raw.decode("utf-8")
        except UnicodeDecodeError:
            return signature_raw.decode("utf-8", errors="replace")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text
    return signature_raw


def _extract_reasoning_details(response: Any) -> Optional[Any]:
    choices = None
    if hasattr(response, "get"):
        choices = response.get("choices")
    if choices is None:
        choices = getattr(response, "choices", None)
    if not choices:
        return None

    choice = choices[0]
    message = None
    if isinstance(choice, dict):
        message = choice.get("message")
    else:
        message = getattr(choice, "message", None)
    if message is None:
        return None

    value = None
    if isinstance(message, dict):
        value = message.get("reasoning_details")
    else:
        value = getattr(message, "reasoning_details", None)

    if value is None:
        provider_fields = getattr(message, "provider_specific_fields", None)
        if isinstance(provider_fields, dict):
            value = provider_fields.get("reasoning_details")

    if value is None:
        provider_fields = getattr(choice, "provider_specific_fields", None)
        if isinstance(provider_fields, dict):
            value = provider_fields.get("reasoning_details")

    return value


def _attach_signature_to_response(
    llm_response: LlmResponse, signature: Optional[bytes]
) -> Optional[int]:
    if not signature or not llm_response.content:
        return None
    parts = llm_response.content.parts or []
    for idx, part in enumerate(parts):
        if part.function_call is not None:
            part.thought_signature = signature
            return idx
    if parts:
        parts[0].thought_signature = signature
        return 0
    return None


def _signature_fingerprint(value: Any) -> str:
    try:
        payload = json.dumps(value, ensure_ascii=False, sort_keys=True).encode(
            "utf-8"
        )
    except (TypeError, ValueError):
        payload = str(value).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:12]


def _signature_cache_summary(signatures: list[Optional[bytes]]) -> list[str]:
    summary = []
    for signature in signatures:
        if signature is None:
            summary.append("none")
        else:
            summary.append(_signature_fingerprint(_deserialize_signature(signature)))
    return summary
