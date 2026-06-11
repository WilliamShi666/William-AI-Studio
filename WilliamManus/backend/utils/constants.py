# Master model configuration - single source of truth
MODELS = {
    "anthropic/claude-sonnet-4-20250514": {
        "aliases": ["claude-sonnet-4"],
        "pricing": {
            "input_cost_per_million_tokens": 3.00,
            "output_cost_per_million_tokens": 15.00
        },
        "tier_availability": ["free", "paid"]
    },
    "openrouter/anthropic/claude-sonnet-4.5": {
        "aliases": ["anthropic/claude-sonnet-4.5", "claude-sonnet-4.5"],
        "pricing": {
            "input_cost_per_million_tokens": 3.00,
            "output_cost_per_million_tokens": 15.00
        },
        "tier_availability": ["free", "paid"]
    },
    "openrouter/deepseek/deepseek-chat": {
        "aliases": ["openrouter-deepseek"],
        "pricing": {
            "input_cost_per_million_tokens": 0.14,
            "output_cost_per_million_tokens": 0.28
        },
        "tier_availability": ["free", "paid"]
    },
    "deepseek/deepseek-chat": {
        "aliases": ["deepseek", "deepseek-chat", "DeepSeek", "DeepSeek/DeepSeek-chat", "DeepSeek-chat"],
        "pricing": {
            "input_cost_per_million_tokens": 0.14,
            "output_cost_per_million_tokens": 0.28
        },
        "tier_availability": ["free", "paid"]
    },
    "gemini/gemini-3.1-pro-preview": {
        "aliases": ["gemini-3.1-pro-preview", "google/gemini-3.1-pro-preview"],
        "pricing": {
            "input_cost_per_million_tokens": 1.25,
            "output_cost_per_million_tokens": 10.00
        },
        "tier_availability": ["free", "paid"]
    },
    "openrouter/google/gemini-3.1-pro-preview": {
        "aliases": ["openrouter/google/gemini-3.1-pro-preview"],
        "pricing": {
            "input_cost_per_million_tokens": 1.25,
            "output_cost_per_million_tokens": 10.00
        },
        "tier_availability": ["free", "paid"]
    },
    "openrouter/google/gemini-3-flash-preview": {
        "aliases": ["openrouter/google/gemini-3-flash-preview"],
        "pricing": {
            "input_cost_per_million_tokens": 0.15,
            "output_cost_per_million_tokens": 0.60
        },
        "tier_availability": ["free", "paid"]
    },
    # Qwen 3.6 Plus is OpenRouter-only for now; keep pricing aligned to Qwen 3.5 Plus
    # until provider-specific billing is updated.
    "openrouter/qwen/qwen3.6-plus": {
        "aliases": [
            "openrouter/qwen/qwen3.6-plus",
            "qwen/qwen3.6-plus",
            "qwen3.6-plus",
        ],
        "pricing": {
            "input_cost_per_million_tokens": 0.13,
            "output_cost_per_million_tokens": 0.60
        },
        "tier_availability": ["free", "paid"]
    },
    # Qwen 3.5 Plus pricing currently follows the previous Qwen baseline until official update.
    "dashscope/qwen3.5-plus": {
        "aliases": [
            "dashscope/qwen3.5-plus",
            "qwen/qwen3.5-plus",
            "qwen3.5-plus",
            "openrouter/qwen/qwen3.5-plus",
            # Legacy aliases kept for backward compatibility.
            "dashscope/qwen3.5-397b-a17b",
            "qwen/qwen3.5-397b-a17b",
            "qwen3.5-397b-a17b",
            "openrouter/qwen/qwen3.5-397b-a17b",
        ],
        "pricing": {
            "input_cost_per_million_tokens": 0.13,
            "output_cost_per_million_tokens": 0.60
        },
        "tier_availability": ["free", "paid"]
    },
    # "openrouter/google/gemini-2.5-flash-preview-05-20": {
    #     "aliases": ["gemini-flash-2.5"],
    #     "pricing": {
    #         "input_cost_per_million_tokens": 0.15,
    #         "output_cost_per_million_tokens": 0.60
    #     },
    #     "tier_availability": ["free", "paid"]
    # },
    # "openrouter/deepseek/deepseek-chat-v3-0324": {
    #     "aliases": ["deepseek/deepseek-chat-v3-0324"],
    #     "pricing": {
    #         "input_cost_per_million_tokens": 0.38,
    #         "output_cost_per_million_tokens": 0.89
    #     },
    #     "tier_availability": ["free", "paid"]
    # },
    "openrouter/moonshotai/kimi-k2": {
        "aliases": ["moonshotai/kimi-k2"],
        "pricing": {
            "input_cost_per_million_tokens": 1.00,
            "output_cost_per_million_tokens": 3.00
        },
        "tier_availability": ["free", "paid"]
    },
    "openrouter/minimax/minimax-m2.1": {
        "aliases": ["openrouter/minimax/minimax-m2.1", "minimax/minimax-m2.1"],
        "pricing": {
            "input_cost_per_million_tokens": 1.00,
            "output_cost_per_million_tokens": 3.00
        },
        "tier_availability": ["free", "paid"]
    },
    "openrouter/minimax/minimax-m2.5": {
        "aliases": [
            "openrouter/minimax/minimax-m2.5",
            "minimax/minimax-m2.5",
        ],
        "pricing": {
            "input_cost_per_million_tokens": 1.00,
            "output_cost_per_million_tokens": 3.00
        },
        "tier_availability": ["free", "paid"]
    },
    "openrouter/minimax/minimax-m2.7": {
        "aliases": [
            "openrouter/minimax/minimax-m2.7",
            "minimax/minimax-m2.7",
        ],
        "pricing": {
            "input_cost_per_million_tokens": 0.00,
            "output_cost_per_million_tokens": 0.00
        },
        "tier_availability": ["free", "paid"]
    },
    "openrouter/xiaomi/mimo-v2-pro": {
        "aliases": [
            "openrouter/xiaomi/mimo-v2-pro",
            "xiaomi/mimo-v2-pro",
        ],
        "pricing": {
            "input_cost_per_million_tokens": 0.00,
            "output_cost_per_million_tokens": 0.00
        },
        "tier_availability": ["free", "paid"]
    },
    "openrouter/xiaomi/mimo-v2.5-pro": {
        "aliases": [
            "openrouter/xiaomi/mimo-v2.5-pro",
            "xiaomi/mimo-v2.5-pro",
            "mimo-v2.5-pro",
        ],
        "pricing": {
            "input_cost_per_million_tokens": 0.00,
            "output_cost_per_million_tokens": 0.00
        },
        "tier_availability": ["free", "paid"]
    },
    "dashscope/minimax-m2.5": {
        "aliases": [
            "dashscope/minimax-m2.5",
            "minimax-m2.5",
            "MiniMax-M2.5",
        ],
        "pricing": {
            "input_cost_per_million_tokens": 1.00,
            "output_cost_per_million_tokens": 3.00
        },
        "tier_availability": ["free", "paid"]
    },
    "openrouter/moonshotai/kimi-k2.5": {
        "aliases": ["openrouter/moonshotai/kimi-k2.5"],
        "pricing": {
            "input_cost_per_million_tokens": 4.00,
            "output_cost_per_million_tokens": 21.00
        },
        "tier_availability": ["free", "paid"]
    },
    "openrouter/moonshotai/kimi-k2.6": {
        "aliases": [
            "openrouter/moonshotai/kimi-k2.6",
            "moonshotai/kimi-k2.6-openrouter",
            "openrouter-kimi-k2.6",
        ],
        "pricing": {
            "input_cost_per_million_tokens": 0.00,
            "output_cost_per_million_tokens": 0.00
        },
        "tier_availability": ["free", "paid"]
    },
    "ppio/moonshotai/kimi-k2.5": {
        "aliases": ["ppio/moonshotai/kimi-k2.5"],
        "pricing": {
            "input_cost_per_million_tokens": 4.00,
            "output_cost_per_million_tokens": 21.00
        },
        "tier_availability": ["free", "paid"]
    },
    "kimi-k2.5": {
        "aliases": [
            "kimi-k2.5",
            "moonshot/kimi-k2.5",
            "moonshotai/kimi-k2.5",
        ],
        "pricing": {
            "input_cost_per_million_tokens": 4.00,
            "output_cost_per_million_tokens": 21.00
        },
        "tier_availability": ["free", "paid"]
    },
    "kimi-k2.6": {
        "aliases": [
            "kimi-k2.6",
            "moonshot/kimi-k2.6",
            "moonshotai/kimi-k2.6",
        ],
        "pricing": {
            "input_cost_per_million_tokens": 0.00,
            "output_cost_per_million_tokens": 0.00
        },
        "tier_availability": ["free", "paid"]
    },
    "openrouter/deepseek/deepseek-v4-pro": {
        "aliases": [
            "openrouter/deepseek/deepseek-v4-pro",
            "deepseek/deepseek-v4-pro-openrouter",
            "openrouter-deepseek-v4-pro",
        ],
        "pricing": {
            "input_cost_per_million_tokens": 0.00,
            "output_cost_per_million_tokens": 0.00
        },
        "tier_availability": ["free", "paid"]
    },
    "openrouter/deepseek/deepseek-v4-flash": {
        "aliases": [
            "openrouter/deepseek/deepseek-v4-flash",
            "deepseek/deepseek-v4-flash-openrouter",
            "openrouter-deepseek-v4-flash",
        ],
        "pricing": {
            "input_cost_per_million_tokens": 0.00,
            "output_cost_per_million_tokens": 0.00
        },
        "tier_availability": ["free", "paid"]
    },
    "deepseek-v4-pro-high": {
        "aliases": [
            "deepseek-v4-pro-high",
            "deepseek/deepseek-v4-pro",
            "deepseek/deepseek-v4-pro:high",
            "deepseek/deepseek-v4-pro-high",
        ],
        "pricing": {
            "input_cost_per_million_tokens": 0.00,
            "output_cost_per_million_tokens": 0.00
        },
        "tier_availability": ["free", "paid"]
    },
    "deepseek-v4-pro-max": {
        "aliases": [
            "deepseek-v4-pro-max",
            "deepseek/deepseek-v4-pro:max",
            "deepseek/deepseek-v4-pro-max",
        ],
        "pricing": {
            "input_cost_per_million_tokens": 0.00,
            "output_cost_per_million_tokens": 0.00
        },
        "tier_availability": ["free", "paid"]
    },
    "deepseek-v4-flash-high": {
        "aliases": [
            "deepseek-v4-flash-high",
            "deepseek/deepseek-v4-flash",
            "deepseek/deepseek-v4-flash:high",
            "deepseek/deepseek-v4-flash-high",
        ],
        "pricing": {
            "input_cost_per_million_tokens": 0.00,
            "output_cost_per_million_tokens": 0.00
        },
        "tier_availability": ["free", "paid"]
    },
    "deepseek-v4-flash-max": {
        "aliases": [
            "deepseek-v4-flash-max",
            "deepseek/deepseek-v4-flash:max",
            "deepseek/deepseek-v4-flash-max",
        ],
        "pricing": {
            "input_cost_per_million_tokens": 0.00,
            "output_cost_per_million_tokens": 0.00
        },
        "tier_availability": ["free", "paid"]
    },
    "openrouter/z-ai/glm-4.7": {
        "aliases": ["glm-4.7", "z-ai/glm-4.7", "openrouter/z-ai/glm-4.7"],
        "pricing": {
            "input_cost_per_million_tokens": 0.40,
            "output_cost_per_million_tokens": 1.50
        },
        "tier_availability": ["free", "paid"]
    },
    "openrouter/z-ai/glm-5": {
        "aliases": ["glm-5", "z-ai/glm-5", "openrouter/z-ai/glm-5"],
        "pricing": {
            "input_cost_per_million_tokens": 0.40,
            "output_cost_per_million_tokens": 1.50
        },
        "tier_availability": ["free", "paid"]
    },
    "openrouter/z-ai/glm-5.1": {
        "aliases": ["glm-5.1", "z-ai/glm-5.1", "openrouter/z-ai/glm-5.1"],
        "pricing": {
            "input_cost_per_million_tokens": 0.40,
            "output_cost_per_million_tokens": 1.50
        },
        "tier_availability": ["free", "paid"]
    },
    "volcengine/doubao-seed-2-0-pro-260215": {
        "aliases": [
            "volcengine/doubao-seed-2-0-pro-260215",
            "doubao-seed-2-0-pro-260215",
        ],
        "pricing": {
            "input_cost_per_million_tokens": 1.00,
            "output_cost_per_million_tokens": 3.00
        },
        "tier_availability": ["free", "paid"]
    },
    "volcengine/doubao-seed-2-0-code-preview-260215": {
        "aliases": [
            "volcengine/doubao-seed-2-0-code-preview-260215",
            "doubao-seed-2-0-code-preview-260215",
        ],
        "pricing": {
            "input_cost_per_million_tokens": 1.00,
            "output_cost_per_million_tokens": 3.00
        },
        "tier_availability": ["free", "paid"]
    },
    "xai/grok-4": {
        "aliases": ["grok-4", "x-ai/grok-4"],
        "pricing": {
            "input_cost_per_million_tokens": 5.00,
            "output_cost_per_million_tokens": 15.00
        },
        "tier_availability": ["paid"]
    },

    # Paid tier only models
    "gemini/gemini-2.5-pro": {
        "aliases": ["google/gemini-2.5-pro"],
        "pricing": {
            "input_cost_per_million_tokens": 1.25,
            "output_cost_per_million_tokens": 10.00
        },
        "tier_availability": ["paid"]
    },
    "openai/gpt-4o": {
        "aliases": ["gpt-4o"],
        "pricing": {
            "input_cost_per_million_tokens": 2.50,
            "output_cost_per_million_tokens": 10.00
        },
        "tier_availability": ["paid"]
    },
    "openai/gpt-4o-mini": {
        "aliases": ["gpt-4o-mini"],
        "pricing": {
            "input_cost_per_million_tokens": 0.15,
            "output_cost_per_million_tokens": 0.60
        },
        "tier_availability": ["free", "paid"]
    },
    "openai/gpt-4.1": {
        "aliases": ["gpt-4.1"],
        "pricing": {
            "input_cost_per_million_tokens": 15.00,
            "output_cost_per_million_tokens": 60.00
        },
        "tier_availability": ["paid"]
    },
    "openai/gpt-5": {
        "aliases": ["gpt-5"],
        "pricing": {
            "input_cost_per_million_tokens": 10.00,
            "output_cost_per_million_tokens": 30.00
        },
        "tier_availability": ["paid"]
    },
    "openai/gpt-5-mini": {
        "aliases": ["gpt-5-mini"],
        "pricing": {
            "input_cost_per_million_tokens": 0.25,
            "output_cost_per_million_tokens": 2.00
        },
        "tier_availability": ["paid"]
    },
    "openai-proxy/gpt-5.4": {
        "aliases": ["mystery-model"],
        "pricing": {
            "input_cost_per_million_tokens": 0.00,
            "output_cost_per_million_tokens": 0.00
        },
        "tier_availability": ["free", "paid"]
    },
    "openai/gpt-4.1": {
        "aliases": ["gpt-4.1"],
        "pricing": {
            "input_cost_per_million_tokens": 2.00,
            "output_cost_per_million_tokens": 8.00
        },
        "tier_availability": ["paid"]
    },
    "openai/gpt-4.1-mini": {
        "aliases": ["gpt-4.1-mini"],
        "pricing": {
            "input_cost_per_million_tokens": 0.40,
            "output_cost_per_million_tokens": 1.60
        },
        "tier_availability": ["paid"]
    },
    "anthropic/claude-3-7-sonnet-latest": {
        "aliases": ["sonnet-3.7"],
        "pricing": {
            "input_cost_per_million_tokens": 3.00,
            "output_cost_per_million_tokens": 15.00
        },
        "tier_availability": ["paid"]
    },
    "anthropic/claude-3-5-sonnet-latest": {
        "aliases": ["sonnet-3.5"],
        "pricing": {
            "input_cost_per_million_tokens": 3.00,
            "output_cost_per_million_tokens": 15.00
        },
        "tier_availability": ["paid"]
    },
}


# Derived structures (auto-generated from MODELS)
def _generate_model_structures():
    """Generate all model structures from the master MODELS dictionary."""

    # Generate tier lists
    free_models = []
    paid_models = []

    # Generate aliases
    aliases = {}

    # Generate pricing
    pricing = {}

    for model_name, config in MODELS.items():
        # Add to tier lists
        if "free" in config["tier_availability"]:
            free_models.append(model_name)
        if "paid" in config["tier_availability"]:
            paid_models.append(model_name)

        # Add aliases
        for alias in config["aliases"]:
            aliases[alias] = model_name

        # Add pricing
        pricing[model_name] = config["pricing"]

        # Also add pricing for legacy model name variations
        if model_name.startswith("openrouter/deepseek/"):
            legacy_name = model_name.replace("openrouter/", "")
            pricing[legacy_name] = config["pricing"]
        elif model_name.startswith("openrouter/qwen/qwen3.6-plus"):
            pricing["qwen/qwen3.6-plus"] = config["pricing"]
            pricing["qwen3.6-plus"] = config["pricing"]
        elif model_name.startswith("dashscope/qwen3.5-plus"):
            pricing["qwen/qwen3.5-plus"] = config["pricing"]
            pricing["qwen3.5-plus"] = config["pricing"]
            pricing["openrouter/qwen/qwen3.5-plus"] = config["pricing"]
            # Legacy aliases kept billable during transparent migration.
            pricing["dashscope/qwen3.5-397b-a17b"] = config["pricing"]
            pricing["qwen/qwen3.5-397b-a17b"] = config["pricing"]
            pricing["qwen3.5-397b-a17b"] = config["pricing"]
            pricing["openrouter/qwen/qwen3.5-397b-a17b"] = config["pricing"]
        elif model_name.startswith("openrouter/z-ai/"):
            legacy_name = model_name.replace("openrouter/", "")
            pricing[legacy_name] = config["pricing"]
        elif model_name.startswith("openrouter/minimax/"):
            legacy_name = model_name.replace("openrouter/", "")
            pricing[legacy_name] = config["pricing"]
        elif model_name.startswith("openrouter/xiaomi/"):
            legacy_name = model_name.replace("openrouter/", "")
            pricing[legacy_name] = config["pricing"]
        elif model_name.startswith("dashscope/minimax-"):
            pricing["minimax-m2.5"] = config["pricing"]
            pricing["MiniMax-M2.5"] = config["pricing"]
        elif model_name.startswith("openai-proxy/"):
            legacy_name = model_name.replace("openai-proxy/", "")
            pricing[legacy_name] = config["pricing"]
        elif model_name == "kimi-k2.5":
            pricing["moonshotai/kimi-k2.5"] = config["pricing"]
        elif model_name.startswith("gemini/"):
            legacy_name = model_name.replace("gemini/", "")
            pricing[legacy_name] = config["pricing"]
        elif model_name.startswith("anthropic/"):
            # Add anthropic/claude-sonnet-4 alias for claude-sonnet-4-20250514
            if "claude-sonnet-4-20250514" in model_name:
                pricing["anthropic/claude-sonnet-4"] = config["pricing"]
        elif model_name.startswith("xai/"):
            # Add pricing for OpenRouter x-ai models
            openrouter_name = model_name.replace("xai/", "openrouter/x-ai/")
            pricing[openrouter_name] = config["pricing"]
        elif model_name.startswith("volcengine/"):
            legacy_name = model_name.replace("volcengine/", "")
            pricing[legacy_name] = config["pricing"]

    return free_models, paid_models, aliases, pricing


# Generate all structures
FREE_TIER_MODELS, PAID_TIER_MODELS, MODEL_NAME_ALIASES, HARDCODED_MODEL_PRICES = _generate_model_structures()

MODEL_ACCESS_TIERS = {
    "free": FREE_TIER_MODELS,
    "tier_2_20": PAID_TIER_MODELS,
    "tier_6_50": PAID_TIER_MODELS,
    "tier_12_100": PAID_TIER_MODELS,
    "tier_25_200": PAID_TIER_MODELS,
    "tier_50_400": PAID_TIER_MODELS,
    "tier_125_800": PAID_TIER_MODELS,
    "tier_200_1000": PAID_TIER_MODELS,
    "tier_25_170_yearly_commitment": PAID_TIER_MODELS,
    "tier_6_42_yearly_commitment": PAID_TIER_MODELS,
    "tier_12_84_yearly_commitment": PAID_TIER_MODELS,
}
