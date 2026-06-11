"""
Model pricing table and cost calculation utilities.

Prices are per 1M tokens in USD, sourced from OpenRouter / provider pricing pages.
Update this table when adding new models or when prices change.
"""

from __future__ import annotations

from typing import Any

# ── Model → price mapping (USD per 1M tokens) ──────────────────────────
# Format: "model_id": (input_price, output_price)
# Use the OpenRouter model ID as key.
MODEL_PRICES: dict[str, tuple[float, float]] = {
    # Anthropic
    "anthropic/claude-sonnet-4": (3.0, 15.0),
    "anthropic/claude-3.5-sonnet": (3.0, 15.0),
    "anthropic/claude-3.5-sonnet:beta": (3.0, 15.0),
    "anthropic/claude-3.7-sonnet": (3.0, 15.0),
    "anthropic/claude-3.7-sonnet:beta": (3.0, 15.0),
    "anthropic/claude-4-opus": (15.0, 75.0),
    "anthropic/claude-3-haiku": (0.25, 1.25),
    # OpenAI
    "openai/gpt-4o": (2.5, 10.0),
    "openai/gpt-4o-mini": (0.15, 0.6),
    "openai/gpt-4.1": (2.0, 8.0),
    "openai/gpt-4.1-mini": (0.4, 1.6),
    "openai/gpt-4.1-nano": (0.1, 0.4),
    "openai/o3": (10.0, 40.0),
    "openai/o3-mini": (1.1, 4.4),
    "openai/o4-mini": (1.1, 4.4),
    # Google
    "google/gemini-2.5-pro-preview": (1.25, 10.0),
    "google/gemini-2.5-flash-preview": (0.15, 0.6),
    "google/gemini-3-flash-preview": (0.15, 0.6),
    # DeepSeek
    "deepseek/deepseek-chat-v3-0324": (0.27, 1.1),
    "deepseek/deepseek-r1": (0.55, 2.19),
    # Qwen
    "qwen/qwen3-235b-a22b": (0.2, 0.6),
    "qwen/qwen3-30b-a3b": (0.1, 0.3),
    # GLM
    "z-ai/glm-4.7": (0.1, 0.3),
    "z-ai/glm-5.1": (0.1, 0.3),
    # Moonshot
    "moonshotai/kimi-k2": (0.6, 2.0),
}

# Fallback price when model is not in the table
_FALLBACK_PRICE: tuple[float, float] = (1.0, 3.0)


def get_model_price(model_name: str) -> tuple[float, float]:
    """Return (input_price_per_1M, output_price_per_1M) for a model."""
    if model_name in MODEL_PRICES:
        return MODEL_PRICES[model_name]
    # Try without provider prefix
    for key, price in MODEL_PRICES.items():
        if key.endswith(f"/{model_name}") or model_name.endswith(key.split("/")[-1]):
            return price
    return _FALLBACK_PRICE


def calculate_cost(
    model_name: str,
    input_tokens: int,
    output_tokens: int,
) -> float:
    """Calculate estimated cost in USD for a single LLM call."""
    input_price, output_price = get_model_price(model_name)
    return (input_tokens * input_price + output_tokens * output_price) / 1_000_000


def calculate_run_cost(generations: list[dict[str, Any]]) -> float:
    """
    Calculate total cost for an entire agent run.

    Each item in `generations` should have:
      - model: str
      - input_tokens: int
      - output_tokens: int
    """
    total = 0.0
    for gen in generations:
        total += calculate_cost(
            gen.get("model", ""),
            gen.get("input_tokens", 0),
            gen.get("output_tokens", 0),
        )
    return round(total, 6)
