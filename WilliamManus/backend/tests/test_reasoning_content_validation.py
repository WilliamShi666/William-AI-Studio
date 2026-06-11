"""Tests for reasoning_content continuity validation hook."""

import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from agentscope_integration.models.openrouter_model import OpenRouterChatModel


def _make_mock_model(thinking_enabled: bool = True):
    """Create a minimal OpenRouterChatModel for testing validation."""
    model = OpenRouterChatModel.__new__(OpenRouterChatModel)
    model._configured_base_url = "https://api.deepseek.com"
    model.stream = False
    model.model_name = "deepseek-v4-pro"
    if thinking_enabled:
        model.generate_kwargs = {
            "extra_body": {"thinking": {"type": "enabled"}},
            "reasoning_effort": "max",
        }
    else:
        model.generate_kwargs = {}
    return model


class TestReasoningContentValidation:
    def test_validation_skips_when_thinking_disabled(self) -> None:
        """No validation when thinking mode is off."""
        model = _make_mock_model(thinking_enabled=False)
        messages = [
            {
                "role": "assistant",
                "content": "Here is the answer.",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "search", "arguments": "{}"},
                    },
                ],
            },
        ]
        # Should not raise, not log warnings for non-thinking mode
        model._validate_reasoning_content_continuity(messages)

    def test_validation_passes_when_reasoning_content_present(self) -> None:
        """No warning when all assistant tool-call messages have reasoning_content."""
        model = _make_mock_model(thinking_enabled=True)
        messages = [
            {"role": "user", "content": "Hello"},
            {
                "role": "assistant",
                "content": "Let me check.",
                "reasoning_content": "I should look this up.",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "search", "arguments": "{}"},
                    },
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "content": "result",
            },
            {
                "role": "assistant",
                "content": "Here is the answer.",
                "reasoning_content": "Based on the result, the answer is clear.",
            },
        ]
        model._validate_reasoning_content_continuity(messages)

    def test_validation_logs_warning_on_missing_reasoning(self) -> None:
        """Method must handle missing reasoning_content without raising."""
        model = _make_mock_model(thinking_enabled=True)
        messages = [
            {"role": "user", "content": "Hello"},
            {
                "role": "assistant",
                "content": "Let me check.",
                # MISSING reasoning_content — must not raise
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "search", "arguments": "{}"},
                    },
                ],
            },
        ]
        # Must not raise even when reasoning_content is missing
        model._validate_reasoning_content_continuity(messages)
