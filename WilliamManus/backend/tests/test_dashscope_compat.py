import asyncio
import sys
import types
from http import HTTPStatus
from pathlib import Path

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

from agentscope_integration.models import dashscope_compat
from agentscope_integration.models.dashscope_compat import (
    _normalize_stream_line,
    apply_dashscope_streamreader_compat_patch,
)


class _ReaderLine:
    def __init__(self, value: bytes = b"data:hello\n") -> None:
        self._value = value

    async def readline(self):
        return self._value


class _ReaderOnlyRead:
    def __init__(self, value: bytes = b"data:hello\n") -> None:
        self._value = value

    async def read(self):
        return self._value


class _AsyncIterable:
    def __init__(self, values):
        self._values = list(values)

    def __aiter__(self):
        self._iter = iter(self._values)
        return self

    async def __anext__(self):
        try:
            return next(self._iter)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


class _FakeResponse:
    def __init__(self, lines):
        self.content = _AsyncIterable(lines)


class _SyncResponse:
    def __init__(self, lines):
        self._lines = list(lines)

    def iter_lines(self):
        return iter(self._lines)


async def _collect_stream_events(stream_fn, response):
    events = []
    async for item in stream_fn(response):
        events.append(item)
    return events


def test_normalize_stream_line_accepts_bytes_and_str() -> None:
    assert asyncio.run(_normalize_stream_line(b"data: hi\n")) == "data: hi\n"
    assert asyncio.run(_normalize_stream_line("data: hi\n")) == "data: hi\n"


def test_normalize_stream_line_reads_reader_objects() -> None:
    assert asyncio.run(_normalize_stream_line(_ReaderLine())) == "data:hello\n"


def test_apply_patch_updates_common_and_http_request_alias(
    monkeypatch,
) -> None:
    import dashscope.api_entities.http_request as dashscope_http_request
    import dashscope.client.base_api as dashscope_base_api
    import dashscope.common.utils as dashscope_common_utils

    original_common_fn = dashscope_common_utils._handle_aio_stream
    original_common_stream_fn = dashscope_common_utils._handle_stream
    original_http_alias_fn = dashscope_http_request._handle_aio_stream
    original_stream_event_handle = dashscope_base_api.StreamEventMixin.__dict__["_handle_stream"]

    monkeypatch.setattr(
        dashscope_common_utils,
        "_handle_aio_stream",
        original_common_fn,
        raising=False,
    )
    monkeypatch.setattr(
        dashscope_common_utils,
        "_handle_stream",
        original_common_stream_fn,
        raising=False,
    )
    monkeypatch.setattr(
        dashscope_http_request,
        "_handle_aio_stream",
        original_http_alias_fn,
        raising=False,
    )
    monkeypatch.setattr(
        dashscope_base_api.StreamEventMixin,
        "_handle_stream",
        original_stream_event_handle,
        raising=False,
    )
    monkeypatch.setattr(dashscope_compat, "_PATCH_APPLIED", False)

    assert apply_dashscope_streamreader_compat_patch() is True
    assert dashscope_common_utils._handle_aio_stream is dashscope_http_request._handle_aio_stream
    assert dashscope_common_utils._handle_aio_stream is not original_common_fn
    assert dashscope_common_utils._handle_stream is not original_common_stream_fn
    assert (
        dashscope_base_api.StreamEventMixin.__dict__["_handle_stream"]
        is not original_stream_event_handle
    )


def test_apply_patch_is_idempotent(
    monkeypatch,
) -> None:
    import dashscope.common.utils as dashscope_common_utils

    monkeypatch.setattr(dashscope_compat, "_PATCH_APPLIED", False)

    assert apply_dashscope_streamreader_compat_patch() is True
    patched_fn = dashscope_common_utils._handle_aio_stream

    assert apply_dashscope_streamreader_compat_patch() is True
    assert dashscope_common_utils._handle_aio_stream is patched_fn


def test_patched_aio_stream_supports_reader_line_objects(
    monkeypatch,
) -> None:
    import dashscope.common.utils as dashscope_common_utils

    monkeypatch.setattr(dashscope_compat, "_PATCH_APPLIED", False)
    assert apply_dashscope_streamreader_compat_patch() is True

    response = _FakeResponse(
        [
            _ReaderLine(b"status: 200\n"),
            _ReaderOnlyRead(b"data:{\"ok\":true}\n"),
        ],
    )

    events = asyncio.run(
        _collect_stream_events(
            dashscope_common_utils._handle_aio_stream,
            response,
        ),
    )

    assert len(events) == 1
    assert events[0][0] is False
    assert events[0][1] == HTTPStatus.OK
    assert events[0][2] == '{"ok":true}'


def test_patched_sync_stream_supports_reader_line_objects(
    monkeypatch,
) -> None:
    import dashscope.common.utils as dashscope_common_utils

    monkeypatch.setattr(dashscope_compat, "_PATCH_APPLIED", False)
    assert apply_dashscope_streamreader_compat_patch() is True

    response = _SyncResponse(
        [
            _ReaderLine(b"id: 1\n"),
            _ReaderLine(b"event: message\n"),
            _ReaderLine(b"status: 200\n"),
            _ReaderOnlyRead(b"data:{\"ok\":true}\n"),
        ],
    )

    events = list(dashscope_common_utils._handle_stream(response))

    assert len(events) == 1
    assert events[0][0] is False
    assert events[0][1] == HTTPStatus.OK
    event = events[0][2]
    assert event.id == "1"
    assert event.eventType == "message"
    assert event.data == '{"ok":true}'


def test_patched_stream_event_mixin_supports_reader_line_objects(
    monkeypatch,
) -> None:
    import dashscope.client.base_api as dashscope_base_api

    monkeypatch.setattr(dashscope_compat, "_PATCH_APPLIED", False)
    assert apply_dashscope_streamreader_compat_patch() is True

    response = _SyncResponse(
        [
            _ReaderLine(b"status: 200\n"),
            _ReaderOnlyRead(b"data:{\"ok\":true}\n"),
        ],
    )

    events = list(dashscope_base_api.StreamEventMixin._handle_stream(response))

    assert len(events) == 1
    assert events[0][0] is False
    assert events[0][1] == HTTPStatus.OK
    assert events[0][2] == '{"ok":true}'
