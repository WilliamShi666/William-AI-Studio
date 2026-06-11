"""DashScope SDK compatibility helpers for streaming responses."""

from __future__ import annotations

import asyncio
import inspect
from http import HTTPStatus
from typing import Any

from utils.logger import logger

_PATCH_APPLIED = False


async def _normalize_stream_line(raw_line: Any) -> str:
    """Normalize DashScope SSE lines across bytes/str/reader variants."""
    if raw_line is None:
        return ""

    if isinstance(raw_line, str):
        return raw_line

    if isinstance(raw_line, (bytes, bytearray, memoryview)):
        return bytes(raw_line).decode("utf8", errors="ignore")

    decode_fn = getattr(raw_line, "decode", None)
    if callable(decode_fn):
        try:
            return decode_fn("utf8")
        except TypeError:
            try:
                return decode_fn()
            except Exception:
                pass
        except Exception:
            pass

    for reader_name in ("readline", "read"):
        reader_fn = getattr(raw_line, reader_name, None)
        if not callable(reader_fn):
            continue
        try:
            value = await reader_fn()
        except Exception:
            continue

        if isinstance(value, str):
            return value
        if isinstance(value, (bytes, bytearray, memoryview)):
            return bytes(value).decode("utf8", errors="ignore")

    return str(raw_line)


def _normalize_stream_line_sync(raw_line: Any) -> str:
    """Normalize stream line objects for sync paths."""
    if raw_line is None:
        return ""

    if isinstance(raw_line, str):
        return raw_line

    if isinstance(raw_line, (bytes, bytearray, memoryview)):
        return bytes(raw_line).decode("utf8", errors="ignore")

    decode_fn = getattr(raw_line, "decode", None)
    if callable(decode_fn):
        try:
            return decode_fn("utf8")
        except TypeError:
            try:
                return decode_fn()
            except Exception:
                pass
        except Exception:
            pass

    for reader_name in ("readline", "read"):
        reader_fn = getattr(raw_line, reader_name, None)
        if not callable(reader_fn):
            continue
        try:
            value = reader_fn()
            if inspect.isawaitable(value):
                try:
                    asyncio.get_running_loop()
                except RuntimeError:
                    value = asyncio.run(value)
                else:
                    continue
        except Exception:
            continue

        if isinstance(value, str):
            return value
        if isinstance(value, (bytes, bytearray, memoryview)):
            return bytes(value).decode("utf8", errors="ignore")

    return str(raw_line)


def _source_has_decode_literal(func: Any) -> bool:
    """Best-effort diagnostic helper for patch verification logs."""
    try:
        src = inspect.getsource(func)
    except Exception:
        return False
    return '.decode("utf8")' in src or ".decode('utf8')" in src


def apply_dashscope_streamreader_compat_patch() -> bool:
    """Patch DashScope async SSE decoding for newer aiohttp stream objects."""
    global _PATCH_APPLIED
    if _PATCH_APPLIED:
        return True

    patched_targets: list[tuple[str, Any]] = []
    compat_aio_stream = None
    compat_stream_sync = None

    async def _handle_aio_stream_compat(response):
        is_error = False
        status_code = HTTPStatus.BAD_REQUEST

        async for raw_line in response.content:
            line = await _normalize_stream_line(raw_line)
            if not line:
                continue

            line = line.rstrip("\n").rstrip("\r")
            if line.startswith("event:error"):
                is_error = True
            elif line.startswith("status:"):
                try:
                    status_code = int(line[len("status:") :].strip())
                except (TypeError, ValueError):
                    continue
            elif line.startswith("data:"):
                payload = line[len("data:") :]
                yield (is_error, status_code, payload)
                if is_error:
                    break

    def _handle_stream_sync_compat(response):
        from dashscope.common.utils import SSEEvent

        is_error = False
        status_code = HTTPStatus.BAD_REQUEST
        event = SSEEvent(None, None, None)  # type: ignore[arg-type]
        event_type = None
        for raw_line in response.iter_lines():
            line = _normalize_stream_line_sync(raw_line)
            if not line:
                continue

            line = line.rstrip("\n").rstrip("\r")
            if line.startswith("id:"):
                event.id = line[len("id:") :].strip()
            elif line.startswith("event:"):
                event_type = line[len("event:") :].strip()
                event.eventType = event_type
                if event_type == "error":
                    is_error = True
            elif line.startswith("status:"):
                try:
                    status_code = int(line[len("status:") :].strip())
                except (TypeError, ValueError):
                    continue
            elif line.startswith("data:"):
                event.data = line[len("data:") :].strip()
                if event_type == "done":
                    continue
                yield (is_error, status_code, event)
                if is_error:
                    break

    try:
        import dashscope.common.utils as dashscope_common_utils

        dashscope_common_utils._handle_aio_stream = _handle_aio_stream_compat
        compat_aio_stream = dashscope_common_utils._handle_aio_stream
        patched_targets.append(
            ("dashscope.common.utils._handle_aio_stream", compat_aio_stream),
        )
        dashscope_common_utils._handle_stream = _handle_stream_sync_compat
        compat_stream_sync = dashscope_common_utils._handle_stream
        patched_targets.append(
            ("dashscope.common.utils._handle_stream", compat_stream_sync),
        )
    except Exception as error:
        logger.warning(
            "[DashScopeCompat] Failed to patch dashscope.common.utils stream handlers: %s",
            error,
        )

    try:
        import dashscope.api_entities.http_request as dashscope_http_request

        dashscope_http_request._handle_aio_stream = (
            compat_aio_stream or _handle_aio_stream_compat
        )
        patched_targets.append(
            (
                "dashscope.api_entities.http_request._handle_aio_stream",
                dashscope_http_request._handle_aio_stream,
            ),
        )
    except Exception as error:
        logger.warning(
            "[DashScopeCompat] Failed to patch dashscope.api_entities.http_request._handle_aio_stream: %s",
            error,
        )

    try:
        import dashscope.client.base_api as dashscope_base_api

        def _stream_event_mixin_handle_stream_compat(cls, response):
            is_error = False
            status_code = HTTPStatus.INTERNAL_SERVER_ERROR
            for raw_line in response.iter_lines():
                line = _normalize_stream_line_sync(raw_line)
                if not line:
                    continue

                line = line.rstrip("\n").rstrip("\r")
                if line.startswith("event:error"):
                    is_error = True
                elif line.startswith("status:"):
                    try:
                        status_code = int(line[len("status:") :].strip())
                    except (TypeError, ValueError):
                        continue
                elif line.startswith("data:"):
                    payload = line[len("data:") :]
                    yield (is_error, status_code, payload)
                    if is_error:
                        break

        dashscope_base_api.StreamEventMixin._handle_stream = classmethod(
            _stream_event_mixin_handle_stream_compat,
        )
        patched_targets.append(
            (
                "dashscope.client.base_api.StreamEventMixin._handle_stream",
                dashscope_base_api.StreamEventMixin._handle_stream,
            ),
        )
    except Exception as error:
        logger.warning(
            "[DashScopeCompat] Failed to patch dashscope.client.base_api.StreamEventMixin._handle_stream: %s",
            error,
        )

    try:
        from dashscope.api_entities.aiohttp_request import AioHttpRequest
    except Exception as error:
        AioHttpRequest = None
        logger.warning(
            "[DashScopeCompat] Failed to import AioHttpRequest for patching: %s",
            error,
        )

    async def _handle_stream_compat(self, response):
        is_error = False
        status_code = HTTPStatus.BAD_REQUEST

        async for raw_line in response.content:
            line = await _normalize_stream_line(raw_line)
            if not line:
                continue

            line = line.rstrip("\n").rstrip("\r")
            if line.startswith("event:error"):
                is_error = True
            elif line.startswith("status:"):
                status_code = int(line[len("status:") :].strip())
            elif line.startswith("data:"):
                payload = line[len("data:") :]
                yield (is_error, status_code, payload)
                if is_error:
                    break

    if AioHttpRequest is not None:
        AioHttpRequest._handle_stream = _handle_stream_compat
        patched_targets.append(
            (
                "dashscope.api_entities.aiohttp_request.AioHttpRequest._handle_stream",
                AioHttpRequest._handle_stream,
            ),
        )

    if not patched_targets:
        logger.warning("[DashScopeCompat] StreamReader compatibility patch had no targets")
        return False

    _PATCH_APPLIED = True
    logger.info(
        "[DashScopeCompat] Applied StreamReader compatibility patch to: %s",
        ", ".join(name for name, _ in patched_targets),
    )
    logger.info(
        "[DashScopeCompat] Patch diagnostics: %s",
        "; ".join(
            f"{name}@id={id(func)} decode_literal={_source_has_decode_literal(func)}"
            for name, func in patched_targets
        ),
    )
    return True
