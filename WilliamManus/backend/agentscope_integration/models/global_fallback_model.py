"""Global fallback wrapper for chat models."""

from __future__ import annotations

from typing import Any, AsyncGenerator, Callable

from agentscope.model import ChatModelBase
from agentscope.model._model_response import ChatResponse

from utils.logger import logger


class GlobalFallbackChatModel(ChatModelBase):
    """Wrap a primary model and fallback only after primary retries are exhausted."""

    def __init__(
        self,
        primary_model: ChatModelBase,
        primary_model_key: str,
        fallback_model_key: str,
        fallback_factory: Callable[[bool], ChatModelBase],
        primary_max_retries: int = 0,
        fallback_on_stream_error_after_output: bool = True,
    ) -> None:
        super().__init__(
            model_name=getattr(primary_model, "model_name", primary_model_key),
            stream=getattr(primary_model, "stream", True),
        )
        self._primary_model = primary_model
        self._primary_model_key = str(primary_model_key or "").lower()
        self._fallback_model_key = str(fallback_model_key or "").strip()
        self._fallback_factory = fallback_factory
        self._fallback_model: ChatModelBase | None = None
        self._primary_max_retries = max(0, int(primary_max_retries or 0))
        self._fallback_on_stream_error_after_output = bool(
            fallback_on_stream_error_after_output,
        )

    def __getattr__(self, item: str) -> Any:
        return getattr(self._primary_model, item)

    def _can_fallback(self) -> bool:
        if not self._fallback_model_key:
            return False
        return self._primary_model_key != self._fallback_model_key.lower()

    def _fallback_model_instance(self) -> ChatModelBase:
        if self._fallback_model is None:
            self._fallback_model = self._fallback_factory(self.stream)
        return self._fallback_model

    @staticmethod
    def _sanitize_messages_for_fallback(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        sanitized: list[dict[str, Any]] = []
        for msg in messages:
            if not isinstance(msg, dict):
                sanitized.append(msg)
                continue
            payload = dict(msg)
            payload.pop("reasoning_content", None)
            sanitized.append(payload)
        return sanitized

    async def _call_fallback(
        self,
        error: Exception,
        messages: list[dict[str, Any]],
        tools: list[dict] | None,
        tool_choice: str | None,
        structured_model: Any,
        kwargs: dict[str, Any],
        failed_attempts: int,
    ) -> ChatResponse | AsyncGenerator[ChatResponse, None]:
        if not self._can_fallback():
            raise error

        logger.warning(
            "[GlobalFallbackChatModel] Primary model failed after %s attempt(s) (%s). Falling back to %s",
            failed_attempts,
            error,
            self._fallback_model_key,
        )
        fallback_model = self._fallback_model_instance()
        fallback_messages = self._sanitize_messages_for_fallback(messages)
        return await fallback_model(
            fallback_messages,
            tools=tools,
            tool_choice=tool_choice,
            structured_model=structured_model,
            **kwargs,
        )

    async def _yield_response(
        self,
        response: ChatResponse | AsyncGenerator[ChatResponse, None],
    ) -> AsyncGenerator[ChatResponse, None]:
        if hasattr(response, "__aiter__"):
            async for chunk in response:
                yield chunk
            return
        yield response

    def _log_primary_retry(self, error: Exception, failed_attempts: int, reason: str) -> None:
        logger.warning(
            "[GlobalFallbackChatModel] Primary model failed attempt %s/%s (%s). Retrying primary model (%s)",
            failed_attempts,
            self._primary_max_retries + 1,
            error,
            reason,
        )

    async def __call__(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict] | None = None,
        tool_choice: str | None = None,
        structured_model: Any = None,
        **kwargs: Any,
    ) -> ChatResponse | AsyncGenerator[ChatResponse, None]:
        if not self.stream:
            failed_attempts = 0
            while True:
                try:
                    return await self._primary_model(
                        messages,
                        tools=tools,
                        tool_choice=tool_choice,
                        structured_model=structured_model,
                        **kwargs,
                    )
                except Exception as error:
                    failed_attempts += 1
                    if failed_attempts <= self._primary_max_retries:
                        self._log_primary_retry(
                            error,
                            failed_attempts,
                            reason="call",
                        )
                        continue
                    return await self._call_fallback(
                        error=error,
                        messages=messages,
                        tools=tools,
                        tool_choice=tool_choice,
                        structured_model=structured_model,
                        kwargs=kwargs,
                        failed_attempts=failed_attempts,
                    )

        async def _stream_with_fallback() -> AsyncGenerator[ChatResponse, None]:
            failed_attempts = 0
            while True:
                try:
                    primary_response = await self._primary_model(
                        messages,
                        tools=tools,
                        tool_choice=tool_choice,
                        structured_model=structured_model,
                        **kwargs,
                    )
                except Exception as error:
                    failed_attempts += 1
                    if failed_attempts <= self._primary_max_retries:
                        self._log_primary_retry(
                            error,
                            failed_attempts,
                            reason="call",
                        )
                        continue

                    fallback_response = await self._call_fallback(
                        error=error,
                        messages=messages,
                        tools=tools,
                        tool_choice=tool_choice,
                        structured_model=structured_model,
                        kwargs=kwargs,
                        failed_attempts=failed_attempts,
                    )
                    async for chunk in self._yield_response(fallback_response):
                        yield chunk
                    return

                if not hasattr(primary_response, "__aiter__"):
                    yield primary_response
                    return

                yielded_primary_output = False
                try:
                    async for chunk in primary_response:
                        yielded_primary_output = True
                        yield chunk
                    return
                except Exception as error:
                    if yielded_primary_output:
                        if not self._fallback_on_stream_error_after_output:
                            raise
                        fallback_response = await self._call_fallback(
                            error=error,
                            messages=messages,
                            tools=tools,
                            tool_choice=tool_choice,
                            structured_model=structured_model,
                            kwargs=kwargs,
                            failed_attempts=failed_attempts + 1,
                        )
                        async for chunk in self._yield_response(fallback_response):
                            yield chunk
                        return

                    failed_attempts += 1
                    if failed_attempts <= self._primary_max_retries:
                        self._log_primary_retry(
                            error,
                            failed_attempts,
                            reason="stream_start",
                        )
                        continue

                    fallback_response = await self._call_fallback(
                        error=error,
                        messages=messages,
                        tools=tools,
                        tool_choice=tool_choice,
                        structured_model=structured_model,
                        kwargs=kwargs,
                        failed_attempts=failed_attempts,
                    )
                    async for chunk in self._yield_response(fallback_response):
                        yield chunk
                    return

        return _stream_with_fallback()
