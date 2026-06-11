import asyncio
import sys
import types
from pathlib import Path

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
    postgresql_mod = types.ModuleType("services.postgresql")

    class _DummyDBConnection:
        @property
        async def client(self):
            raise RuntimeError("DB client is not available in this unit test")

    postgresql_mod.DBConnection = _DummyDBConnection
    services_pkg.postgresql = postgresql_mod
    sys.modules["services"] = services_pkg
    sys.modules["services.postgresql"] = postgresql_mod

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

from agentscope.model import ChatModelBase
from agentscope_integration.models.global_fallback_model import GlobalFallbackChatModel


class _FailingModel(ChatModelBase):
    async def __call__(self, *_args, **_kwargs):
        raise RuntimeError("primary failed")


class _FallbackModel(ChatModelBase):
    def __init__(self, model_name: str, stream: bool = False):
        super().__init__(model_name=model_name, stream=stream)
        self.last_messages = None

    async def __call__(self, messages, *_args, **_kwargs):
        self.last_messages = messages
        if self.stream:
            async def _gen():
                yield {"fallback": "chunk"}

            return _gen()
        return {"fallback": True}


class _BrokenStreamingModel(ChatModelBase):
    async def __call__(self, *_args, **_kwargs):
        async def _gen():
            yield {"primary": "chunk"}
            raise RuntimeError("stream failed")

        return _gen()


class _FlakyModel(ChatModelBase):
    def __init__(
        self,
        model_name: str,
        stream: bool,
        failures_before_success: int,
    ) -> None:
        super().__init__(model_name=model_name, stream=stream)
        self.failures_before_success = failures_before_success
        self.calls = 0

    async def __call__(self, *_args, **_kwargs):
        self.calls += 1
        if self.calls <= self.failures_before_success:
            raise RuntimeError(f"flaky failure #{self.calls}")

        if self.stream:
            async def _gen():
                yield {"primary": "ok"}

            return _gen()

        return {"primary": True}


def test_global_fallback_model_retries_non_stream_request() -> None:
    fallback = _FallbackModel("z-ai/glm-4.7", stream=False)
    wrapper = GlobalFallbackChatModel(
        primary_model=_FailingModel("qwen3.5-plus", stream=False),
        primary_model_key="qwen3.5-plus",
        fallback_model_key="glm-4.7",
        fallback_factory=lambda _stream: fallback,
    )

    result = asyncio.run(
        wrapper(
            [{"role": "assistant", "reasoning_content": "keep", "content": "ok"}],
        )
    )

    assert result == {"fallback": True}
    assert fallback.last_messages[0].get("reasoning_content") is None


def test_global_fallback_model_handles_streaming_failure() -> None:
    wrapper = GlobalFallbackChatModel(
        primary_model=_BrokenStreamingModel("qwen3.5-plus", stream=True),
        primary_model_key="qwen3.5-plus",
        fallback_model_key="glm-4.7",
        fallback_factory=lambda _stream: _FallbackModel("z-ai/glm-4.7", stream=True),
    )

    async def _collect() -> list[dict]:
        response = await wrapper([{"role": "user", "content": "hello"}])
        chunks = []
        async for chunk in response:
            chunks.append(chunk)
        return chunks

    chunks = asyncio.run(_collect())
    assert chunks == [{"primary": "chunk"}, {"fallback": "chunk"}]


def test_global_fallback_model_retries_primary_before_fallback() -> None:
    primary = _FlakyModel(
        "qwen3.5-plus",
        stream=False,
        failures_before_success=2,
    )
    fallback = _FallbackModel("z-ai/glm-4.7", stream=False)
    wrapper = GlobalFallbackChatModel(
        primary_model=primary,
        primary_model_key="qwen3.5-plus",
        fallback_model_key="glm-4.7",
        fallback_factory=lambda _stream: fallback,
        primary_max_retries=2,
    )

    result = asyncio.run(wrapper([{"role": "user", "content": "hello"}]))

    assert result == {"primary": True}
    assert primary.calls == 3
    assert fallback.last_messages is None


def test_global_fallback_model_raises_stream_error_after_output_when_disabled() -> None:
    wrapper = GlobalFallbackChatModel(
        primary_model=_BrokenStreamingModel("qwen3.5-plus", stream=True),
        primary_model_key="qwen3.5-plus",
        fallback_model_key="glm-4.7",
        fallback_factory=lambda _stream: _FallbackModel("z-ai/glm-4.7", stream=True),
        fallback_on_stream_error_after_output=False,
    )

    async def _collect():
        response = await wrapper([{"role": "user", "content": "hello"}])
        chunks = []
        async for chunk in response:
            chunks.append(chunk)
        return chunks

    try:
        asyncio.run(_collect())
    except RuntimeError as exc:
        assert "stream failed" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("Expected RuntimeError when fallback-after-output is disabled")


def test_global_fallback_model_skips_self_fallback() -> None:
    wrapper = GlobalFallbackChatModel(
        primary_model=_FailingModel("z-ai/glm-4.7", stream=False),
        primary_model_key="glm-4.7",
        fallback_model_key="glm-4.7",
        fallback_factory=lambda _stream: _FallbackModel("z-ai/glm-4.7", stream=False),
    )

    try:
        asyncio.run(wrapper([{"role": "user", "content": "hello"}]))
    except RuntimeError as exc:
        assert "primary failed" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("Expected RuntimeError when fallback key equals primary key")
