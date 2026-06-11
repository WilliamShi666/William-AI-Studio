"""
Patch LiteLLM's convert_to_gemini_tool_call_invoke for Gemini 3 Pro thought_signature.

Gemini 3 Pro requires functionCall parts to carry a thought_signature for
multi-turn tool calls. LiteLLM does not populate this field, which triggers
INVALID_ARGUMENT errors like:
    "Function call is missing a thought_signature in functionCall parts."

CRITICAL IMPLEMENTATION NOTE:
============================
The patch MUST be applied at module load time, BEFORE transformation.py is imported!

Why? Python's `from X import Y` mechanism:
- Creates a local binding in the importing module's namespace
- This binding points to X.Y's value AT IMPORT TIME
- Later modifications to X.Y do NOT affect existing local bindings!

transformation.py does:
    from factory import convert_to_gemini_tool_call_invoke
    # ... later uses convert_to_gemini_tool_call_invoke directly

If we patch factory AFTER transformation is loaded, transformation's local binding
still points to the ORIGINAL function! This is why previous patches failed.

Solution: Patch factory BEFORE importing transformation.
"""
import base64
import logging
import os
from typing import List, Optional, Tuple, Any

logger = logging.getLogger(__name__)

# =============================================================================
# Step 1: Import factory ONLY (do NOT import transformation yet!)
# =============================================================================
from litellm.litellm_core_utils.prompt_templates import factory as _factory
from litellm.types.llms.openai import ChatCompletionAssistantMessage
from litellm.types.llms.vertex_ai import PartType as VertexPartType, ContentType


# =============================================================================
# Step 2: Define the patched function
# =============================================================================
_DUMMY_SIG = "skip_thought_signature_validator"


def _encode_sig(raw: Optional[str]) -> str:
    """
    Produce a thoughtSignature value.

    Default: use Google's documented bypass token to skip validation when we
    don't have the real signature from the model. This avoids "corrupted
    thought signature" errors when LiteLLM/ADK strips provider fields.

    Opt-in env override:
        GEMINI_THOUGHT_SIGNATURE_MODE=fake -> base64(id/name/random)
    """
    mode = os.getenv("GEMINI_THOUGHT_SIGNATURE_MODE", "skip").lower()
    if mode == "skip":
        return _DUMMY_SIG

    token = (raw or "").strip()
    if not token:
        token = base64.b64encode(os.urandom(16)).decode("utf-8")
        return token
    return base64.b64encode(token.encode("utf-8")).decode("utf-8")


def _patched_convert_to_gemini_tool_call_invoke(
    message: ChatCompletionAssistantMessage,
) -> List[VertexPartType]:
    """
    Patched version that attaches thoughtSignature to every functionCall part.

    This ensures Gemini 3 Pro multi-turn tool calls don't fail with
    "Function call is missing a thought_signature" errors.
    """
    try:
        parts: List[VertexPartType] = []
        tool_calls = message.get("tool_calls", None)
        function_call = message.get("function_call", None)

        if tool_calls is not None:
            for tool in tool_calls:
                func_params = tool.get("function")
                if not func_params:
                    raise Exception(
                        f"function_call missing. Received tool call without function: {tool}"
                    )

                gemini_function_call = _factory._gemini_tool_call_invoke_helper(
                    function_call_params=func_params
                )

                if gemini_function_call is None:
                    raise Exception(
                        f"function_call missing. Received tool call with 'type': 'function'. "
                        f"No function call in argument - {tool}"
                    )

                # Generate signature from tool call ID or function name
                sig = _encode_sig(tool.get("id") or func_params.get("name"))

                # Create VertexPartType with thoughtSignature attached
                parts.append(
                    VertexPartType(
                        function_call=gemini_function_call,
                        thoughtSignature=sig
                    )
                )
                logger.debug(f"[Gemini Patch] Attached thoughtSignature: {sig[:20]}...")

        elif function_call is not None:
            gemini_function_call = _factory._gemini_tool_call_invoke_helper(
                function_call_params=function_call
            )

            if gemini_function_call is None:
                raise Exception(
                    f"function_call missing. Received tool call with 'type': 'function'. "
                    f"No function call in argument - {message}"
                )

            sig = _encode_sig(function_call.get("id") or function_call.get("name"))
            parts.append(
                VertexPartType(
                    function_call=gemini_function_call,
                    thoughtSignature=sig
                )
            )
            logger.debug(f"[Gemini Patch] Attached thoughtSignature for function_call: {function_call.get('name')}")

        return parts

    except Exception as e:
        raise Exception(
            f"Unable to convert openai tool calls={message} to gemini tool calls. "
            f"Received error={str(e)}"
        )


# =============================================================================
# Step 3: IMMEDIATELY patch factory at module load time
# This MUST happen BEFORE transformation.py is imported!
# =============================================================================
_original_func = _factory.convert_to_gemini_tool_call_invoke
_factory.convert_to_gemini_tool_call_invoke = _patched_convert_to_gemini_tool_call_invoke
logger.info("[Gemini Patch] Patched factory.convert_to_gemini_tool_call_invoke at module load time")


# =============================================================================
# Step 4: NOW it's safe to import transformation
# It will get the PATCHED function via its "from factory import ..." statement
# =============================================================================
from litellm.llms.vertex_ai.gemini import transformation as _transformation

# Also patch transformation's module attribute for completeness
# (in case any code accesses transformation.convert_to_gemini_tool_call_invoke directly)
_transformation.convert_to_gemini_tool_call_invoke = _patched_convert_to_gemini_tool_call_invoke
logger.info("[Gemini Patch] Also patched transformation module attribute")


# =============================================================================
# Step 4.1: Add a safety net on the final Gemini request body
# =============================================================================
_original_transform_request_body = _transformation._transform_request_body


def _ensure_thought_signatures(contents: Optional[List[ContentType]]) -> Tuple[int, int, int]:
    """Ensure every function_call part carries thoughtSignature."""
    if not contents:
        return 0, 0, 0

    added = 0
    existing = 0
    total = 0

    for content in contents:
        try:
            parts = None
            if isinstance(content, dict):
                parts = content.get("parts")
            else:
                parts = getattr(content, "parts", None)
            if not parts:
                continue
            for part in parts:
                total += 1
                fc = None
                if isinstance(part, dict):
                    fc = part.get("function_call")
                    sig = part.get("thoughtSignature")
                else:
                    fc = getattr(part, "function_call", None)
                    sig = getattr(part, "thoughtSignature", None)
                if not fc:
                    continue
                if sig:
                    existing += 1
                    continue
                # PartType.function_call is a TypedDict without id; fall back to name
                name = None
                if isinstance(fc, dict):
                    name = fc.get("name") or fc.get("id")
                else:
                    name = getattr(fc, "name", None) or getattr(fc, "id", None)
                patched_sig = _encode_sig(name or "tool_call")
                if isinstance(part, dict):
                    part["thoughtSignature"] = patched_sig
                else:
                    setattr(part, "thoughtSignature", patched_sig)
                added += 1
        except Exception as e:  # pragma: no cover - defensive
            logger.warning(f"[Gemini Patch] Failed ensuring thoughtSignature on content: {e}")
            continue
    return added, existing, total


def _patched_transform_request_body(*args: Any, **kwargs: Any):
    """Wrap _transform_request_body to enforce thoughtSignature before request is sent."""
    data = _original_transform_request_body(*args, **kwargs)
    try:
        contents = None
        if isinstance(data, dict):
            contents = data.get("contents")
        else:
            contents = getattr(data, "contents", None)
        added, existing, total = _ensure_thought_signatures(contents)
        fc_summary = []
        if contents:
            for c_idx, content in enumerate(contents):
                parts = content.get("parts") if isinstance(content, dict) else getattr(content, "parts", None)
                if not parts:
                    continue
                for p_idx, part in enumerate(parts):
                    fc = part.get("function_call") if isinstance(part, dict) else getattr(part, "function_call", None)
                    if not fc:
                        continue
                    sig = part.get("thoughtSignature") if isinstance(part, dict) else getattr(part, "thoughtSignature", None)
                    name = fc.get("name") if isinstance(fc, dict) else getattr(fc, "name", None)
                    fc_summary.append(
                        f"c{c_idx}p{p_idx}:name={name},sig={'yes' if sig else 'no'}"
                    )
        logger.info(
            "[Gemini Patch] Safety-net summary: added=%s existing=%s total_parts=%s | fcs=%s",
            added, existing, total, "; ".join(fc_summary) if fc_summary else "none",
        )
    except Exception as e:  # pragma: no cover - defensive
        logger.warning(f"[Gemini Patch] Failed safety-net thoughtSignature injection: {e}")
    return data


_transformation._transform_request_body = _patched_transform_request_body
logger.info("[Gemini Patch] Wrapped transformation._transform_request_body with safety net")


# =============================================================================
# Step 4.2: One-time verification logs to confirm binding
# =============================================================================
try:
    logger.info(
        "[Gemini Patch] Verification IDs | factory: %s | transformation: %s | patched: %s",
        id(_factory.convert_to_gemini_tool_call_invoke),
        id(_transformation.convert_to_gemini_tool_call_invoke),
        id(_patched_convert_to_gemini_tool_call_invoke),
    )
except Exception:
    logger.debug("[Gemini Patch] Verification ID log failed", exc_info=True)


# =============================================================================
# Step 5: Provide apply_patch() for backward compatibility
# =============================================================================
_PATCHED = True  # Already patched at import time


def apply_patch():
    """
    No-op function for backward compatibility.

    The patch is already applied at module import time (Step 3 above).
    This function exists only for backward compatibility with code that
    explicitly calls apply_patch().
    """
    global _PATCHED
    if _PATCHED:
        logger.debug("[Gemini Patch] apply_patch() called - patch already active from module import")
        return

    # This code path should never be reached, but just in case:
    _factory.convert_to_gemini_tool_call_invoke = _patched_convert_to_gemini_tool_call_invoke
    _transformation.convert_to_gemini_tool_call_invoke = _patched_convert_to_gemini_tool_call_invoke
    _PATCHED = True
    logger.info("[Gemini Patch] Applied patch via apply_patch() call")
