import asyncio
import hashlib
import json
import logging
import os
from typing import Any, AsyncGenerator, Iterable, Optional

from dotenv import load_dotenv
from google.adk.agents import LlmAgent
from google.adk.models.lite_llm import (
    LiteLlm,
    _append_fallback_user_content_if_missing,
    _get_completion_inputs,
    _model_response_to_generate_content_response,
    _normalize_ollama_chat_messages,
)
from google.adk.models.llm_request import LlmRequest
from google.adk.models.llm_response import LlmResponse
from google.adk.runners import InMemoryRunner
from google.adk.tools import FunctionTool
from google.genai import types

logger = logging.getLogger("GeminiOpenRouter")
_LOG_FORMAT = "%(asctime)s - %(name)s - %(levelname)s - %(message)s"


def _configure_logging() -> None:
    log_level = os.getenv("OPENROUTER_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(level=log_level, format=_LOG_FORMAT)

    log_path = os.getenv("OPENROUTER_RUN_LOG")
    if not log_path:
        return

    root_logger = logging.getLogger()
    for handler in root_logger.handlers:
        if isinstance(handler, logging.FileHandler):
            if handler.baseFilename == os.path.abspath(log_path):
                return

    file_handler = logging.FileHandler(log_path)
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(logging.Formatter(_LOG_FORMAT))
    root_logger.addHandler(file_handler)
    logger.info("Writing run log to %s", log_path)


class OpenRouterGeminiLlm(LiteLlm):
    """
    Gemini 3 wrapper for OpenRouter that preserves thought signatures.

    The signature is captured from OpenRouter's `reasoning_details` response
    and stored on the function_call Part as `thought_signature`. On subsequent
    turns, that signature is injected back into assistant messages.
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
        if stream:
            raise NotImplementedError(
                "Streaming is not supported for OpenRouterGeminiLlm. "
                "Use StreamingMode.NONE for now."
            )

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
                "Assistant signature cache: %s",
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
        logger.debug(
            "Outgoing messages: %s",
            json.dumps(
                _summarize_messages(messages),
                ensure_ascii=False,
                sort_keys=True,
            ),
        )

        completion_args = {
            "model": effective_model,
            "messages": messages,
            "tools": tools,
            "response_format": response_format,
        }
        completion_args.update(self._additional_args)
        if generation_params:
            completion_args.update(generation_params)

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
                "Captured reasoning_details for assistant idx=%d (sig=%s)",
                assistant_index,
                _signature_fingerprint(new_signature),
            )
        else:
            logger.info("No reasoning_details found in response.")

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


def _message_tool_calls(message: Any) -> list[Any]:
    if isinstance(message, dict):
        return message.get("tool_calls") or []
    return getattr(message, "tool_calls", None) or []


def _message_reasoning_details(message: Any) -> Optional[Any]:
    if isinstance(message, dict):
        return message.get("reasoning_details")
    return getattr(message, "reasoning_details", None)


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


def _summarize_messages(messages: Iterable[Any]) -> list[dict[str, Any]]:
    summary = []
    for idx, message in enumerate(messages):
        role = _message_role(message)
        tool_calls = _message_tool_calls(message)
        reasoning_details = _message_reasoning_details(message)
        summary.append(
            {
                "idx": idx,
                "role": role,
                "tool_calls": len(tool_calls),
                "reasoning_details": (
                    _signature_fingerprint(reasoning_details)
                    if reasoning_details is not None
                    else None
                ),
            }
        )
    return summary


# --- Tool Definition ---

def get_weather(city: str) -> dict[str, Any]:
    """
    Get the weather for a specific city.

    Args:
        city: The name of the city (e.g., "Tokyo", "New York").
    """
    logger.info("TOOL EXECUTION: get_weather called for %s", city)
    city_key = city.strip().lower()
    if "tokyo" in city_key:
        condition = "Clear and sunny"
        temperature_c = 25
    elif "paris" in city_key:
        condition = "Rainy"
        temperature_c = 15
    elif "new york" in city_key or "nyc" in city_key:
        condition = "Partly cloudy"
        temperature_c = 18
    else:
        condition = "Unknown"
        temperature_c = None

    return {
        "city": city,
        "condition": condition,
        "temperature_c": temperature_c,
    }


def calculator(expression: str) -> dict[str, Any]:
    """
    Evaluate a simple arithmetic expression.

    Args:
        expression: Arithmetic expression using +, -, *, /, **, parentheses.
    """
    logger.info("TOOL EXECUTION: calculator called for %s", expression)
    value = _safe_eval(expression)
    return {"expression": expression, "result": value}


def _safe_eval(expression: str) -> float:
    import ast
    import operator

    operators = {
        ast.Add: operator.add,
        ast.Sub: operator.sub,
        ast.Mult: operator.mul,
        ast.Div: operator.truediv,
        ast.Pow: operator.pow,
        ast.UAdd: operator.pos,
        ast.USub: operator.neg,
    }

    def _eval(node: ast.AST) -> float:
        if isinstance(node, ast.Expression):
            return _eval(node.body)
        if isinstance(node, ast.Constant) and isinstance(
            node.value, (int, float)
        ):
            return float(node.value)
        if isinstance(node, ast.UnaryOp) and type(node.op) in operators:
            return operators[type(node.op)](_eval(node.operand))
        if isinstance(node, ast.BinOp) and type(node.op) in operators:
            return operators[type(node.op)](_eval(node.left), _eval(node.right))
        raise ValueError("Unsupported expression")

    parsed = ast.parse(expression, mode="eval")
    return _eval(parsed)


def _extract_final_text(events: list[Any]) -> str:
    for event in reversed(events):
        if hasattr(event, "is_final_response") and event.is_final_response():
            parts = getattr(event, "content", None)
            if parts and parts.parts:
                return "".join(
                    part.text
                    for part in parts.parts
                    if part.text and not part.thought
                )
    return ""


# --- Main Execution Script ---

async def main() -> None:
    load_dotenv()
    _configure_logging()

    openrouter_key = os.getenv("OPENROUTER_API_KEY")
    if not openrouter_key:
        raise RuntimeError(
            "OPENROUTER_API_KEY is not set. Add it to .env or your shell."
        )
    os.environ.setdefault("OPENROUTER_API_KEY", openrouter_key)

    openrouter_base = os.getenv(
        "OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1"
    )
    model_name = os.getenv(
        "OPENROUTER_MODEL", "openrouter/google/gemini-3-flash-preview"
    )
    reasoning_effort = os.getenv("OPENROUTER_REASONING_EFFORT")

    print("Initializing ADK Agent with OpenRouter Gemini 3 wrapper...")

    gemini_model = OpenRouterGeminiLlm(
        model=model_name,
        api_base=openrouter_base,
        reasoning_effort=reasoning_effort,
    )

    weather_tool = FunctionTool(get_weather)
    calculator_tool = FunctionTool(calculator)

    agent = LlmAgent(
        name="weather_planner",
        model=gemini_model,
        tools=[weather_tool, calculator_tool],
        instruction=(
            "You are a helpful travel assistant. "
            "You MUST use tools and follow this flow:\n"
            "1) Call get_weather for ONE city at a time (no parallel calls).\n"
            "2) Wait for each tool result before calling get_weather again.\n"
            "3) After all weather tool calls return, use the calculator tool "
            "to compute the requested calculation.\n"
            "4) Only after tool calls are done, respond to the user.\n"
            "Do not do math in your head; always use calculator."
        ),
    )

    user_query = (
        "Get the weather for Tokyo, Paris, and New York. Then compute the "
        "average temperature in Celsius using the calculator tool."
    )

    print(f"\nUser: {user_query}\n")
    print("-" * 50)

    runner = InMemoryRunner(agent=agent)
    events = await runner.run_debug(user_query, verbose=True)

    print("-" * 50)
    print("\nFinal Agent Response:\n")
    print(_extract_final_text(events))


if __name__ == "__main__":
    asyncio.run(main())
