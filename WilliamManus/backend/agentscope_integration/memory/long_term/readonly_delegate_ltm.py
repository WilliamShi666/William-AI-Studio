"""Read-only long-term memory adapter for delegated agents."""

from __future__ import annotations

from typing import Any

from agentscope.memory import LongTermMemoryBase
from agentscope.message import Msg, TextBlock
from agentscope.tool import ToolResponse


class ReadOnlyDelegateLongTermMemory(LongTermMemoryBase):
    """Expose retrieval to delegated agents while blocking all writes."""

    def __init__(
        self,
        delegate: LongTermMemoryBase,
        *,
        manage_lifecycle: bool = False,
    ) -> None:
        super().__init__()
        if delegate is None:
            raise ValueError("delegate long-term memory must be provided")
        self._delegate = delegate
        self._manage_lifecycle = bool(manage_lifecycle)
        self._entered = False

    async def __aenter__(self) -> "ReadOnlyDelegateLongTermMemory":
        if self._manage_lifecycle and hasattr(self._delegate, "__aenter__"):
            await self._delegate.__aenter__()
            self._entered = True
        return self

    async def __aexit__(
        self,
        exc_type,
        exc_val,
        exc_tb,
    ) -> None:
        if (
            self._manage_lifecycle
            and self._entered
            and hasattr(self._delegate, "__aexit__")
        ):
            await self._delegate.__aexit__(exc_type, exc_val, exc_tb)
        self._entered = False

    async def record(
        self,
        msgs: list[Msg | None],
        **kwargs: Any,
    ) -> None:
        # Delegated agents only auto-retrieve; they never auto-record.
        return None

    async def retrieve(
        self,
        msg: Msg | list[Msg] | None,
        limit: int = 5,
        **kwargs: Any,
    ) -> str:
        return await self._delegate.retrieve(msg, limit=limit, **kwargs)

    async def record_to_memory(
        self,
        thinking: str,
        content: list[str],
        **kwargs: Any,
    ) -> ToolResponse:
        return ToolResponse(
            content=[
                TextBlock(
                    type="text",
                    text=(
                        "Long-term memory recording is disabled for delegated agents. "
                        "Report reusable lessons upstream instead."
                    ),
                ),
            ],
            metadata={
                "success": False,
                "write_disabled": True,
            },
        )

    async def retrieve_from_memory(
        self,
        keywords: list[str],
        limit: int = 5,
        **kwargs: Any,
    ) -> ToolResponse:
        return await self._delegate.retrieve_from_memory(
            keywords,
            limit=limit,
            **kwargs,
        )

    async def delete_from_memory(
        self,
        thinking: str,
        content_to_delete: list[str],
        **kwargs: Any,
    ) -> ToolResponse:
        return ToolResponse(
            content=[
                TextBlock(
                    type="text",
                    text=(
                        "Long-term memory deletion is disabled for delegated agents. "
                        "Report incorrect memories upstream instead."
                    ),
                ),
            ],
            metadata={
                "success": False,
                "delete_disabled": True,
            },
        )
