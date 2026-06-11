import asyncio
import sys
import types
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

# Minimal stub for optional logging dependency during import.
if "structlog" not in sys.modules:

    class _DummyBoundLogger:
        def info(self, *args, **kwargs):
            return None

        def warning(self, *args, **kwargs):
            return None

        def error(self, *args, **kwargs):
            return None

        def debug(self, *args, **kwargs):
            return None

    class _DummyProcessorFormatter:
        @staticmethod
        def wrap_for_formatter(*args, **kwargs):
            return None

    dummy_structlog = types.SimpleNamespace(
        configure=lambda **kwargs: None,
        get_logger=lambda *args, **kwargs: _DummyBoundLogger(),
        stdlib=types.SimpleNamespace(
            add_log_level=lambda *args, **kwargs: None,
            PositionalArgumentsFormatter=lambda *args, **kwargs: None,
            ProcessorFormatter=_DummyProcessorFormatter,
            LoggerFactory=lambda *args, **kwargs: None,
            BoundLogger=_DummyBoundLogger,
        ),
        processors=types.SimpleNamespace(TimeStamper=lambda *args, **kwargs: None),
        contextvars=types.SimpleNamespace(
            clear_contextvars=lambda: None,
            bind_contextvars=lambda **kwargs: None,
            get_contextvars=lambda: {},
        ),
    )
    sys.modules["structlog"] = dummy_structlog


if "services.postgresql" not in sys.modules:
    services_pkg = types.ModuleType("services")
    services_pkg.__path__ = [str(BACKEND_ROOT / "services")]
    postgresql_mod = types.ModuleType("services.postgresql")
    eval_runtime_registry_mod = types.ModuleType("services.eval_runtime_registry")

    class _DummyDBConnection:
        @property
        async def client(self):
            raise RuntimeError("DB client is not available in this unit test")

    postgresql_mod.DBConnection = _DummyDBConnection
    eval_runtime_registry_mod.record_model_generation = lambda *args, **kwargs: None
    eval_runtime_registry_mod.record_ttft_seconds = lambda *args, **kwargs: None
    eval_runtime_registry_mod.record_safety_event = lambda *args, **kwargs: None
    eval_runtime_registry_mod.record_tool_event = lambda *args, **kwargs: None
    eval_runtime_registry_mod.start_eval_run = lambda *args, **kwargs: None
    eval_runtime_registry_mod.pop_eval_run = lambda *args, **kwargs: None
    eval_runtime_registry_mod.update_eval_run_metadata = lambda *args, **kwargs: None
    services_pkg.postgresql = postgresql_mod
    services_pkg.eval_runtime_registry = eval_runtime_registry_mod
    sys.modules["services"] = services_pkg
    sys.modules["services.postgresql"] = postgresql_mod
    sys.modules["services.eval_runtime_registry"] = eval_runtime_registry_mod

from agentscope.formatter import DashScopeChatFormatter, OpenAIChatFormatter
from agentscope.message import (
    Msg,
    ThinkingBlock,
    ToolUseBlock,
    ToolResultBlock,
    TextBlock,
)
from agentscope.model import DashScopeChatModel

from agentscope_integration.models.model_factory import ModelFactory
from agentscope_integration.models.dashscope_qwen_native_model import (
    DashScopeQwenNativeChatModel,
)
from agentscope_integration.models.moonshot_formatter import MoonshotChatFormatter
from agentscope_integration.models.openrouter_reasoning_formatter import (
    OPENROUTER_REASONING_DETAILS_KEY,
    OpenRouterKimiHybridFormatter,
    OpenRouterReasoningChatFormatter,
)
from utils.constants import MODEL_NAME_ALIASES


def test_moonshot_formatter_keeps_reasoning_content_for_tool_calls() -> None:
    formatter = MoonshotChatFormatter()
    msg = Msg(
        name="assistant",
        role="assistant",
        content=[
            ThinkingBlock(
                type="thinking", thinking="Need to call tool for latest data."
            ),
            ToolUseBlock(
                type="tool_use",
                id="call_123",
                name="web_search",
                input={"query": "Kimi API docs"},
            ),
        ],
    )

    formatted = asyncio.run(formatter.format([msg]))

    assert len(formatted) == 1
    assistant = formatted[0]
    assert assistant["role"] == "assistant"
    assert assistant.get("tool_calls")
    assert assistant.get("reasoning_content") == "Need to call tool for latest data."


def test_moonshot_formatter_backfills_reasoning_for_tool_calls_missing_thinking() -> (
    None
):
    formatter = MoonshotChatFormatter()
    msg = Msg(
        name="assistant",
        role="assistant",
        content=[
            ToolUseBlock(
                type="tool_use",
                id="call_missing_reasoning",
                name="read_file",
                input={"path": "/workspace/report.md"},
            ),
        ],
    )

    formatted = asyncio.run(formatter.format([msg]))

    assistant = formatted[0]
    assert assistant["role"] == "assistant"
    assert assistant.get("tool_calls")
    assert assistant.get("reasoning_content")


def test_model_factory_uses_moonshot_formatter_for_kimi() -> None:
    _, formatter = ModelFactory.create("kimi-k2.5", api_key="test-moonshot-key")
    assert isinstance(formatter, MoonshotChatFormatter)


def test_model_factory_supports_official_kimi_k26_separately_from_openrouter() -> None:
    model, formatter = ModelFactory.create("kimi-k2.6", api_key="test-moonshot-key")

    assert isinstance(formatter, MoonshotChatFormatter)
    assert model.model_name == "kimi-k2.6"
    assert "api.moonshot.cn" in getattr(model, "_configured_base_url", "")
    extra_body = (model.generate_kwargs or {}).get("extra_body", {})
    assert extra_body == {"thinking": {"type": "enabled"}}


def test_model_factory_keeps_openai_formatter_for_non_moonshot() -> None:
    _, formatter = ModelFactory.create("gemini-3-flash", api_key="test-openrouter-key")
    assert isinstance(formatter, OpenAIChatFormatter)
    assert not isinstance(formatter, MoonshotChatFormatter)


def test_moonshot_formatter_supports_long_tool_call_chains() -> None:
    formatter = MoonshotChatFormatter()
    msgs = []

    for idx in range(300):
        call_id = f"call_{idx}"
        msgs.append(
            Msg(
                name="assistant",
                role="assistant",
                content=[
                    ThinkingBlock(
                        type="thinking",
                        thinking=f"Round {idx}: prepare tool call.",
                    ),
                    ToolUseBlock(
                        type="tool_use",
                        id=call_id,
                        name="web_search",
                        input={"query": f"q-{idx}"},
                    ),
                ],
            ),
        )
        msgs.append(
            Msg(
                name="tool",
                role="assistant",
                content=[
                    ToolResultBlock(
                        type="tool_result",
                        id=call_id,
                        name="web_search",
                        output=[TextBlock(type="text", text=f"result-{idx}")],
                    ),
                ],
            ),
        )

    formatted = asyncio.run(formatter.format(msgs))

    assistant_messages = [msg for msg in formatted if msg.get("role") == "assistant"]
    tool_messages = [msg for msg in formatted if msg.get("role") == "tool"]

    assert len(assistant_messages) == 300
    assert len(tool_messages) == 300
    assert all(msg.get("reasoning_content") for msg in assistant_messages)


def test_model_factory_supports_openrouter_kimi_with_reasoning_enabled() -> None:
    model, formatter = ModelFactory.create(
        "openrouter-kimi-k2.5", api_key="test-openrouter-key"
    )

    assert isinstance(formatter, OpenRouterKimiHybridFormatter)
    assert not isinstance(formatter, MoonshotChatFormatter)
    assert model.model_name == "moonshotai/kimi-k2.5"
    assert "openrouter.ai" in getattr(model, "_configured_base_url", "")

    extra_body = (model.generate_kwargs or {}).get("extra_body", {})
    assert extra_body.get("reasoning", {}).get("enabled") is True


def test_model_factory_supports_openrouter_kimi_k26_with_hybrid_formatter() -> None:
    model, formatter = ModelFactory.create(
        "openrouter-kimi-k2.6",
        api_key="test-openrouter-key",
    )

    assert isinstance(formatter, OpenRouterKimiHybridFormatter)
    assert model.model_name == "moonshotai/kimi-k2.6"
    assert "openrouter.ai" in getattr(model, "_configured_base_url", "")
    extra_body = (model.generate_kwargs or {}).get("extra_body", {})
    assert extra_body.get("reasoning", {}).get("enabled") is True


def test_openrouter_reasoning_formatter_replays_reasoning_details() -> None:
    formatter = OpenRouterReasoningChatFormatter()
    reasoning_details = [{"type": "reasoning.encrypted", "data": "sig-123"}]
    msg = Msg(
        name="assistant",
        role="assistant",
        content=[
            ThinkingBlock(type="thinking", thinking="Plan next tool call."),
            ToolUseBlock(
                type="tool_use",
                id="call_reasoning",
                name="web_search",
                input={"query": "OpenRouter Kimi"},
            ),
        ],
        metadata={OPENROUTER_REASONING_DETAILS_KEY: reasoning_details},
    )

    formatted = asyncio.run(formatter.format([msg]))

    assert len(formatted) == 1
    assistant = formatted[0]
    assert assistant["role"] == "assistant"
    assert assistant.get("tool_calls")
    assert assistant.get("reasoning_details") == reasoning_details
    assert assistant.get("reasoning_content") is None


def test_deepseek_official_formatter_replays_reasoning_content_for_tool_calls() -> None:
    _, formatter = ModelFactory.create(
        "deepseek-v4-pro-high", api_key="test-deepseek-key"
    )
    msg = Msg(
        name="assistant",
        role="assistant",
        content=[
            ThinkingBlock(type="thinking", thinking="Need to call the tool."),
            ToolUseBlock(
                type="tool_use",
                id="call_deepseek_reasoning",
                name="write_file",
                input={"path": "/workspace/demo.md", "content": "hello"},
            ),
        ],
    )

    formatted = asyncio.run(formatter.format([msg]))

    assert len(formatted) == 1
    assistant = formatted[0]
    assert assistant["role"] == "assistant"
    assert assistant.get("tool_calls")
    assert assistant.get("content") == ""
    assert assistant.get("reasoning_content") == "Need to call the tool."


def test_openrouter_kimi_hybrid_formatter_replays_reasoning_details_and_content() -> (
    None
):
    formatter = OpenRouterKimiHybridFormatter()
    reasoning_details = [{"type": "reasoning.encrypted", "data": "sig-456"}]
    msg = Msg(
        name="assistant",
        role="assistant",
        content=[
            ThinkingBlock(type="thinking", thinking="Plan next tool call."),
            ToolUseBlock(
                type="tool_use",
                id="call_reasoning",
                name="write_file",
                input={"path": "/workspace/demo.md", "content": "hello"},
            ),
        ],
        metadata={OPENROUTER_REASONING_DETAILS_KEY: reasoning_details},
    )

    formatted = asyncio.run(formatter.format([msg]))

    assert len(formatted) == 1
    assistant = formatted[0]
    assert assistant["role"] == "assistant"
    assert assistant.get("tool_calls")
    assert assistant.get("reasoning_details") == reasoning_details
    assert assistant.get("reasoning_content") == "Plan next tool call."


def test_model_factory_supports_ppio_kimi_with_moonshot_formatter() -> None:
    model, formatter = ModelFactory.create("ppio-kimi-k2.5", api_key="test-ppio-key")

    assert isinstance(formatter, MoonshotChatFormatter)
    assert model.model_name == "moonshotai/kimi-k2.5"
    assert "api.ppio.com/openai" in getattr(model, "_configured_base_url", "")

    extra_body = (model.generate_kwargs or {}).get("extra_body", {})
    assert extra_body.get("reasoning") is None


def test_openrouter_kimi_alias_keeps_openrouter_canonical_id() -> None:
    assert (
        MODEL_NAME_ALIASES["openrouter/moonshotai/kimi-k2.5"]
        == "openrouter/moonshotai/kimi-k2.5"
    )


def test_ppio_kimi_alias_keeps_ppio_canonical_id() -> None:
    assert (
        MODEL_NAME_ALIASES["ppio/moonshotai/kimi-k2.5"] == "ppio/moonshotai/kimi-k2.5"
    )


def test_qwen36_alias_maps_to_openrouter_canonical_id() -> None:
    assert MODEL_NAME_ALIASES["qwen/qwen3.6-plus"] == "openrouter/qwen/qwen3.6-plus"
    assert MODEL_NAME_ALIASES["qwen3.6-plus"] == "openrouter/qwen/qwen3.6-plus"


def test_model_factory_supports_glm_51() -> None:
    model, formatter = ModelFactory.create("glm-5.1", api_key="test-openrouter-key")

    assert isinstance(formatter, OpenRouterReasoningChatFormatter)
    assert model.model_name == "z-ai/glm-5.1"
    assert "openrouter.ai" in getattr(model, "_configured_base_url", "")


def test_model_factory_routes_openrouter_qwen36_through_openrouter() -> None:
    model, formatter = ModelFactory.create_from_full_name(
        "openrouter/qwen/qwen3.6-plus",
        api_key="test-openrouter-key",
    )

    assert isinstance(formatter, OpenRouterReasoningChatFormatter)
    assert not isinstance(formatter, DashScopeChatFormatter)
    assert not isinstance(model, DashScopeQwenNativeChatModel)
    assert model.model_name == "qwen/qwen3.6-plus"
    assert "openrouter.ai" in getattr(model, "_configured_base_url", "")


def test_model_factory_routes_openrouter_kimi_full_name_to_hybrid_formatter() -> None:
    model, formatter = ModelFactory.create_from_full_name(
        "openrouter/moonshotai/kimi-k2.5",
        api_key="test-openrouter-key",
    )

    assert model.model_name == "moonshotai/kimi-k2.5"
    assert "openrouter.ai" in getattr(model, "_configured_base_url", "")
    assert isinstance(formatter, OpenRouterKimiHybridFormatter)


def test_model_factory_routes_official_kimi_k26_alias_to_moonshot_provider() -> None:
    model, formatter = ModelFactory.create_from_full_name(
        "moonshotai/kimi-k2.6",
        api_key="test-moonshot-key",
    )

    assert model.model_name == "kimi-k2.6"
    assert "api.moonshot.cn" in getattr(model, "_configured_base_url", "")
    assert isinstance(formatter, MoonshotChatFormatter)


def test_model_factory_supports_glm5_and_minimax_m25() -> None:
    glm_model, glm_formatter = ModelFactory.create(
        "glm-5", api_key="test-openrouter-key"
    )
    minimax_model, minimax_formatter = ModelFactory.create(
        "minimax-m2.5", api_key="test-dashscope-key"
    )

    assert glm_model.model_name == "z-ai/glm-5"
    assert minimax_model.model_name == "MiniMax-M2.5"
    assert "dashscope.aliyuncs.com/compatible-mode/v1" in getattr(
        minimax_model,
        "_configured_base_url",
        "",
    )
    extra_body = (minimax_model.generate_kwargs or {}).get("extra_body", {})
    assert extra_body.get("reasoning", {}).get("effort") == "high"
    assert isinstance(glm_formatter, OpenAIChatFormatter)
    assert isinstance(minimax_formatter, OpenAIChatFormatter)


def test_model_factory_supports_openrouter_minimax_m25_with_reasoning_enabled() -> None:
    minimax_model, minimax_formatter = ModelFactory.create(
        "openrouter-minimax-m2.5",
        api_key="test-openrouter-key",
    )

    assert minimax_model.model_name == "minimax/minimax-m2.5"
    assert "openrouter.ai" in getattr(minimax_model, "_configured_base_url", "")
    extra_body = (minimax_model.generate_kwargs or {}).get("extra_body", {})
    assert extra_body.get("reasoning", {}).get("enabled") is True
    assert extra_body.get("reasoning", {}).get("effort") == "high"
    assert isinstance(minimax_formatter, OpenRouterReasoningChatFormatter)


def test_model_factory_supports_openrouter_mimo_v25_pro_with_existing_shape() -> None:
    model, formatter = ModelFactory.create(
        "mimo-v2.5-pro",
        api_key="test-openrouter-key",
    )

    assert model.model_name == "xiaomi/mimo-v2.5-pro"
    assert "openrouter.ai" in getattr(model, "_configured_base_url", "")
    extra_body = (model.generate_kwargs or {}).get("extra_body", {})
    assert extra_body.get("reasoning", {}).get("enabled") is True
    assert extra_body.get("reasoning", {}).get("effort") == "high"


def test_model_factory_routes_mimo_v25_full_name_without_v2_prefix_collision() -> None:
    model, formatter = ModelFactory.create_from_full_name(
        "openrouter/xiaomi/mimo-v2.5-pro",
        api_key="test-openrouter-key",
    )

    assert isinstance(formatter, OpenRouterReasoningChatFormatter)
    assert model.model_name == "xiaomi/mimo-v2.5-pro"
    assert "openrouter.ai" in getattr(model, "_configured_base_url", "")
    assert isinstance(formatter, OpenRouterReasoningChatFormatter)


def test_model_factory_supports_openrouter_deepseek_v4_with_openrouter_shape() -> None:
    model, formatter = ModelFactory.create(
        "openrouter-deepseek-v4-pro",
        api_key="test-openrouter-key",
    )

    assert model.model_name == "deepseek/deepseek-v4-pro"
    assert "openrouter.ai" in getattr(model, "_configured_base_url", "")
    extra_body = (model.generate_kwargs or {}).get("extra_body", {})
    assert extra_body.get("reasoning", {}).get("enabled") is True
    assert extra_body.get("reasoning", {}).get("effort") == "high"
    assert "thinking" not in extra_body
    assert isinstance(formatter, OpenRouterReasoningChatFormatter)


def test_model_factory_applies_runtime_reasoning_effort_to_openrouter_deepseek_v4() -> (
    None
):
    model, _ = ModelFactory.create(
        "openrouter-deepseek-v4-pro",
        api_key="test-openrouter-key",
        reasoning_effort="max",
    )

    extra_body = (model.generate_kwargs or {}).get("extra_body", {})
    assert extra_body.get("reasoning", {}).get("enabled") is True
    assert extra_body.get("reasoning", {}).get("effort") == "max"


def test_model_factory_supports_official_deepseek_v4_with_thinking_shape(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-deepseek-key")

    model, formatter = ModelFactory.create("deepseek-v4-pro-max")

    assert model.model_name == "deepseek-v4-pro"
    assert "api.deepseek.com" in getattr(model, "_configured_base_url", "")
    generate_kwargs = model.generate_kwargs or {}
    assert generate_kwargs.get("reasoning_effort") == "max"
    assert generate_kwargs.get("extra_body") == {"thinking": {"type": "enabled"}}
    assert "reasoning" not in generate_kwargs.get("extra_body", {})
    assert isinstance(formatter, OpenAIChatFormatter)


def test_model_factory_supports_official_deepseek_base_url_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-deepseek-key")
    monkeypatch.setenv("DEEPSEEK_API_BASE", "https://deepseek.example/v1")

    model, _ = ModelFactory.create("deepseek-v4-flash-high")

    assert model.model_name == "deepseek-v4-flash"
    assert getattr(model, "_configured_base_url", "") == "https://deepseek.example/v1"
    assert (model.generate_kwargs or {}).get("reasoning_effort") == "high"


def test_model_factory_supports_dashscope_minimax_m25_backup_route() -> None:
    minimax_model, minimax_formatter = ModelFactory.create(
        "dashscope-minimax-m2.5",
        api_key="test-dashscope-key",
    )

    assert minimax_model.model_name == "MiniMax-M2.5"
    assert "dashscope.aliyuncs.com/compatible-mode/v1" in getattr(
        minimax_model,
        "_configured_base_url",
        "",
    )
    extra_body = (minimax_model.generate_kwargs or {}).get("extra_body", {})
    assert extra_body.get("reasoning", {}).get("enabled") is None
    assert extra_body.get("reasoning", {}).get("effort") == "high"
    assert isinstance(minimax_formatter, OpenAIChatFormatter)


def test_model_factory_routes_openrouter_minimax_m25_full_name_to_openrouter() -> None:
    minimax_model, minimax_formatter = ModelFactory.create_from_full_name(
        "openrouter/minimax/minimax-m2.5",
        api_key="test-openrouter-key",
    )

    assert minimax_model.model_name == "minimax/minimax-m2.5"
    assert "openrouter.ai" in getattr(
        minimax_model,
        "_configured_base_url",
        "",
    )
    extra_body = (minimax_model.generate_kwargs or {}).get("extra_body", {})
    assert extra_body.get("reasoning", {}).get("enabled") is True
    assert extra_body.get("reasoning", {}).get("effort") == "high"
    assert isinstance(minimax_formatter, OpenRouterReasoningChatFormatter)


def test_model_factory_create_from_full_name_accepts_trace_for_openrouter_models() -> (
    None
):
    fake_trace = object()

    minimax_model, _ = ModelFactory.create_from_full_name(
        "openrouter/minimax/minimax-m2.5",
        api_key="test-openrouter-key",
        trace=fake_trace,
    )

    assert getattr(minimax_model, "_lf_trace", None) is fake_trace


def test_model_factory_routes_dashscope_minimax_m25_full_name_to_backup_provider() -> (
    None
):
    minimax_model, minimax_formatter = ModelFactory.create_from_full_name(
        "dashscope/minimax-m2.5",
        api_key="test-dashscope-key",
    )

    assert minimax_model.model_name == "MiniMax-M2.5"
    assert "dashscope.aliyuncs.com/compatible-mode/v1" in getattr(
        minimax_model,
        "_configured_base_url",
        "",
    )
    extra_body = (minimax_model.generate_kwargs or {}).get("extra_body", {})
    assert extra_body.get("reasoning", {}).get("enabled") is None
    assert extra_body.get("reasoning", {}).get("effort") == "high"
    assert isinstance(minimax_formatter, OpenAIChatFormatter)


def test_model_factory_routes_ppio_kimi_full_name_to_ppio_provider() -> None:
    model, formatter = ModelFactory.create_from_full_name(
        "ppio/moonshotai/kimi-k2.5",
        api_key="test-ppio-key",
    )

    assert model.model_name == "moonshotai/kimi-k2.5"
    assert "api.ppio.com/openai" in getattr(model, "_configured_base_url", "")
    assert isinstance(formatter, MoonshotChatFormatter)


def test_model_factory_supports_dashscope_qwen35_plus_with_native_formatter() -> None:
    qwen_model, qwen_formatter = ModelFactory.create(
        "qwen3.5-plus",
        api_key="test-dashscope-key",
    )

    assert qwen_model.model_name == "qwen3.5-plus"
    assert isinstance(qwen_model, DashScopeQwenNativeChatModel)
    assert isinstance(qwen_model, DashScopeChatModel)
    assert qwen_model.stream is True
    assert getattr(qwen_model, "base_http_api_url", None) in (None, "")
    assert isinstance(qwen_formatter, DashScopeChatFormatter)
    assert not isinstance(qwen_formatter, MoonshotChatFormatter)


def test_model_factory_supports_dashscope_qwen35_plus_from_full_name_native() -> None:
    qwen_model, qwen_formatter = ModelFactory.create_from_full_name(
        "dashscope/qwen3.5-plus",
        api_key="test-dashscope-key",
    )

    assert isinstance(qwen_model, DashScopeQwenNativeChatModel)
    assert isinstance(qwen_model, DashScopeChatModel)
    assert qwen_model.model_name == "qwen3.5-plus"
    assert isinstance(qwen_formatter, DashScopeChatFormatter)


def test_model_factory_maps_legacy_qwen35_to_qwen35_plus() -> None:
    qwen_model, qwen_formatter = ModelFactory.create_from_full_name(
        "dashscope/qwen3.5-397b-a17b",
        api_key="test-dashscope-key",
    )

    assert isinstance(qwen_model, DashScopeQwenNativeChatModel)
    assert isinstance(qwen_model, DashScopeChatModel)
    assert qwen_model.model_name == "qwen3.5-plus"
    assert isinstance(qwen_formatter, DashScopeChatFormatter)


def test_dashscope_qwen_generate_kwargs_contract() -> None:
    model, _ = ModelFactory.create(
        "qwen3.5-plus",
        api_key="test-dashscope-key",
        stream=False,
    )

    assert model.enable_thinking is True


def test_model_factory_allows_dashscope_native_base_url_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "DASHSCOPE_BASE_URL",
        "https://dashscope.aliyuncs.com/api/v1",
    )
    model, _ = ModelFactory.create(
        "qwen3.5-plus",
        api_key="test-dashscope-key",
    )

    assert isinstance(model, DashScopeChatModel)
    assert model.model_name == "qwen3.5-plus"


def test_model_factory_supports_volcengine_doubao_models() -> None:
    model, formatter = ModelFactory.create(
        "doubao-seed-2-0-pro-260215",
        api_key="test-volc-key",
    )

    assert model.model_name == "doubao-seed-2-0-pro-260215"
    assert "volces.com" in getattr(model, "_configured_base_url", "")
    assert isinstance(formatter, OpenAIChatFormatter)


def test_model_factory_keeps_proxy_gpt_54_on_plain_streaming_contract() -> None:
    model, formatter = ModelFactory.create(
        "proxy-gpt-5.4",
        api_key="test-openai-proxy-key",
    )

    assert model.model_name == "gpt-5.4"
    assert "openai-proxy.org" in getattr(model, "_configured_base_url", "")
    assert isinstance(formatter, OpenAIChatFormatter)

    extra_body = (model.generate_kwargs or {}).get("extra_body", {})
    assert extra_body.get("reasoning", {}).get("enabled") is False
    assert extra_body.get("reasoning", {}).get("effort") is None


def test_deepseek_official_formatter_is_moonshot_chat_formatter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DeepSeek official API must use MoonshotChatFormatter (not OpenRouterReasoningChatFormatter).

    MoonshotChatFormatter was purpose-built for the 'thinking mode + tool calls +
    reasoning_content preservation' pattern.  DeepSeek uses the same
    ``reasoning_content`` convention, so this formatter is the right fit.
    """
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-deepseek-key")
    _, formatter = ModelFactory.create("deepseek-v4-pro-max")
    assert isinstance(
        formatter, MoonshotChatFormatter
    ), f"Expected MoonshotChatFormatter for deepseek-v4-pro-max, got {type(formatter).__name__}"
    assert not isinstance(
        formatter, OpenRouterReasoningChatFormatter
    ), "DeepSeek official API must NOT use OpenRouterReasoningChatFormatter"


def test_deepseek_formatter_multi_turn_tool_calls_preserves_reasoning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Simulate multi-turn: assistant(thinking+tool) -> tool -> user -> assistant(thinking).

    Every assistant message with tool_calls must carry reasoning_content per DeepSeek docs.
    """
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-deepseek-key")
    _, formatter = ModelFactory.create("deepseek-v4-pro-max")

    call_id_1 = "call_multi_1"
    msgs = [
        Msg(
            name="user",
            role="user",
            content=[
                TextBlock(type="text", text="What's the weather in Hangzhou tomorrow?")
            ],
        ),
        Msg(
            name="assistant",
            role="assistant",
            content=[
                ThinkingBlock(
                    type="thinking", thinking="Need to get tomorrow's date first."
                ),
                ToolUseBlock(
                    type="tool_use",
                    id=call_id_1,
                    name="get_date",
                    input={},
                ),
            ],
        ),
        Msg(
            name="tool",
            role="assistant",
            content=[
                ToolResultBlock(
                    type="tool_result",
                    id=call_id_1,
                    name="get_date",
                    output=[TextBlock(type="text", text="2026-05-10")],
                ),
            ],
        ),
        Msg(
            name="assistant",
            role="assistant",
            content=[
                ThinkingBlock(
                    type="thinking",
                    thinking="Now call weather for Hangzhou on 2026-05-10.",
                ),
                TextBlock(type="text", text="Let me check the weather."),
            ],
        ),
        Msg(
            name="user",
            role="user",
            content=[TextBlock(type="text", text="How about Guangzhou?")],
        ),
    ]

    formatted = asyncio.run(formatter.format(msgs))

    assistant_msgs = [m for m in formatted if m.get("role") == "assistant"]
    for idx, am in enumerate(assistant_msgs):
        if am.get("tool_calls"):
            assert am.get("reasoning_content"), (
                f"Assistant message {idx} with tool_calls missing reasoning_content. "
                f"Keys present: {sorted(am.keys())}"
            )

    assert len(assistant_msgs) >= 2


def test_moonshot_formatter_strips_reasoning_for_non_tool_call_assistant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DeepSeek docs: reasoning_content from non-tool-call turns is ignored
    by the API and MUST be stripped to save context.

    Scenario: user → assistant(thinking, NO tools) → user (next turn)
    The assistant message between user messages has NO tool_calls, so
    its reasoning_content should be stripped.
    """
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-deepseek-key")
    _, formatter = ModelFactory.create("deepseek-v4-pro-max")

    msgs = [
        Msg(
            name="user",
            role="user",
            content=[TextBlock(type="text", text="Hello")],
        ),
        Msg(
            name="assistant",
            role="assistant",
            content=[
                ThinkingBlock(
                    type="thinking", thinking="Just a simple greeting, no tools needed."
                ),
                TextBlock(type="text", text="Hi! How can I help?"),
            ],
        ),
        Msg(
            name="user",
            role="user",
            content=[TextBlock(type="text", text="What's the weather?")],
        ),
    ]

    formatted = asyncio.run(formatter.format(msgs))

    assistant_msgs = [m for m in formatted if m.get("role") == "assistant"]
    assert len(assistant_msgs) == 1
    assert (
        "reasoning_content" not in assistant_msgs[0]
    ), "reasoning_content must be stripped from non-tool-call assistant messages"


def test_moonshot_formatter_preserves_reasoning_for_tool_call_chain_then_strips_final(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Full DeepSeek tool-call flow:
    user → assistant(thinking+tool) → tool → assistant(thinking+tool) → tool
         → assistant(thinking, NO tools) → user (next question)

    Assistant msgs WITH tool_calls MUST keep reasoning_content.
    Final assistant msg WITHOUT tool_calls MUST strip reasoning_content.
    """
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-deepseek-key")
    _, formatter = ModelFactory.create("deepseek-v4-pro-max")

    call_id_1 = "call_strip_1"
    call_id_2 = "call_strip_2"
    msgs = [
        Msg(
            name="user",
            role="user",
            content=[TextBlock(type="text", text="What's 2+2?")],
        ),
        # First tool call
        Msg(
            name="assistant",
            role="assistant",
            content=[
                ThinkingBlock(type="thinking", thinking="Need to calculate."),
                ToolUseBlock(
                    type="tool_use",
                    id=call_id_1,
                    name="execute_command",
                    input={"command": "echo $((2+2))"},
                ),
            ],
        ),
        Msg(
            name="tool",
            role="assistant",
            content=[
                ToolResultBlock(
                    type="tool_result",
                    id=call_id_1,
                    name="execute_command",
                    output=[TextBlock(type="text", text="4")],
                ),
            ],
        ),
        # Second tool call
        Msg(
            name="assistant",
            role="assistant",
            content=[
                ThinkingBlock(type="thinking", thinking="Verify with Python too."),
                ToolUseBlock(
                    type="tool_use",
                    id=call_id_2,
                    name="execute_command",
                    input={"command": "python3 -c 'print(2+2)'"},
                ),
            ],
        ),
        Msg(
            name="tool",
            role="assistant",
            content=[
                ToolResultBlock(
                    type="tool_result",
                    id=call_id_2,
                    name="execute_command",
                    output=[TextBlock(type="text", text="4")],
                ),
            ],
        ),
        # Final answer — NO tool calls
        Msg(
            name="assistant",
            role="assistant",
            content=[
                ThinkingBlock(type="thinking", thinking="Both confirmed 4. Wrap up."),
                TextBlock(type="text", text="2+2 = 4"),
            ],
        ),
        # Next user turn
        Msg(
            name="user",
            role="user",
            content=[TextBlock(type="text", text="Now what's 3+3?")],
        ),
    ]

    formatted = asyncio.run(formatter.format(msgs))

    assistant_msgs = [m for m in formatted if m.get("role") == "assistant"]
    assert (
        len(assistant_msgs) == 3
    ), f"Expected 3 assistant msgs, got {len(assistant_msgs)}"

    # First two assistant msgs have tool_calls → MUST keep reasoning_content
    for idx in range(2):
        assert assistant_msgs[idx].get(
            "tool_calls"
        ), f"Assistant msg {idx} should have tool_calls"
        assert assistant_msgs[idx].get(
            "reasoning_content"
        ), f"Assistant msg {idx} with tool_calls missing reasoning_content"

    # Third assistant msg has NO tool_calls → MUST strip reasoning_content
    assert not assistant_msgs[2].get(
        "tool_calls"
    ), "Third assistant msg should NOT have tool_calls"
    assert (
        "reasoning_content" not in assistant_msgs[2]
    ), "reasoning_content must be stripped from non-tool-call assistant messages"


def test_model_aliases_include_new_models() -> None:
    assert MODEL_NAME_ALIASES["openrouter/z-ai/glm-5"] == "openrouter/z-ai/glm-5"
    assert MODEL_NAME_ALIASES["z-ai/glm-5"] == "openrouter/z-ai/glm-5"
    assert (
        MODEL_NAME_ALIASES["openrouter/minimax/minimax-m2.5"]
        == "openrouter/minimax/minimax-m2.5"
    )
    assert (
        MODEL_NAME_ALIASES["minimax/minimax-m2.5"] == "openrouter/minimax/minimax-m2.5"
    )
    assert MODEL_NAME_ALIASES["dashscope/minimax-m2.5"] == "dashscope/minimax-m2.5"
    assert MODEL_NAME_ALIASES["minimax-m2.5"] == "dashscope/minimax-m2.5"
    assert MODEL_NAME_ALIASES["MiniMax-M2.5"] == "dashscope/minimax-m2.5"
    assert MODEL_NAME_ALIASES["dashscope/qwen3.5-plus"] == "dashscope/qwen3.5-plus"
    assert MODEL_NAME_ALIASES["qwen/qwen3.5-plus"] == "dashscope/qwen3.5-plus"
    assert (
        MODEL_NAME_ALIASES["openrouter/qwen/qwen3.5-plus"] == "dashscope/qwen3.5-plus"
    )
    assert MODEL_NAME_ALIASES["dashscope/qwen3.5-397b-a17b"] == "dashscope/qwen3.5-plus"
    assert MODEL_NAME_ALIASES["qwen/qwen3.5-397b-a17b"] == "dashscope/qwen3.5-plus"
    assert (
        MODEL_NAME_ALIASES["openrouter/qwen/qwen3.5-397b-a17b"]
        == "dashscope/qwen3.5-plus"
    )
    assert (
        MODEL_NAME_ALIASES["doubao-seed-2-0-pro-260215"]
        == "volcengine/doubao-seed-2-0-pro-260215"
    )


def test_official_deepseek_formatter_serializes_tool_results_as_provider_tool_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Local tool-result Msgs may use user role, but API payload must be role=tool."""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-deepseek-key")
    _, formatter = ModelFactory.create("deepseek-v4-pro-max")

    msgs = [
        Msg(
            name="assistant",
            role="assistant",
            content=[
                ThinkingBlock(type="thinking", thinking="Need file contents."),
                ToolUseBlock(
                    type="tool_use",
                    id="call-read",
                    name="read_file",
                    input={"path": "a.py"},
                ),
            ],
        ),
        Msg(
            name="tool",
            role="user",
            content=[
                ToolResultBlock(
                    type="tool_result",
                    id="call-read",
                    name="read_file",
                    output=[TextBlock(type="text", text="print('hi')")],
                ),
            ],
        ),
    ]

    formatted = asyncio.run(formatter.format(msgs))

    assistant = formatted[0]
    assert assistant["role"] == "assistant"
    assert assistant["tool_calls"][0]["function"]["name"] == "read_file"
    assert assistant["reasoning_content"] == "Need file contents."
    tool = formatted[1]
    assert tool["role"] == "tool"
    assert tool["tool_call_id"] == "call-read"
    assert tool["name"] == "read_file"
    assert "print('hi')" in tool["content"]
