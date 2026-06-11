"""
Model Factory for AgentScope Integration

Supports:
- google/gemini-3.1-pro-preview (via OpenRouter, with reasoning)
- google/gemini-3-flash-preview (via OpenRouter, with reasoning)
- minimax/minimax-m2.1 (via OpenRouter)
- minimax/minimax-m2.5 (via OpenRouter, reasoning enabled)
- MiniMax-M2.5 (via DashScope OpenAI-compatible API backup route)
- z-ai/glm-4.7 (via OpenRouter)
- z-ai/glm-5.1 (via OpenRouter)
- z-ai/glm-5 (via OpenRouter)
- qwen/qwen3.6-plus (via OpenRouter)
- qwen3.5-plus (via DashScope native API)
- anthropic/claude-sonnet-4.5 (via OpenRouter)
- moonshotai/kimi-k2.5 (via OpenRouter, reasoning enabled)
- moonshotai/kimi-k2.5 (via PPIO OpenAI-compatible API)
- kimi-k2.5 (via Moonshot official API, thinking enabled by default)
- doubao-seed-2-0-* (via Volcengine Ark OpenAI-compatible API)

OpenRouter Reasoning:
- Uses `extra_body={"reasoning": {"effort": "high"}}` format
- Reasoning tokens appear in response.choices[].message.reasoning
- For streaming: choices[].delta.reasoning_content

Moonshot Kimi K2.5:
- Uses OpenAI-compatible endpoint `https://api.moonshot.cn/v1`
- Uses `extra_body={"thinking": {"type": "enabled"}}` by default

PPIO Kimi K2.5:
- Uses OpenAI-compatible endpoint `https://api.ppio.com/openai`
- Reuses Moonshot formatter semantics to preserve reasoning_content in tool rounds
"""

import os
from typing import Optional, Tuple

from agentscope.formatter import (
    DashScopeChatFormatter,
    FormatterBase,
    OpenAIChatFormatter,
)
from agentscope.model import ChatModelBase

from .dashscope_compat import apply_dashscope_streamreader_compat_patch
from .dashscope_qwen_native_model import DashScopeQwenNativeChatModel
from .openrouter_model import OpenRouterChatModel
from .moonshot_formatter import MoonshotChatFormatter
from .openrouter_reasoning_formatter import (
    OpenRouterKimiHybridFormatter,
    OpenRouterReasoningChatFormatter,
)
from utils.logger import logger


class ModelFactory:
    """Model factory for creating AgentScope models."""

    OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
    MOONSHOT_BASE_URL = "https://api.moonshot.cn/v1"
    PPIO_BASE_URL = "https://api.ppio.com/openai"
    DEEPSEEK_BASE_URL = "https://api.deepseek.com"
    DEEPSEEK_BETA_BASE_URL = "https://api.deepseek.com/beta"
    DASHSCOPE_BASE_URL = "https://dashscope.aliyuncs.com/api/v1"
    DASHSCOPE_COMPAT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    VOLCENGINE_ARK_BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
    OPENAI_PROXY_BASE_URL = "https://api.openai-proxy.org/v1"

    @staticmethod
    def _select_openai_compatible_formatter(provider: str, model_name: str):
        if provider in {"moonshot", "ppio"}:
            return MoonshotChatFormatter()
        if provider == "deepseek":
            return MoonshotChatFormatter()
        if provider in {"openrouter", "openai_proxy"}:
            if str(model_name or "").strip().lower() in {
                "moonshotai/kimi-k2.5",
                "moonshotai/kimi-k2.6",
            }:
                return OpenRouterKimiHybridFormatter()
            return OpenRouterReasoningChatFormatter()
        return OpenAIChatFormatter()

    # Model configurations
    # reasoning_effort: "xhigh", "high", "medium", "low", "minimal", "none", or None
    MODELS = {
        # Gemini models (with reasoning enabled)
        "gemini-3-pro": {
            "name": "google/gemini-3.1-pro-preview",
            "provider": "openrouter",
            "reasoning_effort": "medium",  # Enable thinking for Gemini
        },
        "gemini-3-flash": {
            "name": "google/gemini-3-flash-preview",
            "provider": "openrouter",
            "reasoning_effort": "medium",  # Enable thinking for Gemini
        },
        # Minimax M2.1 (OpenRouter)
        "minimax-m2.1": {
            "name": "minimax/minimax-m2.1",
            "provider": "openrouter",
            "reasoning_effort": None,
        },
        # Minimax M2.5 via OpenRouter.
        "openrouter-minimax-m2.5": {
            "name": "minimax/minimax-m2.5",
            "provider": "openrouter",
            "reasoning_enabled": True,
            "reasoning_effort": "high",
        },
        "openrouter-minimax-m2.7": {
            "name": "minimax/minimax-m2.7",
            "provider": "openrouter",
            "reasoning_enabled": True,
            "reasoning_effort": "high",
        },
        "mimo-v2-pro": {
            "name": "xiaomi/mimo-v2-pro",
            "provider": "openrouter",
            "reasoning_enabled": True,
            "reasoning_effort": "high",
        },
        "mimo-v2.5-pro": {
            "name": "xiaomi/mimo-v2.5-pro",
            "provider": "openrouter",
            "reasoning_enabled": True,
            "reasoning_effort": "high",
        },
        # Minimax M2.5 via DashScope OpenAI-compatible API.
        "dashscope-minimax-m2.5": {
            "name": "MiniMax-M2.5",
            "provider": "dashscope_compatible",
            "reasoning_effort": "high",
        },
        # Legacy internal key kept for Shadow Clone / review defaults.
        "minimax-m2.5": {
            "name": "MiniMax-M2.5",
            "provider": "dashscope_compatible",
            "reasoning_effort": "high",
        },
        # GLM 4.7 (OpenRouter)
        "glm-4.7": {
            "name": "z-ai/glm-4.7",
            "provider": "openrouter",
            "reasoning_effort": None,
        },
        # GLM 5 (OpenRouter)
        "glm-5.1": {
            "name": "z-ai/glm-5.1",
            "provider": "openrouter",
            "reasoning_effort": None,
        },
        "glm-5": {
            "name": "z-ai/glm-5",
            "provider": "openrouter",
            "reasoning_effort": None,
        },
        # Qwen 3.6 Plus (OpenRouter)
        "qwen3.6-plus": {
            "name": "qwen/qwen3.6-plus",
            "provider": "openrouter",
            "reasoning_effort": None,
        },
        # Qwen 3.5 Plus (DashScope native API)
        "qwen3.5-plus": {
            "name": "qwen3.5-plus",
            "provider": "dashscope",
            "enable_thinking": True,
            "reasoning_effort": None,
        },
        # Legacy key compatibility -> transparently route to qwen3.5-plus
        "qwen3.5-397b-a17b": {
            "name": "qwen3.5-plus",
            "provider": "dashscope",
            "enable_thinking": True,
            "reasoning_effort": None,
        },
        # Claude Sonnet 4.5 (OpenRouter, reasoning enabled)
        "claude-sonnet-4.5": {
            "name": "anthropic/claude-sonnet-4.5",
            "provider": "openrouter",
            "reasoning_effort": "xhigh",
        },
        # Kimi K2.5 via OpenRouter
        "openrouter-kimi-k2.5": {
            "name": "moonshotai/kimi-k2.5",
            "provider": "openrouter",
            "reasoning_enabled": True,
            "reasoning_effort": None,
        },
        "openrouter-kimi-k2.6": {
            "name": "moonshotai/kimi-k2.6",
            "provider": "openrouter",
            "reasoning_enabled": True,
            "reasoning_effort": None,
        },
        "openrouter-deepseek-v4-pro": {
            "name": "deepseek/deepseek-v4-pro",
            "provider": "openrouter",
            "reasoning_enabled": True,
            "reasoning_effort": "high",
            "max_tokens": 32768,
        },
        "openrouter-deepseek-v4-flash": {
            "name": "deepseek/deepseek-v4-flash",
            "provider": "openrouter",
            "reasoning_enabled": True,
            "reasoning_effort": "high",
            "max_tokens": 32768,
        },
        # Kimi K2.5 via PPIO OpenAI-compatible API
        "ppio-kimi-k2.5": {
            "name": "moonshotai/kimi-k2.5",
            "provider": "ppio",
            "reasoning_effort": None,
        },
        # Kimi K2.5 via Moonshot official API
        "kimi-k2.5": {
            "name": "kimi-k2.5",
            "provider": "moonshot",
            "thinking_mode": "enabled",
            "reasoning_effort": None,
        },
        "kimi-k2.6": {
            "name": "kimi-k2.6",
            "provider": "moonshot",
            "thinking_mode": "enabled",
            "reasoning_effort": None,
        },
        "deepseek-v4-pro-high": {
            "name": "deepseek-v4-pro",
            "provider": "deepseek",
            "reasoning_effort": "high",
            "max_tokens": 32768,
        },
        "deepseek-v4-pro-max": {
            "name": "deepseek-v4-pro",
            "provider": "deepseek",
            "reasoning_effort": "max",
            "max_tokens": 32768,
        },
        "deepseek-v4-flash-high": {
            "name": "deepseek-v4-flash",
            "provider": "deepseek",
            "reasoning_effort": "high",
            "max_tokens": 32768,
        },
        "deepseek-v4-flash-max": {
            "name": "deepseek-v4-flash",
            "provider": "deepseek",
            "reasoning_effort": "max",
            "max_tokens": 32768,
        },
        # Volcengine Ark Doubao models (OpenAI-compatible)
        "doubao-seed-2-0-pro-260215": {
            "name": "doubao-seed-2-0-pro-260215",
            "provider": "volcengine",
            "reasoning_effort": None,
        },
        "doubao-seed-2-0-code-preview-260215": {
            "name": "doubao-seed-2-0-code-preview-260215",
            "provider": "volcengine",
            "reasoning_effort": None,
        },
        "proxy-gpt-5.4": {
            "name": "gpt-5.4",
            "provider": "openai_proxy",
            # The current proxy chat/completions route does not expose separable
            # reasoning chunks, so keep this route on plain content streaming.
            "reasoning_enabled": False,
            "reasoning_effort": None,
        },
    }

    # Default model
    DEFAULT_MODEL = "gemini-3-flash"

    @classmethod
    def get_available_models(cls) -> list:
        """Get list of available model keys."""
        return list(cls.MODELS.keys())

    @staticmethod
    def _env_flag(name: str, default: bool) -> bool:
        value = os.environ.get(name)
        if value is None:
            return default
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    @staticmethod
    def _is_http_url(value: str) -> bool:
        normalized = str(value or "").strip().lower()
        return normalized.startswith("http://") or normalized.startswith("https://")

    @staticmethod
    def _is_dashscope_compatible_mode_url(value: str) -> bool:
        return "compatible-mode" in str(value or "").strip().lower()

    @classmethod
    def _resolve_dashscope_native_base_url(cls) -> Tuple[str, str]:
        """Resolve DashScope native endpoint with auto-heal fallback."""
        strict_native = cls._env_flag(
            "AGENTSCOPE_QWEN_DASHSCOPE_STRICT_NATIVE",
            default=True,
        )
        candidates = [
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
            if cls._is_dashscope_compatible_mode_url(normalized):
                if strict_native:
                    logger.warning(
                        "Ignoring DashScope %s because it points to compatible-mode endpoint: %s",
                        source,
                        normalized,
                    )
                    continue
                return normalized, source
            if not cls._is_http_url(normalized):
                logger.warning(
                    "Ignoring DashScope %s because it is not a valid http(s) URL: %s",
                    source,
                    normalized,
                )
                continue
            return normalized, source

        return cls.DASHSCOPE_BASE_URL, "default_native"

    @classmethod
    def _resolve_dashscope_compatible_base_url(cls) -> Tuple[str, str]:
        """Resolve DashScope OpenAI-compatible endpoint for non-native providers."""
        candidates = [
            (
                "DASHSCOPE_COMPAT_BASE_URL",
                os.environ.get("DASHSCOPE_COMPAT_BASE_URL"),
            ),
            (
                "DASHSCOPE_COMPATIBLE_BASE_URL",
                os.environ.get("DASHSCOPE_COMPATIBLE_BASE_URL"),
            ),
            ("DASHSCOPE_BASE_URL", os.environ.get("DASHSCOPE_BASE_URL")),
            ("DASHSCOPE_API_BASE", os.environ.get("DASHSCOPE_API_BASE")),
        ]

        for source, candidate in candidates:
            normalized = str(candidate or "").strip()
            if not normalized:
                continue
            if not cls._is_http_url(normalized):
                logger.warning(
                    "Ignoring DashScope %s because it is not a valid http(s) URL: %s",
                    source,
                    normalized,
                )
                continue
            if not cls._is_dashscope_compatible_mode_url(normalized):
                logger.warning(
                    "Ignoring DashScope %s because compatible-mode endpoint is required: %s",
                    source,
                    normalized,
                )
                continue
            return normalized, source

        return cls.DASHSCOPE_COMPAT_BASE_URL, "default_compatible"

    @classmethod
    def _resolve_provider_connection(
        cls,
        provider: str,
        api_key: Optional[str],
    ) -> Tuple[str, Optional[str], Optional[str]]:
        """Resolve provider-specific API key and base URL."""
        if provider == "moonshot":
            resolved_api_key = api_key or os.environ.get("MOONSHOT_API_KEY")
            if not resolved_api_key:
                raise ValueError(
                    "MOONSHOT_API_KEY environment variable is not set",
                )

            base_url = (
                os.environ.get("MOONSHOT_API_BASE")
                or os.environ.get("MOONSHOT_BASE_URL")
                or cls.MOONSHOT_BASE_URL
            )
            return resolved_api_key, base_url, None

        if provider == "ppio":
            resolved_api_key = api_key or os.environ.get("PPIO_API_KEY")
            if not resolved_api_key:
                raise ValueError(
                    "PPIO_API_KEY environment variable is not set",
                )

            base_url = (
                os.environ.get("PPIO_API_BASE")
                or os.environ.get("PPIO_BASE_URL")
                or cls.PPIO_BASE_URL
            )
            return resolved_api_key, base_url, None

        if provider == "deepseek":
            resolved_api_key = api_key or os.environ.get("DEEPSEEK_API_KEY")
            if not resolved_api_key:
                raise ValueError(
                    "DEEPSEEK_API_KEY environment variable is not set",
                )

            base_url = (
                os.environ.get("DEEPSEEK_API_BASE")
                or os.environ.get("DEEPSEEK_BASE_URL")
                or (
                    cls.DEEPSEEK_BETA_BASE_URL
                    if os.environ.get("AGENTSCOPE_DEEPSEEK_STRICT_MODE", "")
                    .strip()
                    .lower()
                    in ("1", "true")
                    else cls.DEEPSEEK_BASE_URL
                )
            )
            return resolved_api_key, base_url, None

        if provider == "dashscope":
            resolved_api_key = api_key or os.environ.get("DASHSCOPE_API_KEY")
            if not resolved_api_key:
                raise ValueError(
                    "DASHSCOPE_API_KEY environment variable is not set",
                )

            base_url, base_source = cls._resolve_dashscope_native_base_url()
            return resolved_api_key, base_url, base_source

        if provider == "dashscope_compatible":
            resolved_api_key = api_key or os.environ.get("DASHSCOPE_API_KEY")
            if not resolved_api_key:
                raise ValueError(
                    "DASHSCOPE_API_KEY environment variable is not set",
                )

            base_url, base_source = cls._resolve_dashscope_compatible_base_url()
            return resolved_api_key, base_url, base_source

        if provider == "volcengine":
            resolved_api_key = (
                api_key
                or os.environ.get("VOLCENGINE_ARK_API_KEY")
                or os.environ.get("ARK_API_KEY")
            )
            if not resolved_api_key:
                raise ValueError(
                    "VOLCENGINE_ARK_API_KEY (or ARK_API_KEY) environment variable is not set",
                )

            base_url = (
                os.environ.get("VOLCENGINE_ARK_BASE_URL")
                or os.environ.get("ARK_BASE_URL")
                or cls.VOLCENGINE_ARK_BASE_URL
            )
            return resolved_api_key, base_url, None

        if provider == "openai_proxy":
            resolved_api_key = api_key or os.environ.get("OPENAI_PROXY_API_KEY")
            if not resolved_api_key:
                raise ValueError(
                    "OPENAI_PROXY_API_KEY environment variable is not set",
                )

            base_url = (
                os.environ.get("OPENAI_PROXY_API_BASE")
                or os.environ.get("OPENAI_PROXY_BASE_URL")
                or cls.OPENAI_PROXY_BASE_URL
            )
            return resolved_api_key, base_url, None

        # Default to OpenRouter.
        resolved_api_key = api_key or os.environ.get("OPENROUTER_API_KEY")
        if not resolved_api_key:
            raise ValueError(
                "OPENROUTER_API_KEY environment variable is not set",
            )

        base_url = (
            os.environ.get("OPENROUTER_API_BASE")
            or os.environ.get("OPENROUTER_BASE_URL")
            or cls.OPENROUTER_BASE_URL
        )
        return resolved_api_key, base_url, None

    @classmethod
    def create(
        cls,
        model_key: str = None,
        stream: bool = True,
        api_key: str = None,
        reasoning_effort: Optional[str] = None,
        kv_cache_options: Optional[dict] = None,
        trace=None,
        prompt=None,
    ) -> Tuple[ChatModelBase, FormatterBase]:
        """
        Create a model and formatter pair.

        Args:
            model_key: Key from MODELS dict (e.g., "gemini-3-flash", "kimi-k2.5")
            stream: Whether to enable streaming
            api_key: Provider API key override
            kv_cache_options: KV cache runtime options passed to OpenRouter-like models
            trace: Langfuse trace object for generation tracking
            prompt: Langfuse prompt object to link every LLM call to (for Metrics-tab
                per-version cost/latency/quality attribution)

        Returns:
            Tuple of (model, formatter)
        """
        # Use default if not specified
        if model_key is None:
            model_key = cls.DEFAULT_MODEL

        if model_key == "deepseek-chat":
            logger.warning(
                "DeepSeek disabled; using kimi-k2.5 instead of deepseek-chat",
            )
            model_key = "kimi-k2.5"

        # Get config
        model_config = cls.MODELS.get(model_key)
        if model_config is None:
            # Try to match by partial name
            for key, config in cls.MODELS.items():
                if (
                    key in model_key.lower()
                    or model_key.lower() in config["name"].lower()
                ):
                    model_config = config
                    break

        if model_config is None:
            raise ValueError(
                f"Unknown model: {model_key}. "
                f"Available models: {list(cls.MODELS.keys())}"
            )

        effective_model_name = model_config["name"]
        if model_config.get("provider") == "openai_proxy":
            effective_model_name = (
                os.environ.get("OPENAI_PROXY_GPT_5_4_MODEL_NAME")
                or effective_model_name
            )

        provider = model_config.get("provider", "openrouter")
        resolved_api_key, base_url, dashscope_base_source = (
            cls._resolve_provider_connection(
                provider=provider,
                api_key=api_key,
            )
        )

        generate_kwargs = {}

        if provider == "moonshot":
            thinking_mode = model_config.get("thinking_mode")
            if thinking_mode in {"enabled", "disabled"}:
                generate_kwargs["extra_body"] = {
                    "thinking": {
                        "type": thinking_mode,
                    }
                }
        elif provider == "deepseek":
            effective_reasoning_effort = (
                reasoning_effort
                if reasoning_effort is not None
                else model_config.get("reasoning_effort")
            )
            if effective_reasoning_effort:
                generate_kwargs["reasoning_effort"] = effective_reasoning_effort
            generate_kwargs["extra_body"] = {
                "thinking": {
                    "type": "enabled",
                }
            }

            max_tokens = model_config.get("max_tokens")
            if isinstance(max_tokens, int) and max_tokens > 0:
                generate_kwargs["max_tokens"] = max_tokens
        elif provider == "ppio":
            pass
        elif provider == "dashscope":
            enable_thinking = model_config.get("enable_thinking")
            if enable_thinking is not None:
                generate_kwargs["enable_thinking"] = bool(enable_thinking)

            thinking_budget = model_config.get("thinking_budget")
            if isinstance(thinking_budget, int) and thinking_budget > 0:
                generate_kwargs["thinking_budget"] = thinking_budget

            thinking_budget_env = os.environ.get("AGENTSCOPE_QWEN_THINKING_BUDGET")
            if thinking_budget_env:
                try:
                    thinking_budget_value = int(thinking_budget_env)
                    if thinking_budget_value > 0:
                        generate_kwargs["thinking_budget"] = thinking_budget_value
                except ValueError:
                    logger.warning(
                        "Invalid AGENTSCOPE_QWEN_THINKING_BUDGET value: %s",
                        thinking_budget_env,
                    )
        else:
            # OpenAI-compatible providers use `reasoning` payload for thinking controls.
            extra_body = {}
            reasoning_enabled = model_config.get("reasoning_enabled")
            if reasoning_enabled is not None:
                extra_body["reasoning"] = {
                    "enabled": bool(reasoning_enabled),
                }

            effective_reasoning_effort = (
                reasoning_effort
                if reasoning_effort is not None
                else model_config.get("reasoning_effort")
            )
            if effective_reasoning_effort:
                reasoning_payload = extra_body.get("reasoning", {})
                reasoning_payload["effort"] = effective_reasoning_effort
                extra_body["reasoning"] = reasoning_payload

            if extra_body:
                generate_kwargs["extra_body"] = extra_body

        # ── Model-level max_tokens (can be overridden by AGENTSCOPE_MAX_TOKENS below) ──
        max_tokens_cfg = model_config.get("max_tokens")
        if isinstance(max_tokens_cfg, int) and max_tokens_cfg > 0:
            generate_kwargs["max_tokens"] = max_tokens_cfg

        max_tokens_env = os.environ.get("AGENTSCOPE_MAX_TOKENS")
        if max_tokens_env:
            try:
                max_tokens_value = int(max_tokens_env)
                if max_tokens_value > 0:
                    generate_kwargs["max_tokens"] = max_tokens_value
            except ValueError:
                logger.warning(
                    "Invalid AGENTSCOPE_MAX_TOKENS value: %s", max_tokens_env
                )

        if provider == "dashscope":
            apply_dashscope_streamreader_compat_patch()
            logger.info(
                "[ModelFactory] DashScope native route selected for model=%s base=%s source=%s strict_native=%s openai_base_set=%s",
                model_config["name"],
                base_url,
                dashscope_base_source or "n/a",
                cls._env_flag("AGENTSCOPE_QWEN_DASHSCOPE_STRICT_NATIVE", default=True),
                bool(str(os.environ.get("OPENAI_BASE_URL") or "").strip()),
            )
            model_kwargs = {
                "model_name": model_config["name"],
                "api_key": resolved_api_key,
                "stream": stream,
            }
            if base_url:
                model_kwargs["base_http_api_url"] = base_url
            if "enable_thinking" in generate_kwargs:
                model_kwargs["enable_thinking"] = generate_kwargs.pop(
                    "enable_thinking",
                )
            if generate_kwargs:
                model_kwargs["generate_kwargs"] = generate_kwargs

            model = DashScopeQwenNativeChatModel(**model_kwargs)
            formatter = DashScopeChatFormatter()
        else:
            if provider == "dashscope_compatible":
                logger.info(
                    "[ModelFactory] DashScope compatible route selected for model=%s base=%s source=%s",
                    effective_model_name,
                    base_url,
                    dashscope_base_source or "n/a",
                )
            model_kwargs = {
                "model_name": effective_model_name,
                "api_key": resolved_api_key,
                "client_kwargs": {"base_url": base_url},
                "stream": stream,
            }
            if kv_cache_options is not None:
                model_kwargs["kv_cache_options"] = dict(kv_cache_options)

            if generate_kwargs:
                model_kwargs["generate_kwargs"] = generate_kwargs

            if trace is not None:
                model_kwargs["trace"] = trace
            if prompt is not None:
                model_kwargs["prompt"] = prompt
            model = OpenRouterChatModel(**model_kwargs)
            formatter = cls._select_openai_compatible_formatter(
                provider,
                effective_model_name,
            )

        return model, formatter

    @classmethod
    def create_from_full_name(
        cls,
        model_name: str,
        stream: bool = True,
        api_key: str = None,
        reasoning_effort: Optional[str] = None,
        kv_cache_options: Optional[dict] = None,
        trace=None,
        prompt=None,
    ) -> Tuple[ChatModelBase, FormatterBase]:
        """
        Create a model from full model name.

        Args:
            model_name: Full model name (e.g., "google/gemini-3-flash-preview")
            stream: Whether to enable streaming
            api_key: Provider API key override
            reasoning_effort: Reasoning effort level
            kv_cache_options: KV cache runtime options passed to OpenRouter-like models
            trace: Langfuse trace object for generation tracking
            prompt: Langfuse prompt object to link every LLM call

        Returns:
            Tuple of (model, formatter)
        """
        if str(model_name or "").strip().lower() == "deepseek-chat":
            logger.warning(
                "DeepSeek disabled; using kimi-k2.5 instead of %s",
                model_name,
            )
            model_name = "kimi-k2.5"

        if str(model_name or "").strip().lower() == "gpt-5.4":
            model_name = "openai-proxy/gpt-5.4"

        lowered_model_name = model_name.lower()
        normalized_model_name = model_name
        provider = "openrouter"
        if lowered_model_name.startswith(
            "ppio/moonshotai/kimi-k2.5"
        ) or lowered_model_name.startswith("ppio/kimi-k2.5"):
            provider = "ppio"
            normalized_model_name = "moonshotai/kimi-k2.5"
        elif (
            lowered_model_name.startswith("openrouter/qwen/qwen3.6-plus")
            or lowered_model_name.startswith("qwen/qwen3.6-plus")
            or "qwen3.6-plus" in lowered_model_name
        ):
            provider = "openrouter"
            normalized_model_name = "qwen/qwen3.6-plus"
        elif (
            lowered_model_name.startswith("dashscope/qwen3.5-plus")
            or lowered_model_name.startswith("qwen/qwen3.5-plus")
            or lowered_model_name.startswith("openrouter/qwen/qwen3.5-plus")
            or "qwen3.5-plus" in lowered_model_name
        ):
            provider = "dashscope"
            normalized_model_name = "qwen3.5-plus"
        elif (
            lowered_model_name.startswith("dashscope/qwen3.5-397b-a17b")
            or lowered_model_name.startswith("qwen/qwen3.5-397b-a17b")
            or lowered_model_name.startswith("openrouter/qwen/qwen3.5-397b-a17b")
            or "qwen3.5-397b-a17b" in lowered_model_name
        ):
            provider = "dashscope"
            normalized_model_name = "qwen3.5-plus"
        elif lowered_model_name.startswith(
            "openrouter/minimax/minimax-m2.5"
        ) or lowered_model_name.startswith("minimax/minimax-m2.5"):
            provider = "openrouter"
            normalized_model_name = "minimax/minimax-m2.5"
        elif (
            lowered_model_name.startswith("openrouter/z-ai/glm-5.1")
            or lowered_model_name.startswith("z-ai/glm-5.1")
            or lowered_model_name == "glm-5.1"
        ):
            provider = "openrouter"
            normalized_model_name = "z-ai/glm-5.1"
        elif lowered_model_name.startswith(
            "openrouter/minimax/minimax-m2.7"
        ) or lowered_model_name.startswith("minimax/minimax-m2.7"):
            provider = "openrouter"
            normalized_model_name = "minimax/minimax-m2.7"
        elif lowered_model_name.startswith(
            "openrouter/xiaomi/mimo-v2.5-pro"
        ) or lowered_model_name.startswith("xiaomi/mimo-v2.5-pro"):
            provider = "openrouter"
            normalized_model_name = "xiaomi/mimo-v2.5-pro"
        elif lowered_model_name.startswith(
            "openrouter/xiaomi/mimo-v2-pro"
        ) or lowered_model_name.startswith("xiaomi/mimo-v2-pro"):
            provider = "openrouter"
            normalized_model_name = "xiaomi/mimo-v2-pro"
        elif (
            lowered_model_name.startswith("dashscope/minimax-m2.5")
            or lowered_model_name.startswith("minimax-m2.5")
            or lowered_model_name == "minimax/m2.5"
            or model_name == "MiniMax-M2.5"
        ):
            provider = "dashscope_compatible"
            normalized_model_name = "MiniMax-M2.5"
        elif lowered_model_name.startswith("openrouter/moonshotai/kimi-k2.5"):
            provider = "openrouter"
            normalized_model_name = "moonshotai/kimi-k2.5"
        elif lowered_model_name.startswith("openrouter/moonshotai/kimi-k2.6"):
            provider = "openrouter"
            normalized_model_name = "moonshotai/kimi-k2.6"
        elif lowered_model_name.startswith("openrouter/deepseek/deepseek-v4-pro"):
            provider = "openrouter"
            normalized_model_name = "deepseek/deepseek-v4-pro"
        elif lowered_model_name.startswith("openrouter/deepseek/deepseek-v4-flash"):
            provider = "openrouter"
            normalized_model_name = "deepseek/deepseek-v4-flash"
        elif lowered_model_name in {
            "deepseek-v4-pro-high",
            "deepseek-v4-pro-max",
            "deepseek-v4-flash-high",
            "deepseek-v4-flash-max",
        }:
            model_config = cls.MODELS[lowered_model_name]
            provider = "deepseek"
            normalized_model_name = model_config["name"]
            if reasoning_effort is None:
                reasoning_effort = model_config.get("reasoning_effort")
        elif lowered_model_name.startswith("openrouter/z-ai/glm-5"):
            provider = "openrouter"
            normalized_model_name = "z-ai/glm-5"
        elif lowered_model_name.startswith("openai-proxy/"):
            provider = "openai_proxy"
            normalized_model_name = model_name.split("openai-proxy/", 1)[1]
        elif lowered_model_name.startswith(
            "volcengine/"
        ) or lowered_model_name.startswith("doubao-"):
            provider = "volcengine"
        elif lowered_model_name.startswith("openrouter/"):
            provider = "openrouter"
        elif (
            lowered_model_name.startswith("kimi-")
            or lowered_model_name.endswith("/kimi-k2.5")
            or lowered_model_name.endswith("/kimi-k2.6")
        ):
            provider = "moonshot"
            if lowered_model_name.endswith("/kimi-k2.6"):
                normalized_model_name = "kimi-k2.6"
            elif lowered_model_name.endswith("/kimi-k2.5"):
                normalized_model_name = "kimi-k2.5"
        thinking_mode: Optional[str] = None
        reasoning_enabled: Optional[bool] = None

        # Check if this is a known model and inherit defaults.
        max_tokens_from_config: int | None = None
        for key, model_config in cls.MODELS.items():
            if (
                model_config["name"] == normalized_model_name
                and model_config.get("provider", provider) == provider
            ):
                if reasoning_effort is None:
                    reasoning_effort = model_config.get("reasoning_effort")
                provider = model_config.get("provider", provider)
                thinking_mode = model_config.get("thinking_mode")
                reasoning_enabled = model_config.get("reasoning_enabled")
                max_tokens_from_config = model_config.get("max_tokens")
                break

        resolved_api_key, base_url, dashscope_base_source = (
            cls._resolve_provider_connection(
                provider=provider,
                api_key=api_key,
            )
        )

        if provider == "openai_proxy":
            normalized_model_name = (
                os.environ.get("OPENAI_PROXY_GPT_5_4_MODEL_NAME")
                or normalized_model_name
            )

        generate_kwargs = {}

        if provider == "moonshot":
            mode = thinking_mode or "enabled"
            if mode in {"enabled", "disabled"}:
                generate_kwargs["extra_body"] = {
                    "thinking": {
                        "type": mode,
                    }
                }
        elif provider == "deepseek":
            if reasoning_effort:
                generate_kwargs["reasoning_effort"] = reasoning_effort
            generate_kwargs["extra_body"] = {
                "thinking": {
                    "type": "enabled",
                }
            }
        elif provider == "ppio":
            pass
        elif provider == "dashscope":
            enable_thinking = True
            if enable_thinking is not None:
                generate_kwargs["enable_thinking"] = bool(enable_thinking)

            thinking_budget_env = os.environ.get("AGENTSCOPE_QWEN_THINKING_BUDGET")
            if thinking_budget_env:
                try:
                    thinking_budget_value = int(thinking_budget_env)
                    if thinking_budget_value > 0:
                        generate_kwargs["thinking_budget"] = thinking_budget_value
                except ValueError:
                    logger.warning(
                        "Invalid AGENTSCOPE_QWEN_THINKING_BUDGET value: %s",
                        thinking_budget_env,
                    )
        else:
            extra_body = {}
            if reasoning_enabled is not None:
                extra_body["reasoning"] = {
                    "enabled": bool(reasoning_enabled),
                }
            if reasoning_effort:
                reasoning_payload = extra_body.get("reasoning", {})
                reasoning_payload["effort"] = reasoning_effort
                extra_body["reasoning"] = reasoning_payload
            if extra_body:
                generate_kwargs["extra_body"] = extra_body

        # ── Model-level max_tokens (can be overridden by AGENTSCOPE_MAX_TOKENS below) ──
        if isinstance(max_tokens_from_config, int) and max_tokens_from_config > 0:
            generate_kwargs["max_tokens"] = max_tokens_from_config

        max_tokens_env = os.environ.get("AGENTSCOPE_MAX_TOKENS")
        if max_tokens_env:
            try:
                max_tokens_value = int(max_tokens_env)
                if max_tokens_value > 0:
                    generate_kwargs["max_tokens"] = max_tokens_value
            except ValueError:
                logger.warning(
                    "Invalid AGENTSCOPE_MAX_TOKENS value: %s", max_tokens_env
                )

        if provider == "dashscope":
            if lowered_model_name.startswith("openrouter/"):
                logger.warning(
                    "[ModelFactory] Ignoring OpenRouter-style model prefix for Qwen and forcing DashScope native provider: %s",
                    model_name,
                )
            apply_dashscope_streamreader_compat_patch()
            logger.info(
                "[ModelFactory] DashScope native route selected for model=%s base=%s source=%s strict_native=%s openai_base_set=%s",
                normalized_model_name,
                base_url,
                dashscope_base_source or "n/a",
                cls._env_flag("AGENTSCOPE_QWEN_DASHSCOPE_STRICT_NATIVE", default=True),
                bool(str(os.environ.get("OPENAI_BASE_URL") or "").strip()),
            )
            model_kwargs = {
                "model_name": normalized_model_name,
                "api_key": resolved_api_key,
                "stream": stream,
            }
            if base_url:
                model_kwargs["base_http_api_url"] = base_url
            if "enable_thinking" in generate_kwargs:
                model_kwargs["enable_thinking"] = generate_kwargs.pop(
                    "enable_thinking",
                )
            if generate_kwargs:
                model_kwargs["generate_kwargs"] = generate_kwargs
            model = DashScopeQwenNativeChatModel(**model_kwargs)
            formatter = DashScopeChatFormatter()
        else:
            if provider == "dashscope_compatible":
                logger.info(
                    "[ModelFactory] DashScope compatible route selected for model=%s base=%s source=%s",
                    normalized_model_name,
                    base_url,
                    dashscope_base_source or "n/a",
                )
            model_kwargs = {
                "model_name": normalized_model_name,
                "api_key": resolved_api_key,
                "client_kwargs": {"base_url": base_url},
                "stream": stream,
            }
            if kv_cache_options is not None:
                model_kwargs["kv_cache_options"] = dict(kv_cache_options)

            if generate_kwargs:
                model_kwargs["generate_kwargs"] = generate_kwargs

            if trace is not None:
                model_kwargs["trace"] = trace
            if prompt is not None:
                model_kwargs["prompt"] = prompt
            model = OpenRouterChatModel(**model_kwargs)
            formatter = cls._select_openai_compatible_formatter(
                provider,
                normalized_model_name,
            )

        return model, formatter
