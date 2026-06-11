import importlib.util
import asyncio
import json
import sys
import types
from datetime import datetime
from pathlib import Path
from typing import Mapping

from pydantic import BaseModel

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))


def _ensure_real_eval_runtime_registry() -> None:
    """Load the real eval registry module even if another test stubbed services."""
    registry_module = sys.modules.get("services.eval_runtime_registry")
    if getattr(registry_module, "__file__", None):
        return

    services_pkg = sys.modules.get("services")
    if services_pkg is None:
        services_pkg = types.ModuleType("services")
        sys.modules["services"] = services_pkg

    service_path = str(BACKEND_ROOT / "services")
    package_path = list(getattr(services_pkg, "__path__", []))
    if service_path not in package_path:
        package_path.append(service_path)
    services_pkg.__path__ = package_path

    module_path = BACKEND_ROOT / "services" / "eval_runtime_registry.py"
    spec = importlib.util.spec_from_file_location(
        "services.eval_runtime_registry",
        module_path,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["services.eval_runtime_registry"] = module
    spec.loader.exec_module(module)


_ensure_real_eval_runtime_registry()

from services.eval_runtime_registry import pop_eval_run, start_eval_run

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

MODULE_PATH = BACKEND_ROOT / "agentscope_integration" / "models" / "openrouter_model.py"
spec = importlib.util.spec_from_file_location("openrouter_model_module", MODULE_PATH)
module = importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules["openrouter_model_module"] = module
spec.loader.exec_module(module)

SSE_MODULE_PATH = (
    BACKEND_ROOT / "agentscope_integration" / "streaming" / "sse_adapter.py"
)
sse_spec = importlib.util.spec_from_file_location(
    "openrouter_sse_adapter_module", SSE_MODULE_PATH
)
sse_module = importlib.util.module_from_spec(sse_spec)
assert sse_spec and sse_spec.loader
sys.modules["openrouter_sse_adapter_module"] = sse_module
sse_spec.loader.exec_module(sse_module)

_extract_partial_write_file_input = module._extract_partial_write_file_input
_merge_tool_arguments = module._merge_tool_arguments
OpenRouterChatModel = module.OpenRouterChatModel
OpenAIChatModel = module.OpenAIChatModel
APIError = module.APIError
SSEAdapter = sse_module.SSEAdapter


class _CompressionSummary(BaseModel):
    research_question: str = ""
    sources_consulted: str = ""
    key_findings: str = ""


class _DummyAsyncStream:
    def __init__(self, items):
        self._items = items
        self._index = 0

    async def __aenter__(self):
        self._index = 0
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._index >= len(self._items):
            raise StopAsyncIteration
        item = self._items[self._index]
        self._index += 1
        return item


class _DummyMsg:
    def __init__(
        self,
        *,
        role: str = "assistant",
        blocks: list[dict] | None = None,
        text: str = "",
    ) -> None:
        self.role = role
        self._blocks = blocks or []
        self._text = text

    def get_content_blocks(self) -> list[dict]:
        return self._blocks

    def get_text_content(self) -> str:
        return self._text


def _new_model_for_structured_parsing() -> OpenRouterChatModel:
    model = OpenRouterChatModel.__new__(OpenRouterChatModel)
    model.generate_kwargs = {}
    model._kv_cache_metrics_enabled = False
    return model


def _build_completion_response(*, content: str, parsed: BaseModel | None):
    message = types.SimpleNamespace(
        reasoning=None,
        reasoning_content=None,
        content=content,
        audio=None,
        tool_calls=[],
        parsed=parsed,
    )
    return types.SimpleNamespace(
        choices=[types.SimpleNamespace(message=message)],
        usage=None,
    )


def _build_stream_chunk_item(content: str):
    delta = types.SimpleNamespace(
        reasoning=None,
        reasoning_content=None,
        content=content,
        audio=None,
        tool_calls=None,
        parsed=None,
    )
    chunk = types.SimpleNamespace(
        usage=None,
        choices=[types.SimpleNamespace(delta=delta)],
    )
    return types.SimpleNamespace(type="chunk", chunk=chunk)


def _build_tool_stream_chunk(
    arguments: str,
    *,
    name: str = "write_file",
    index: int = 0,
    tool_call_id: str = "call-1",
):
    function = types.SimpleNamespace(
        name=name,
        arguments=arguments,
    )
    tool_call = types.SimpleNamespace(
        index=index,
        id=tool_call_id,
        function=function,
    )
    delta = types.SimpleNamespace(
        reasoning=None,
        reasoning_content=None,
        content=None,
        audio=None,
        tool_calls=[tool_call],
        parsed=None,
    )
    return types.SimpleNamespace(
        usage=None,
        choices=[types.SimpleNamespace(delta=delta)],
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
                "raw_arguments": getattr(block, "raw_arguments", None),
                "arguments_source": getattr(block, "arguments_source", None),
            }
    return {}


def _extract_tool_use_input(parsed_chunk):
    block = _extract_tool_use_block(parsed_chunk)
    block_input = block.get("input")
    if isinstance(block_input, Mapping):
        return dict(block_input)
    return {}


def test_merge_tool_arguments_handles_cumulative_chunks() -> None:
    merged = ""
    chunks = [
        '{"file_path":"/workspace/demo.md","content":"R"',
        '{"file_path":"/workspace/demo.md","content":"Ro"',
        '{"file_path":"/workspace/demo.md","content":"Roys"}',
    ]

    for chunk in chunks:
        merged = _merge_tool_arguments(merged, chunk)

    assert merged == chunks[-1]


def test_merge_tool_arguments_handles_delta_chunks() -> None:
    merged = ""
    chunks = [
        '{"file_path":"/workspace/demo.md","content":"R',
        "oys",
        ' Alpha"}',
    ]

    for chunk in chunks:
        merged = _merge_tool_arguments(merged, chunk)

    assert merged == '{"file_path":"/workspace/demo.md","content":"Roys Alpha"}'


def test_merge_tool_arguments_preserves_true_delta_repeated_boundary_chars() -> None:
    merged = ""
    chunks = [
        '{"file_path":"/workspace/045',
        '5.md","content":"occur',
        'rences and new Chart()"}',
    ]

    for chunk in chunks:
        merged = _merge_tool_arguments(merged, chunk)

    assert merged == (
        '{"file_path":"/workspace/0455.md",'
        '"content":"occurrences and new Chart()"}'
    )


def test_extract_partial_write_file_input_from_incomplete_json() -> None:
    partial = _extract_partial_write_file_input(
        '{"file_path":"/workspace/demo.md","content":"Roys\\nAlp',
    )

    assert partial["file_path"] == "/workspace/demo.md"
    assert partial["content"] == "Roys\nAlp"


def test_extract_partial_write_file_input_tolerates_trailing_escape() -> None:
    partial = _extract_partial_write_file_input('{"content":"Roys\\')

    assert partial["content"] == "Roys"


def test_extract_partial_write_file_input_decodes_tab_escape() -> None:
    partial = _extract_partial_write_file_input(
        '{"content":"if True:\\n\\tprint(1)',
    )

    assert partial["content"] == "if True:\n\tprint(1)"


def test_end_lf_generation_records_model_usage_in_eval_registry() -> None:
    from utils.agent_run_context import set_agent_run_context, clear_agent_run_context

    set_agent_run_context("trace-phase1", "thread-1")
    try:
        start_eval_run(run_id="trace-phase1", trace_id="trace-phase1", metadata={})
        model = OpenRouterChatModel.__new__(OpenRouterChatModel)
        model._lf_trace = types.SimpleNamespace(trace_id="trace-phase1")
        model.model_name = "qwen/qwen3.5-35b-a3b"
        # _coerce_int is a plain function (no self) — assign directly
        model._coerce_int = OpenRouterChatModel._coerce_int

        class _DummyGeneration:
            def __init__(self) -> None:
                self.update_calls: list = []
                self.ended = False

            def update(self, **kwargs):
                self.update_calls.append(kwargs)
                return self

            def end(self, **kwargs):
                self.ended = True

        generation = _DummyGeneration()
        response = types.SimpleNamespace(
            usage=types.SimpleNamespace(input_tokens=120, output_tokens=45),
            content="done",
        )

        model._end_lf_generation(generation, response)

        record = pop_eval_run("trace-phase1")
        assert record is not None
        assert record.model_generations == [
            {
                "model": "qwen/qwen3.5-35b-a3b",
                "input_tokens": 120,
                "output_tokens": 45,
            }
        ]
    finally:
        clear_agent_run_context()


def test_openrouter_stream_parser_recovers_unescaped_html_quotes_in_write_content() -> (
    None
):
    model = OpenRouterChatModel(
        model_name="minimax/minimax-m2.5",
        api_key="test-key",
        stream=True,
    )

    chunk = _build_tool_stream_chunk(
        '{"path":"/workspace/index.html","content":"<meta name="viewport" content="width=device-width">\\n<div class="hero">Hi</div>"}',
    )

    async def _run():
        async for parsed in model._parse_openai_stream_response(
            datetime.now(),
            _DummyAsyncStream([chunk]),
        ):
            return parsed
        return None

    parsed_chunk = asyncio.run(_run())
    assert parsed_chunk is not None
    tool_input = _extract_tool_use_input(parsed_chunk)
    assert tool_input["path"] == "/workspace/index.html"
    assert 'name="viewport"' in tool_input["content"], tool_input["content"]
    assert 'class="hero"' in tool_input["content"], tool_input["content"]


def test_openrouter_stream_parser_keeps_growing_write_content_with_unescaped_quotes() -> (
    None
):
    model = OpenRouterChatModel(
        model_name="minimax/minimax-m2.5",
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
        last = None
        async for parsed in model._parse_openai_stream_response(
            datetime.now(),
            _DummyAsyncStream(chunks),
        ):
            last = parsed
        return last

    last_chunk = asyncio.run(_run())
    assert last_chunk is not None
    tool_input = _extract_tool_use_input(last_chunk)
    assert tool_input["path"] == "/workspace/index.html"
    assert tool_input["content"].endswith("Hello</div></body></html>"), tool_input[
        "content"
    ]


def test_openrouter_stream_parser_sanitizes_unexpected_write_file_keys() -> None:
    model = OpenRouterChatModel(
        model_name="minimax/minimax-m2.5",
        api_key="test-key",
        stream=True,
    )

    chunk = _build_tool_stream_chunk(
        json.dumps(
            {
                "path": "/workspace/index.html",
                "content": '<a href="https://example.com">demo</a>',
                'href="https': "",
                "unexpected_field": "drop-me",
                "append": True,
            },
            ensure_ascii=False,
        ),
    )

    async def _run():
        async for parsed in model._parse_openai_stream_response(
            datetime.now(),
            _DummyAsyncStream([chunk]),
        ):
            return parsed
        return None

    parsed_chunk = asyncio.run(_run())
    assert parsed_chunk is not None
    tool_input = _extract_tool_use_input(parsed_chunk)
    assert tool_input["path"] == "/workspace/index.html"
    assert tool_input["content"] == '<a href="https://example.com">demo</a>'
    assert "unexpected_field" not in tool_input
    assert 'href="https' not in tool_input
    assert tool_input["append"] is True


def test_openrouter_stream_parser_preserves_repeated_chars_in_write_file_deltas() -> (
    None
):
    model = OpenRouterChatModel(
        model_name="deepseek-v4-pro",
        api_key="test-key",
        stream=True,
    )

    chunks = [
        _build_tool_stream_chunk('{"path":"/workspace/045'),
        _build_tool_stream_chunk('5.md","content":"occur'),
        _build_tool_stream_chunk('rences and new Chart()"}'),
    ]

    async def _run():
        last = None
        async for parsed in model._parse_openai_stream_response(
            datetime.now(),
            _DummyAsyncStream(chunks),
        ):
            last = parsed
        return last

    last_chunk = asyncio.run(_run())
    assert last_chunk is not None
    tool_input = _extract_tool_use_input(last_chunk)
    assert tool_input["path"] == "/workspace/0455.md"
    assert tool_input["content"] == "occurrences and new Chart()"


def test_openrouter_stream_parser_marks_raw_preview_on_incomplete_write_chunk() -> None:
    model = OpenRouterChatModel(
        model_name="minimax/minimax-m2.5",
        api_key="test-key",
        stream=True,
    )
    raw_arguments = '{"path":"/workspace/main.tsx","content":"const x = 1'
    chunk = _build_tool_stream_chunk(raw_arguments)

    async def _run():
        async for parsed in model._parse_openai_stream_response(
            datetime.now(),
            _DummyAsyncStream([chunk]),
        ):
            return parsed
        return None

    parsed_chunk = asyncio.run(_run())
    assert parsed_chunk is not None
    tool_block = _extract_tool_use_block(parsed_chunk)
    assert tool_block.get("arguments_source") == "raw_preview"
    assert tool_block.get("raw_arguments") == raw_arguments
    tool_input = tool_block.get("input", {})
    assert isinstance(tool_input, Mapping)
    assert tool_input.get("path") == "/workspace/main.tsx"
    assert tool_input.get("content") == "const x = 1"


def test_openrouter_stream_sse_metadata_prefers_raw_write_arguments() -> None:
    model = OpenRouterChatModel(
        model_name="minimax/minimax-m2.5",
        api_key="test-key",
        stream=True,
    )
    raw_arguments = '{"path":"/workspace/main.tsx","content":"const x = 1'
    chunk = _build_tool_stream_chunk(raw_arguments)

    async def _run():
        async for parsed in model._parse_openai_stream_response(
            datetime.now(),
            _DummyAsyncStream([chunk]),
        ):
            return parsed
        return None

    parsed_chunk = asyncio.run(_run())
    assert parsed_chunk is not None

    msg = _DummyMsg(blocks=[dict(block) for block in parsed_chunk.content], text="")
    payload = SSEAdapter("thread-1", "run-1").convert(msg, is_last=False)
    metadata = json.loads(payload["metadata"])
    tool_call = metadata["tool_calls"][0]

    assert tool_call["function"]["arguments"] == raw_arguments
    assert tool_call["trace"]["trace_args_source"] == "raw_preview"


def test_parse_completion_response_keeps_reasoning_details_metadata() -> None:
    model = _new_model_for_structured_parsing()
    reasoning_details = [{"type": "reasoning.encrypted", "data": "sig-abc"}]
    response = _build_completion_response(content="Hello", parsed=None)
    response.choices[0].message.reasoning = "Think first"
    response.choices[0].message.reasoning_details = reasoning_details

    parsed = model._parse_openai_completion_response(datetime.now(), response)

    assert parsed.metadata[module.OPENROUTER_REASONING_DETAILS_KEY] == reasoning_details


def test_dashscope_base_url_avoids_cross_provider_fallback() -> None:
    model = OpenRouterChatModel.__new__(OpenRouterChatModel)
    model._configured_base_url = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    model.model_name = "qwen3.5-plus"

    assert model._fallback_provider_model_name() == "qwen3.5-plus"


def test_wrap_streaming_response_retry_uses_base_call_without_runtime_error(
    monkeypatch,
) -> None:
    model = OpenRouterChatModel.__new__(OpenRouterChatModel)
    model.stream = True
    model.model_name = "qwen3.5-plus"
    model._configured_base_url = "https://openrouter.ai/api/v1"
    model._is_retriable_api_error = lambda _error: True
    model._fallback_provider_model_name = lambda: "z-ai/glm-4.7"

    async def _fake_base_call(self, *_args, **_kwargs):
        return {"fallback": True}

    monkeypatch.setattr(OpenAIChatModel, "__call__", _fake_base_call)

    async def _broken_stream():
        raise APIError(message="boom", request=None, body=None)
        yield {"never": "reached"}  # pragma: no cover

    async def _collect(gen):
        collected = []
        async for item in gen:
            collected.append(item)
        return collected

    wrapped = model._wrap_streaming_response(
        _broken_stream(),
        safe_messages=[{"role": "user", "content": "hi"}],
        tools=None,
        tool_choice=None,
        structured_model=None,
        kwargs={},
    )

    results = asyncio.run(_collect(wrapped))
    assert results == [{"fallback": True}]


def test_kv_cache_context_derives_stable_session_id() -> None:
    model = OpenRouterChatModel.__new__(OpenRouterChatModel)
    model._kv_cache_session_sticky = True
    model._kv_cache_context = {"thread_id": "", "session_id": ""}
    model._kv_cache_session_id = ""

    model.set_cache_context(thread_id="thread-1")
    first = model._kv_cache_session_id
    model.set_cache_context(thread_id="thread-1")
    second = model._kv_cache_session_id

    assert first
    assert first == second


def test_build_kv_cache_runtime_kwargs_adds_provider_payload() -> None:
    model = OpenRouterChatModel.__new__(OpenRouterChatModel)
    model._kv_cache_enabled = True
    model._kv_cache_provider_mode = "openrouter"
    model._kv_cache_breakpoint_mode = "auto"
    model._kv_cache_session_sticky = True
    model._kv_cache_session_ttl_seconds = 7200
    model._kv_cache_metrics_enabled = True
    model._kv_cache_shadow_log_only = False
    model._kv_cache_min_prefix_tokens = 2048
    model._kv_cache_contract_version = "v1"
    model._kv_cache_context = {"thread_id": "thread-1", "session_id": ""}
    model._kv_cache_session_id = model._derive_cache_session_id()

    runtime_kwargs, applied = model._build_kv_cache_runtime_kwargs({})

    assert applied is True
    assert (
        runtime_kwargs["extra_body"]["prompt_cache_key"] == model._kv_cache_session_id
    )
    assert runtime_kwargs["extra_body"]["cache_control"]["contract_version"] == "v1"
    assert (
        runtime_kwargs["extra_headers"]["x-agentscope-cache-session"]
        == model._kv_cache_session_id
    )


def test_extract_kv_cache_telemetry_from_usage_object() -> None:
    model = OpenRouterChatModel.__new__(OpenRouterChatModel)
    model._kv_cache_session_id = "abc123"
    model._kv_cache_enabled = True
    model._kv_cache_breakpoint_mode = "auto"
    model._kv_cache_contract_version = "v1"

    class _PromptDetails:
        cached_tokens = 300

    class _Usage:
        prompt_tokens = 1200
        completion_tokens = 90
        prompt_tokens_details = _PromptDetails()

    telemetry = model._extract_kv_cache_telemetry(_Usage())
    assert telemetry["kv_cache_cached_input_tokens"] == 300
    assert telemetry["kv_cache_prompt_tokens"] == 1200
    assert telemetry["kv_cache_hit_rate"] == 0.25


def test_ppio_base_url_disables_cross_model_fallback() -> None:
    model = OpenRouterChatModel.__new__(OpenRouterChatModel)
    model.model_name = "moonshotai/kimi-k2.5"
    model._configured_base_url = "https://api.ppio.com/openai"

    assert model._fallback_provider_model_name() == "moonshotai/kimi-k2.5"


def test_normalize_structured_metadata_backfills_missing_fields() -> None:
    model = _new_model_for_structured_parsing()
    metadata = model._normalize_structured_metadata(
        _CompressionSummary,
        parsed_obj={"key_findings": "Found signal"},
        raw_text="",
    )

    assert metadata is not None
    assert metadata["research_question"] == ""
    assert metadata["sources_consulted"] == ""
    assert metadata["key_findings"] == "Found signal"


def test_parse_completion_prefers_parsed_model_over_raw_content() -> None:
    model = _new_model_for_structured_parsing()
    parsed = _CompressionSummary(
        research_question="parsed question",
        sources_consulted="parsed source",
        key_findings="parsed finding",
    )
    response = _build_completion_response(
        content='{"research_question":"raw question"}',
        parsed=parsed,
    )

    parsed_response = model._parse_openai_completion_response(
        datetime.now(),
        response,
        structured_model=_CompressionSummary,
    )

    assert parsed_response.metadata is not None
    assert parsed_response.metadata["research_question"] == "parsed question"
    assert parsed_response.metadata["sources_consulted"] == "parsed source"
    assert parsed_response.metadata["key_findings"] == "parsed finding"


def test_parse_completion_backfills_missing_keys_for_template_format() -> None:
    model = _new_model_for_structured_parsing()
    response = _build_completion_response(
        content='{"research_question":"raw only"}',
        parsed=None,
    )

    parsed_response = model._parse_openai_completion_response(
        datetime.now(),
        response,
        structured_model=_CompressionSummary,
    )

    assert parsed_response.metadata is not None
    formatted = (
        "Q:{research_question}\n"
        "Sources:{sources_consulted}\n"
        "Findings:{key_findings}"
    ).format(**parsed_response.metadata)
    assert "Q:raw only" in formatted
    assert "Sources:" in formatted
    assert "Findings:" in formatted


def test_parse_stream_backfills_missing_keys_during_partial_json() -> None:
    model = _new_model_for_structured_parsing()
    stream_response = _DummyAsyncStream(
        [
            _build_stream_chunk_item('{"research_question":"partial"'),
            _build_stream_chunk_item(',"sources_consulted":"docs"}'),
        ],
    )

    async def _collect():
        responses = []
        async for chunk in model._parse_openai_stream_response(
            datetime.now(),
            stream_response,
            structured_model=_CompressionSummary,
        ):
            responses.append(chunk)
        return responses

    responses = asyncio.run(_collect())
    assert responses
    for response in responses:
        assert response.metadata is not None
        assert "research_question" in response.metadata
        assert "sources_consulted" in response.metadata
        assert "key_findings" in response.metadata


# ── Empty tool call → TextBlock injection tests ────────────────────────


def _extract_text_blocks(parsed_chunk) -> list[str]:
    """Return text content of all TextBlock items in a parsed chunk."""
    texts: list[str] = []
    for block in parsed_chunk.content:
        block_type = (
            block.get("type")
            if isinstance(block, dict)
            else getattr(block, "type", None)
        )
        if block_type == "text":
            texts.append(
                block.get("text")
                if isinstance(block, dict)
                else getattr(block, "text", "")
            )
    return texts


def _has_tool_use_block(parsed_chunk, tool_name: str) -> bool:
    """Check if a parsed chunk contains a ToolUseBlock with the given name."""
    for block in parsed_chunk.content:
        block_type = (
            block.get("type")
            if isinstance(block, dict)
            else getattr(block, "type", None)
        )
        if block_type == "tool_use":
            block_name = (
                block.get("name")
                if isinstance(block, dict)
                else getattr(block, "name", None)
            )
            if block_name == tool_name:
                return True
    return False


def test_post_stream_warns_and_keeps_empty_execute_command_tool_use_block() -> None:
    """Empty non-write tool calls get guidance and remain executable for tool-result feedback."""
    model = OpenRouterChatModel(
        model_name="deepseek/deepseek-chat",
        api_key="test-key",
        stream=True,
    )

    chunk = _build_tool_stream_chunk(
        "", name="execute_command", tool_call_id="call_empty_1"
    )

    async def _run():
        last = None
        async for parsed in model._parse_openai_stream_response(
            datetime.now(),
            _DummyAsyncStream([chunk]),
        ):
            last = parsed
        return last

    last_chunk = asyncio.run(_run())
    assert last_chunk is not None

    # Should contain a TextBlock with the warning
    texts = _extract_text_blocks(last_chunk)
    assert texts, "Expected at least one TextBlock in final response"
    warning_text = "".join(texts)
    assert "execute_command" in warning_text, warning_text
    assert "command" in warning_text, warning_text
    assert "required parameters" in warning_text.lower(), warning_text

    # Non-write tools should still reach the toolkit adapter, where the
    # pre-execution gate returns MISSING_REQUIRED_PARAMS as a ToolResponse.  If
    # the parser emits only TextBlock here, ReAct treats it as a final answer and
    # the model never gets a tool result to repair from.
    assert _has_tool_use_block(
        last_chunk, "execute_command"
    ), "Empty execute_command should keep a ToolUseBlock to continue the ReAct loop"


def test_post_stream_malformed_edit_file_yields_tool_use_block_to_keep_react_loop_alive() -> (
    None
):
    """Malformed edit_file calls must not become final assistant text only.

    DeepSeek/OpenRouter can stream an edit_file call containing only new_text
    after a failed execute_command diagnosis.  The toolkit adapter has the
    precise MISSING_REQUIRED_PARAMS feedback path, but it only runs if the model
    parser keeps the ToolUseBlock instead of converting the call to text-only
    final content.
    """
    model = OpenRouterChatModel(
        model_name="deepseek/deepseek-chat",
        api_key="test-key",
        stream=True,
    )

    chunk = _build_tool_stream_chunk(
        '{"new_text":"fixed docstring"}',
        name="edit_file",
        tool_call_id="call_bad_edit",
    )

    async def _run():
        last = None
        async for parsed in model._parse_openai_stream_response(
            datetime.now(),
            _DummyAsyncStream([chunk]),
        ):
            last = parsed
        return last

    last_chunk = asyncio.run(_run())
    assert last_chunk is not None

    texts = _extract_text_blocks(last_chunk)
    assert texts, "Expected inline guidance for malformed edit_file"
    warning_text = "".join(texts)
    assert "edit_file" in warning_text, warning_text
    assert "'path'" in warning_text, warning_text
    assert "'old_text'" in warning_text, warning_text
    assert _has_tool_use_block(
        last_chunk,
        "edit_file",
    ), "Malformed edit_file should keep a ToolUseBlock so ReAct receives tool feedback"


def test_post_stream_preserves_valid_tool_calls_alongside_empty_execute_command() -> (
    None
):
    """Valid write_file tool_use still yields normally when another tool call is empty."""
    model = OpenRouterChatModel(
        model_name="deepseek/deepseek-chat",
        api_key="test-key",
        stream=True,
    )

    # Two tool calls in the same stream: one valid write_file, one empty execute_command
    empty_cmd = _build_tool_stream_chunk(
        "", name="execute_command", index=0, tool_call_id="call_empty"
    )
    valid_write = _build_tool_stream_chunk(
        '{"path":"/workspace/test.txt","content":"hello"}',
        name="write_file",
        index=1,
        tool_call_id="call_write_1",
    )

    # Simulate a stream with both arriving in the same chunk (multi-tool response)
    chunk = types.SimpleNamespace(
        usage=None,
        choices=[
            types.SimpleNamespace(
                delta=types.SimpleNamespace(
                    reasoning=None,
                    reasoning_content=None,
                    content=None,
                    audio=None,
                    tool_calls=[
                        types.SimpleNamespace(
                            index=0,
                            id="call_empty",
                            function=types.SimpleNamespace(
                                name="execute_command", arguments=""
                            ),
                        ),
                        types.SimpleNamespace(
                            index=1,
                            id="call_write_1",
                            function=types.SimpleNamespace(
                                name="write_file",
                                arguments='{"path":"/workspace/test.txt","content":"hello"}',
                            ),
                        ),
                    ],
                    parsed=None,
                ),
            )
        ],
    )

    async def _run():
        all_chunks = []
        async for parsed in model._parse_openai_stream_response(
            datetime.now(),
            _DummyAsyncStream([chunk]),
        ):
            all_chunks.append(parsed)
        return all_chunks

    all_chunks = asyncio.run(_run())
    assert (
        len(all_chunks) >= 2
    ), f"Expected at least 2 chunks (mid-stream + post-stream), got {len(all_chunks)}"

    # The mid-stream chunk should contain the valid write_file ToolUseBlock
    mid_stream = all_chunks[0]
    assert _has_tool_use_block(
        mid_stream, "write_file"
    ), "Valid write_file tool call should be a ToolUseBlock in mid-stream yield"

    # The post-stream chunk should contain the TextBlock for empty execute_command
    post_stream = all_chunks[-1]
    texts = _extract_text_blocks(post_stream)
    warning_text = "".join(texts)
    assert (
        "execute_command" in warning_text
    ), f"Expected warning about execute_command in TextBlock, got: {warning_text}"

    # The empty execute_command should still be present in the post-stream
    # chunk so the toolkit adapter can return MISSING_REQUIRED_PARAMS.
    assert _has_tool_use_block(
        post_stream,
        "execute_command",
    ), "Empty non-write tool calls should remain ToolUseBlocks after warning text"


# ── Phase 1: Parsed-value check (catches {} and {"command":""}) ──────────


def test_post_stream_converts_empty_json_object_to_text_block() -> None:
    """Tool call with arguments='{}' → TextBlock, not ToolUseBlock."""
    model = OpenRouterChatModel(
        model_name="deepseek/deepseek-chat",
        api_key="test-key",
        stream=True,
    )

    chunk = _build_tool_stream_chunk(
        "{}", name="execute_command", tool_call_id="call_empty_json"
    )

    async def _run():
        last = None
        async for parsed in model._parse_openai_stream_response(
            datetime.now(),
            _DummyAsyncStream([chunk]),
        ):
            last = parsed
        return last

    last_chunk = asyncio.run(_run())
    assert last_chunk is not None

    texts = _extract_text_blocks(last_chunk)
    assert texts, "Expected at least one TextBlock for empty-object tool call"
    warning_text = "".join(texts)
    assert "execute_command" in warning_text, warning_text
    assert _has_tool_use_block(
        last_chunk, "execute_command"
    ), "Empty-object non-write tool call should keep ToolUseBlock for adapter feedback"


def test_post_stream_converts_empty_param_value_to_text_block() -> None:
    """Tool call with {"command":""} → TextBlock (parsed value is empty)."""
    model = OpenRouterChatModel(
        model_name="deepseek/deepseek-chat",
        api_key="test-key",
        stream=True,
    )

    chunk = _build_tool_stream_chunk(
        '{"command":""}', name="execute_command", tool_call_id="call_empty_value"
    )

    async def _run():
        last = None
        async for parsed in model._parse_openai_stream_response(
            datetime.now(),
            _DummyAsyncStream([chunk]),
        ):
            last = parsed
        return last

    last_chunk = asyncio.run(_run())
    assert last_chunk is not None

    # Should yield as a ToolUseBlock for local adapter rejection and tool-result
    # feedback; only write_file missing required params remains feedback-only.
    assert _has_tool_use_block(
        last_chunk, "execute_command"
    ), "Tool call with empty command value should keep ToolUseBlock"
    texts = _extract_text_blocks(last_chunk)
    assert texts, "Expected at least one TextBlock"
    warning_text = "".join(texts)
    assert "execute_command" in warning_text


def test_streaming_write_file_missing_path_never_yields_tool_use_block() -> None:
    """write_file with content but no path must not execute mid-stream.

    DeepSeek/OpenRouter may stream a large write_file payload through many
    incomplete snapshots.  If a snapshot only contains ``content`` and lacks
    ``path``, the parser must not emit an executable ToolUseBlock; otherwise the
    ReAct loop can repeatedly reject the same malformed write_file call until
    the harness timeout stops the run.
    """
    model = OpenRouterChatModel(
        model_name="deepseek/deepseek-chat",
        api_key="test-key",
        stream=True,
    )

    chunk = _build_tool_stream_chunk(
        '{"content":"print(1)"}',
        name="write_file",
        tool_call_id="call_write_missing_path",
    )

    async def _run():
        parsed_chunks = []
        async for parsed in model._parse_openai_stream_response(
            datetime.now(),
            _DummyAsyncStream([chunk]),
        ):
            parsed_chunks.append(parsed)
        return parsed_chunks

    parsed_chunks = asyncio.run(_run())
    assert parsed_chunks, "Expected parser to return feedback for malformed write_file"

    for parsed in parsed_chunks:
        assert not _has_tool_use_block(
            parsed,
            "write_file",
        ), "write_file missing path should be feedback-only, not executable"

    warning_text = "".join(
        text for parsed in parsed_chunks for text in _extract_text_blocks(parsed)
    )
    assert "write_file" in warning_text
    assert "'path'" in warning_text


def test_post_stream_preserves_valid_tool_call_with_non_empty_params() -> None:
    """Tool call with valid non-empty params still yields ToolUseBlock."""
    model = OpenRouterChatModel(
        model_name="deepseek/deepseek-chat",
        api_key="test-key",
        stream=True,
    )

    chunk = _build_tool_stream_chunk(
        '{"command":"ls -la"}', name="execute_command", tool_call_id="call_valid"
    )

    async def _run():
        last = None
        async for parsed in model._parse_openai_stream_response(
            datetime.now(),
            _DummyAsyncStream([chunk]),
        ):
            last = parsed
        return last

    last_chunk = asyncio.run(_run())
    assert last_chunk is not None
    assert _has_tool_use_block(
        last_chunk, "execute_command"
    ), "Valid execute_command with non-empty params should remain a ToolUseBlock"


def test_post_stream_tool_without_registered_params_yields_tool_use_block() -> None:
    """Tool not in _REQUIRED_TOOL_PARAMS yields ToolUseBlock even with minimal input."""
    model = OpenRouterChatModel(
        model_name="deepseek/deepseek-chat",
        api_key="test-key",
        stream=True,
    )

    # "screenshot" has no required params registered → should pass through
    chunk = _build_tool_stream_chunk(
        "{}", name="screenshot", tool_call_id="call_screenshot"
    )

    async def _run():
        last = None
        async for parsed in model._parse_openai_stream_response(
            datetime.now(),
            _DummyAsyncStream([chunk]),
        ):
            last = parsed
        return last

    last_chunk = asyncio.run(_run())
    assert last_chunk is not None
    assert _has_tool_use_block(
        last_chunk, "screenshot"
    ), "Tool without registered required params should yield ToolUseBlock"
