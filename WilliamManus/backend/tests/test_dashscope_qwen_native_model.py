import asyncio
import json
import sys
import types
from collections.abc import Mapping
from datetime import datetime
from http import HTTPStatus
from pathlib import Path

import pytest

# Minimal stubs for optional logging dependency during import.
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

# Register services submodule stubs before services package to avoid
# "not a package" import errors from the openrouter_model import chain.
if "services.eval_runtime_registry" not in sys.modules:
    eval_registry_mod = types.ModuleType("services.eval_runtime_registry")

    def _noop_record(*args, **kwargs):
        return None

    eval_registry_mod.record_model_generation = _noop_record
    eval_registry_mod.record_ttft_seconds = _noop_record
    sys.modules["services.eval_runtime_registry"] = eval_registry_mod

if "services.postgresql" not in sys.modules:
    postgresql_mod = types.ModuleType("services.postgresql")

    class _DummyDBConnection:
        @property
        async def client(self):
            raise RuntimeError("DB client is not available in this unit test")

    postgresql_mod.DBConnection = _DummyDBConnection
    sys.modules["services.postgresql"] = postgresql_mod

if "services" not in sys.modules:
    services_pkg = types.ModuleType("services")
    services_pkg.__path__ = []  # mark as package for submodule imports
    sys.modules["services"] = services_pkg
elif not hasattr(sys.modules["services"], "__path__"):
    sys.modules["services"].__path__ = []

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from agentscope_integration.models.dashscope_qwen_native_model import (
    DashScopeQwenNativeChatModel,
)
from agentscope_integration.models.model_factory import ModelFactory


def test_model_factory_uses_qwen_native_wrapper() -> None:
    model, _ = ModelFactory.create("qwen3.5-plus", api_key="test-key")
    assert isinstance(model, DashScopeQwenNativeChatModel)
    assert model.model_name == "qwen3.5-plus"


def test_model_factory_dashscope_native_base_defaults_without_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("DASHSCOPE_BASE_URL", raising=False)
    monkeypatch.delenv("DASHSCOPE_NATIVE_BASE_URL", raising=False)
    monkeypatch.delenv("DASHSCOPE_HTTP_BASE_URL", raising=False)
    monkeypatch.delenv("DASHSCOPE_API_BASE", raising=False)
    monkeypatch.setenv("AGENTSCOPE_QWEN_DASHSCOPE_STRICT_NATIVE", "true")

    _api_key, base_url, base_source = ModelFactory._resolve_provider_connection(
        provider="dashscope",
        api_key="test-key",
    )

    assert base_url == ModelFactory.DASHSCOPE_BASE_URL
    assert base_source == "default_native"


def test_model_factory_ignores_compatible_mode_endpoint_in_strict_native(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "DASHSCOPE_BASE_URL",
        "https://dashscope.aliyuncs.com/compatible-mode/v1",
    )
    monkeypatch.setenv("AGENTSCOPE_QWEN_DASHSCOPE_STRICT_NATIVE", "true")

    _api_key, base_url, base_source = ModelFactory._resolve_provider_connection(
        provider="dashscope",
        api_key="test-key",
    )

    assert base_url == ModelFactory.DASHSCOPE_BASE_URL
    assert base_source == "default_native"


def test_qwen35_model_text_only_uses_aio_multimodal_path(monkeypatch) -> None:
    import dashscope

    model = DashScopeQwenNativeChatModel(
        model_name="qwen3.5-plus",
        api_key="test-key",
        stream=False,
    )

    called = {}

    async def _fake_aio_multimodal_call(*, api_key, **kwargs):
        called["aio_multimodal"] = {"api_key": api_key, "kwargs": kwargs}
        return object()

    async def _fake_parse_response(_start_datetime, _response, _structured_model=None):
        return {"ok": True}

    monkeypatch.setattr(
        dashscope.AioMultiModalConversation,
        "call",
        _fake_aio_multimodal_call,
    )
    monkeypatch.setattr(
        model,
        "_parse_dashscope_generation_response",
        _fake_parse_response,
    )

    result = asyncio.run(model([{"role": "user", "content": "hello"}]))

    assert result == {"ok": True}
    assert "aio_multimodal" in called
    assert called["aio_multimodal"]["api_key"] == "test-key"
    assert called["aio_multimodal"]["kwargs"]["model"] == "qwen3.5-plus"


def test_qwen35_heals_dashscope_base_before_multimodal_call(monkeypatch) -> None:
    import dashscope

    model = DashScopeQwenNativeChatModel(
        model_name="qwen3.5-plus",
        api_key="test-key",
        stream=False,
        base_http_api_url="https://dashscope.aliyuncs.com/api/v1",
    )
    dashscope.base_http_api_url = "https://example.invalid/api/v1"

    called = {}

    async def _fake_aio_multimodal_call(*, api_key, **kwargs):
        _ = (api_key, kwargs)
        called["base_during_call"] = dashscope.base_http_api_url
        return object()

    async def _fake_parse_response(_start_datetime, _response, _structured_model=None):
        return {"ok": True}

    monkeypatch.setattr(
        dashscope.AioMultiModalConversation,
        "call",
        _fake_aio_multimodal_call,
    )
    monkeypatch.setattr(
        model,
        "_parse_dashscope_generation_response",
        _fake_parse_response,
    )

    result = asyncio.run(model([{"role": "user", "content": "hello"}]))

    assert result == {"ok": True}
    assert called["base_during_call"] == "https://dashscope.aliyuncs.com/api/v1"


def test_qwen35_image_grounding_uses_aio_multimodal_path(monkeypatch) -> None:
    import dashscope

    model = DashScopeQwenNativeChatModel(
        model_name="qwen3.5-plus",
        api_key="test-key",
        stream=True,
    )

    called = {}

    async def _fake_aio_multimodal_call(*, api_key, **kwargs):
        called["aio_multimodal"] = {"api_key": api_key, "kwargs": kwargs}
        return object()

    async def _fake_stream():
        yield {"chunk": 1}
        yield {"chunk": 2}

    def _fake_parse_stream_response(_start_datetime, _response, _structured_model=None):
        called["parse_stream"] = True
        return _fake_stream()

    monkeypatch.setattr(
        dashscope.AioMultiModalConversation,
        "call",
        _fake_aio_multimodal_call,
    )
    monkeypatch.setattr(
        model,
        "_parse_dashscope_stream_response",
        _fake_parse_stream_response,
    )

    stream_result = asyncio.run(
        model(
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "analyze this"},
                        {"type": "image", "image": "base64://demo"},
                    ],
                },
            ],
            tool_choice="none",
        ),
    )

    async def _collect(stream):
        chunks = []
        async for item in stream:
            chunks.append(item)
        return chunks

    chunks = asyncio.run(_collect(stream_result))

    assert called["aio_multimodal"]["api_key"] == "test-key"
    assert called["aio_multimodal"]["kwargs"]["model"] == "qwen3.5-plus"
    assert called.get("parse_stream") is True
    assert chunks == [{"chunk": 1}, {"chunk": 2}]


def test_qwen35_sanitizes_unmatched_tool_calls_before_request(monkeypatch) -> None:
    import dashscope

    model = DashScopeQwenNativeChatModel(
        model_name="qwen3.5-plus",
        api_key="test-key",
        stream=False,
    )

    called = {}

    async def _fake_aio_multimodal_call(*, api_key, **kwargs):
        called["aio_multimodal"] = {"api_key": api_key, "kwargs": kwargs}
        return object()

    async def _fake_parse_response(_start_datetime, _response, _structured_model=None):
        return {"ok": True}

    monkeypatch.setattr(
        dashscope.AioMultiModalConversation,
        "call",
        _fake_aio_multimodal_call,
    )
    monkeypatch.setattr(
        model,
        "_parse_dashscope_generation_response",
        _fake_parse_response,
    )

    result = asyncio.run(
        model(
            [
                {"role": "system", "content": "You are helpful."},
                {
                    "role": "assistant",
                    "content": "I will inspect the image",
                    "tool_calls": [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {
                                "name": "browser_inspect",
                                "arguments": "{}",
                            },
                        },
                        {
                            "id": "message[11].role",
                            "type": "function",
                            "function": {
                                "name": "browser_inspect",
                                "arguments": "{}",
                            },
                        },
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call-1",
                    "name": "browser_inspect",
                    "content": "done",
                },
            ],
        ),
    )

    assert result == {"ok": True}
    request_messages = called["aio_multimodal"]["kwargs"]["messages"]
    assistant = request_messages[1]
    assert assistant["role"] == "assistant"
    assert len(assistant["tool_calls"]) == 1
    assert assistant["tool_calls"][0]["id"] == "call-1"


def test_qwen35_tool_choice_none_omits_tools_payload(monkeypatch) -> None:
    import dashscope

    model = DashScopeQwenNativeChatModel(
        model_name="qwen3.5-plus",
        api_key="test-key",
        stream=False,
    )

    called = {}

    async def _fake_aio_multimodal_call(*, api_key, **kwargs):
        called["aio_multimodal"] = {"api_key": api_key, "kwargs": kwargs}
        return object()

    async def _fake_parse_response(_start_datetime, _response, _structured_model=None):
        return {"ok": True}

    monkeypatch.setattr(
        dashscope.AioMultiModalConversation,
        "call",
        _fake_aio_multimodal_call,
    )
    monkeypatch.setattr(
        model,
        "_parse_dashscope_generation_response",
        _fake_parse_response,
    )

    result = asyncio.run(
        model(
            [{"role": "user", "content": "hello"}],
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "dummy_tool",
                        "description": "dummy",
                        "parameters": {"type": "object"},
                    },
                }
            ],
            tool_choice="none",
        ),
    )

    assert result == {"ok": True}
    request_kwargs = called["aio_multimodal"]["kwargs"]
    assert request_kwargs["tool_choice"] == "none"
    assert "tools" not in request_kwargs


def test_qwen35_image_tools_disabled_without_native_tool_flag(monkeypatch) -> None:
    monkeypatch.setenv("AGENTSCOPE_QWEN_NATIVE_MULTIMODAL_TOOL_CALLS_ENABLED", "false")

    model = DashScopeQwenNativeChatModel(
        model_name="qwen3.5-plus",
        api_key="test-key",
        stream=False,
    )

    with pytest.raises(RuntimeError) as exc_info:
        asyncio.run(
            model(
                [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "replicate this ui"},
                            {"type": "image", "image": "base64://demo"},
                        ],
                    },
                ],
                tools=[
                    {
                        "type": "function",
                        "function": {
                            "name": "write_file",
                            "description": "write file",
                            "parameters": {"type": "object"},
                        },
                    },
                ],
                tool_choice="auto",
            ),
        )
    assert "image+tools execution is disabled" in str(exc_info.value)


def test_non_qwen_model_keeps_generation_path(monkeypatch) -> None:
    import dashscope

    model = DashScopeQwenNativeChatModel(
        model_name="qwen-max",
        api_key="test-key",
        stream=False,
    )

    called = {}

    async def _fake_aio_multimodal_call(*, api_key, **kwargs):
        called["aio_multimodal"] = {"api_key": api_key, "kwargs": kwargs}
        return object()

    def _fake_sync_multimodal_call(*, api_key, **kwargs):
        called["sync_multimodal"] = {"api_key": api_key, "kwargs": kwargs}
        return object()

    async def _fake_generation_call(*_args, **_kwargs):
        called["generation"] = True
        return object()

    async def _fake_parse_response(_start_datetime, _response, _structured_model=None):
        return {"ok": True}

    monkeypatch.setattr(
        dashscope.AioMultiModalConversation,
        "call",
        _fake_aio_multimodal_call,
    )
    monkeypatch.setattr(
        dashscope.MultiModalConversation,
        "call",
        _fake_sync_multimodal_call,
    )
    monkeypatch.setattr(
        dashscope.aigc.generation.AioGeneration,
        "call",
        _fake_generation_call,
    )
    monkeypatch.setattr(
        model,
        "_parse_dashscope_generation_response",
        _fake_parse_response,
    )

    result = asyncio.run(model([{"role": "user", "content": "hello"}]))

    assert result == {"ok": True}
    assert "generation" in called
    assert "aio_multimodal" not in called
    assert "sync_multimodal" not in called


class _ChunkMessage(dict):
    @property
    def content(self):
        return self.get("content")


def _build_tool_stream_chunk(arguments: str, *, finish_reason: str | None = None):
    message = _ChunkMessage(
        {
            "content": "",
            "tool_calls": [
                {
                    "index": 0,
                    "id": "call-1",
                    "function": {
                        "name": "write_file",
                        "arguments": arguments,
                    },
                }
            ],
        },
    )
    return types.SimpleNamespace(
        status_code=HTTPStatus.OK,
        output=types.SimpleNamespace(
            choices=[types.SimpleNamespace(message=message, finish_reason=finish_reason)],
        ),
        usage=None,
    )


def _extract_tool_use_block(parsed_chunk):
    for block in parsed_chunk.content:
        if isinstance(block, dict):
            if block.get("type") == "tool_use":
                return dict(block)
            continue

        block_type = getattr(block, "type", None)
        if block_type != "tool_use":
            continue
        block_input = getattr(block, "input", None)
        if isinstance(block_input, Mapping):
            return {
                "type": "tool_use",
                "id": getattr(block, "id", ""),
                "name": getattr(block, "name", ""),
                "input": dict(block_input),
            }
    return {}


def _extract_tool_use_input(parsed_chunk):
    block = _extract_tool_use_block(parsed_chunk)
    block_input = block.get("input")
    if isinstance(block_input, Mapping):
        return dict(block_input)
    return {}


def test_qwen_stream_parser_handles_cumulative_tool_arguments() -> None:
    model = DashScopeQwenNativeChatModel(
        model_name="qwen3.5-plus",
        api_key="test-key",
        stream=True,
    )

    chunks = [
        _build_tool_stream_chunk(
            '{"path":"/workspace/main.tsx","content":"line1"}',
        ),
        _build_tool_stream_chunk(
            '{"path":"/workspace/main.tsx","content":"line1\\nline2"}',
        ),
    ]

    async def _run():
        async def _stream():
            for chunk in chunks:
                yield chunk

        last = None
        async for parsed in model._parse_dashscope_stream_response(
            datetime.now(),
            _stream(),
        ):
            last = parsed
        return last

    last_chunk = asyncio.run(_run())
    assert last_chunk is not None
    tool_input = _extract_tool_use_input(last_chunk)
    assert tool_input, tool_input
    assert tool_input.get("path") == "/workspace/main.tsx" or tool_input.get("file_path") == "/workspace/main.tsx", tool_input
    assert tool_input["content"] == "line1\nline2"


def test_qwen_stream_parser_recovers_partial_write_file_arguments() -> None:
    model = DashScopeQwenNativeChatModel(
        model_name="qwen3.5-plus",
        api_key="test-key",
        stream=True,
    )

    chunk = _build_tool_stream_chunk(
        '{"path":"/workspace/main.tsx","content":"const x = 1',
    )

    async def _run():
        async def _stream():
            yield chunk

        async for parsed in model._parse_dashscope_stream_response(
            datetime.now(),
            _stream(),
        ):
            return parsed
        return None

    parsed_chunk = asyncio.run(_run())
    assert parsed_chunk is not None
    tool_input = _extract_tool_use_input(parsed_chunk)
    assert tool_input, tool_input
    assert tool_input.get("path") == "/workspace/main.tsx" or tool_input.get("file_path") == "/workspace/main.tsx", tool_input
    assert tool_input["content"] == "const x = 1"


def test_qwen_stream_parser_recovers_unescaped_html_quotes_in_write_content() -> None:
    model = DashScopeQwenNativeChatModel(
        model_name="qwen3.5-plus",
        api_key="test-key",
        stream=True,
    )

    chunk = _build_tool_stream_chunk(
        '{"path":"/workspace/index.html","content":"<meta name="viewport" content="width=device-width">\\n<div class="hero">Hi</div>"}',
    )

    async def _run():
        async def _stream():
            yield chunk

        async for parsed in model._parse_dashscope_stream_response(
            datetime.now(),
            _stream(),
        ):
            return parsed
        return None

    parsed_chunk = asyncio.run(_run())
    assert parsed_chunk is not None
    tool_input = _extract_tool_use_input(parsed_chunk)
    assert tool_input, tool_input
    assert tool_input.get("path") == "/workspace/index.html"
    assert 'name="viewport"' in tool_input["content"], tool_input["content"]
    assert 'class="hero"' in tool_input["content"], tool_input["content"]


def test_qwen_stream_parser_keeps_growing_content_with_unescaped_quotes() -> None:
    model = DashScopeQwenNativeChatModel(
        model_name="qwen3.5-plus",
        api_key="test-key",
        stream=True,
    )

    chunks = [
        _build_tool_stream_chunk(
            '{"path":"/workspace/index.html","content":"<meta name="viewport" content="width=device-width">\\n<div class="hero">',
        ),
        _build_tool_stream_chunk(
            '{"path":"/workspace/index.html","content":"<meta name="viewport" content="width=device-width">\\n<div class="hero">Hello</div></body></html>"}',
        ),
    ]

    async def _run():
        async def _stream():
            for chunk in chunks:
                yield chunk

        last = None
        async for parsed in model._parse_dashscope_stream_response(
            datetime.now(),
            _stream(),
        ):
            last = parsed
        return last

    last_chunk = asyncio.run(_run())
    assert last_chunk is not None
    tool_input = _extract_tool_use_input(last_chunk)
    assert tool_input, tool_input
    assert tool_input.get("path") == "/workspace/index.html"
    assert tool_input["content"].endswith("Hello</div></body></html>"), tool_input["content"]


def test_qwen_stream_parser_sanitizes_unexpected_write_file_keys() -> None:
    model = DashScopeQwenNativeChatModel(
        model_name="qwen3.5-plus",
        api_key="test-key",
        stream=True,
    )

    chunk = _build_tool_stream_chunk(
        json.dumps(
            {
                "path": "/workspace/index.html",
                "content": "<a href=\"https://example.com\">demo</a>",
                'href="https': "",
                "unexpected_field": "drop-me",
                "append": True,
            },
            ensure_ascii=False,
        )
    )

    async def _run():
        async def _stream():
            yield chunk

        async for parsed in model._parse_dashscope_stream_response(
            datetime.now(),
            _stream(),
        ):
            return parsed
        return None

    parsed_chunk = asyncio.run(_run())
    assert parsed_chunk is not None
    tool_input = _extract_tool_use_input(parsed_chunk)
    assert tool_input["path"] == "/workspace/index.html"
    assert tool_input["content"] == "<a href=\"https://example.com\">demo</a>"
    assert "unexpected_field" not in tool_input
    assert 'href="https' not in tool_input
    assert tool_input["append"] is True


def test_qwen_stream_parser_marks_raw_preview_on_incomplete_write_chunk() -> None:
    model = DashScopeQwenNativeChatModel(
        model_name="qwen3.5-plus",
        api_key="test-key",
        stream=True,
    )
    raw_arguments = '{"path":"/workspace/main.tsx","content":"const x = 1'
    chunk = _build_tool_stream_chunk(raw_arguments)

    async def _run():
        async def _stream():
            yield chunk

        async for parsed in model._parse_dashscope_stream_response(
            datetime.now(),
            _stream(),
        ):
            return parsed
        return None

    parsed_chunk = asyncio.run(_run())
    assert parsed_chunk is not None
    tool_block = _extract_tool_use_block(parsed_chunk)
    assert tool_block.get("arguments_source") == "raw_preview"
    assert tool_block.get("raw_arguments") == raw_arguments


def test_qwen_stream_parser_applies_final_repair_on_terminal_write_chunk() -> None:
    model = DashScopeQwenNativeChatModel(
        model_name="qwen3.5-plus",
        api_key="test-key",
        stream=True,
    )
    raw_arguments = '{"path":"/workspace/main.tsx","content":"const x = 1'
    chunks = [
        _build_tool_stream_chunk(raw_arguments),
        _build_tool_stream_chunk(raw_arguments, finish_reason="tool_calls"),
    ]

    async def _run():
        async def _stream():
            for chunk in chunks:
                yield chunk

        last = None
        async for parsed in model._parse_dashscope_stream_response(
            datetime.now(),
            _stream(),
        ):
            last = parsed
        return last

    parsed_chunk = asyncio.run(_run())
    assert parsed_chunk is not None
    tool_block = _extract_tool_use_block(parsed_chunk)
    assert tool_block.get("arguments_source") == "final_repair"
    tool_input = tool_block.get("input", {})
    assert isinstance(tool_input, dict)
    assert tool_input.get("path") == "/workspace/main.tsx"


def test_non_streaming_response_metadata_includes_kv_cache_prompt_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After a non-streaming call, ChatResponse.metadata must include
    kv_cache_prompt_tokens so the auto-compact hook can read the real
    input token count (mirrors OpenRouterChatModel behavior)."""
    import dashscope
    from agentscope.message import TextBlock
    from agentscope.model._model_response import ChatResponse, ChatUsage

    model = DashScopeQwenNativeChatModel(
        model_name="qwen3.5-plus",
        api_key="test-key",
        stream=False,
    )

    async def _fake_aio_multimodal_call(*, api_key, **kwargs):
        _ = (api_key, kwargs)
        return object()

    async def _fake_parse_response(
        _start_datetime, _response, _structured_model=None
    ):
        return ChatResponse(
            content=[TextBlock(type="text", text="Hello!")],
            usage=ChatUsage(input_tokens=150, output_tokens=50, time=1.0),
            metadata=None,
        )

    monkeypatch.setattr(
        dashscope.AioMultiModalConversation,
        "call",
        _fake_aio_multimodal_call,
    )
    monkeypatch.setattr(
        model,
        "_parse_dashscope_generation_response",
        _fake_parse_response,
    )

    result = asyncio.run(model([{"role": "user", "content": "hello"}]))

    assert isinstance(result, ChatResponse)
    assert result.metadata is not None, "metadata must be set after enrichment"
    assert result.metadata.get("kv_cache_prompt_tokens") == 150, (
        "kv_cache_prompt_tokens must match usage.input_tokens"
    )


def test_non_streaming_preserves_existing_metadata_when_adding_token_counts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When metadata already exists (e.g. from structured_model), token
    counts must be merged without clobbering existing keys."""
    import dashscope
    from agentscope.message import TextBlock
    from agentscope.model._model_response import ChatResponse, ChatUsage

    model = DashScopeQwenNativeChatModel(
        model_name="qwen3.5-plus",
        api_key="test-key",
        stream=False,
    )

    async def _fake_aio_multimodal_call(*, api_key, **kwargs):
        _ = (api_key, kwargs)
        return object()

    async def _fake_parse_response(
        _start_datetime, _response, _structured_model=None
    ):
        return ChatResponse(
            content=[TextBlock(type="text", text="ok")],
            usage=ChatUsage(input_tokens=300, output_tokens=80, time=2.0),
            metadata={"existing_key": "preserved"},
        )

    monkeypatch.setattr(
        dashscope.AioMultiModalConversation,
        "call",
        _fake_aio_multimodal_call,
    )
    monkeypatch.setattr(
        model,
        "_parse_dashscope_generation_response",
        _fake_parse_response,
    )

    result = asyncio.run(model([{"role": "user", "content": "hi"}]))

    assert result.metadata.get("existing_key") == "preserved"
    assert result.metadata.get("kv_cache_prompt_tokens") == 300


def test_streaming_response_enriches_final_chunk_with_kv_cache_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The last stream chunk (which carries usage info) must include
    kv_cache_prompt_tokens in its metadata for the auto-compact hook."""
    import dashscope
    from agentscope.message import TextBlock
    from agentscope.model._model_response import ChatResponse, ChatUsage

    model = DashScopeQwenNativeChatModel(
        model_name="qwen3.5-plus",
        api_key="test-key",
        stream=True,
    )

    async def _fake_aio_multimodal_call(*, api_key, **kwargs):
        _ = (api_key, kwargs)
        return object()

    async def _fake_stream():
        yield ChatResponse(
            content=[TextBlock(type="text", text="Hello")],
            usage=None,
            metadata=None,
        )
        yield ChatResponse(
            content=[TextBlock(type="text", text=" world!")],
            usage=ChatUsage(input_tokens=200, output_tokens=100, time=2.0),
            metadata=None,
        )

    def _fake_parse_stream_response(
        _start_datetime, _response, _structured_model=None
    ):
        return _fake_stream()

    monkeypatch.setattr(
        dashscope.AioMultiModalConversation,
        "call",
        _fake_aio_multimodal_call,
    )
    monkeypatch.setattr(
        model,
        "_parse_dashscope_stream_response",
        _fake_parse_stream_response,
    )

    async def _collect():
        chunks: list[ChatResponse] = []
        result_stream = await model(
            [{"role": "user", "content": "hello"}],
        )
        async for chunk in result_stream:
            chunks.append(chunk)
        return chunks

    chunks = asyncio.run(_collect())

    assert len(chunks) >= 2
    assert chunks[0].metadata is None, (
        "non-final chunks should have no metadata"
    )
    assert chunks[-1].metadata is not None, (
        "final chunk must have enriched metadata"
    )
    assert chunks[-1].metadata.get("kv_cache_prompt_tokens") == 200
