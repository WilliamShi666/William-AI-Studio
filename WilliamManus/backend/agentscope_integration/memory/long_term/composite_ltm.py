"""Composite long-term memory for task + tool ReMe backends."""

from __future__ import annotations

import asyncio
from typing import Any, Optional

from agentscope.memory import LongTermMemoryBase
from agentscope.message import Msg
from agentscope.tool import ToolResponse

from utils.logger import logger

from .ltm_policy import bucketize_memory_content, sanitize_keywords

_SCROLL_BATCH_SIZE = 100


def _text_tool_response(text: str, metadata: Optional[dict] = None) -> ToolResponse:
    return ToolResponse(
        content=[{"type": "text", "text": str(text or "")}],
        metadata=metadata or {},
    )


def _tool_response_text(response: Any) -> str:
    if response is None:
        return ""

    content = getattr(response, "content", None)
    if isinstance(content, list):
        lines = []
        for block in content:
            if isinstance(block, dict):
                text = str(block.get("text") or "").strip()
                if text:
                    lines.append(text)
        return "\n".join(lines).strip()

    return str(response).strip()


class CompositeLongTermMemory(LongTermMemoryBase):
    """Composite wrapper that fans out to task/tool memory modules."""

    def __init__(
        self,
        *,
        task_memory: Any = None,
        tool_memory: Any = None,
        retrieve_limit: int = 5,
        write_gate_enabled: bool = True,
        static_record_enabled: bool = False,
        fail_open: bool = True,
        task_query_mode: str = "merged",
        task_query_keyword_limit: int = 8,
        qdrant_collection: str = "",
        qdrant_url: Optional[str] = None,
    ) -> None:
        super().__init__()
        self.task_memory = task_memory
        self.tool_memory = tool_memory
        self.retrieve_limit = max(1, int(retrieve_limit or 5))
        self.write_gate_enabled = bool(write_gate_enabled)
        self.static_record_enabled = bool(static_record_enabled)
        self.fail_open = bool(fail_open)
        mode = str(task_query_mode or "merged").strip().lower()
        self.task_query_mode = mode if mode in {"merged", "per_keyword"} else "merged"
        self.task_query_keyword_limit = max(1, int(task_query_keyword_limit or 8))
        self._qdrant_collection = str(qdrant_collection or "").strip()
        self._qdrant_url = str(qdrant_url or "").strip() or None
        self._entered = False

    def _task_retrieval_keywords(self, keywords: list[str]) -> list[str]:
        deduped = list(dict.fromkeys(str(item or "").strip() for item in keywords if str(item or "").strip()))
        if not deduped:
            return []

        limited = deduped[: self.task_query_keyword_limit]
        if self.task_query_mode == "per_keyword":
            return limited

        return [", ".join(limited)]

    async def __aenter__(self) -> "CompositeLongTermMemory":
        enters = []
        for memory in (self.task_memory, self.tool_memory):
            if memory is None:
                continue
            if hasattr(memory, "__aenter__"):
                enters.append(memory.__aenter__())

        if enters:
            results = await asyncio.gather(*enters, return_exceptions=True)
            for result in results:
                if isinstance(result, Exception):
                    if not self.fail_open:
                        raise result
                    logger.warning("[CompositeLTM] __aenter__ sub-memory failed: %s", result)

        self._entered = True
        return self

    async def __aexit__(
        self,
        exc_type: Any = None,
        exc_val: Any = None,
        exc_tb: Any = None,
    ) -> None:
        exits = []
        for memory in (self.task_memory, self.tool_memory):
            if memory is None:
                continue
            if hasattr(memory, "__aexit__"):
                exits.append(memory.__aexit__(exc_type, exc_val, exc_tb))

        if exits:
            results = await asyncio.gather(*exits, return_exceptions=True)
            for result in results:
                if isinstance(result, Exception):
                    if not self.fail_open:
                        raise result
                    logger.warning("[CompositeLTM] __aexit__ sub-memory failed: %s", result)
        self._entered = False

    async def record(
        self,
        msgs: list[Msg | None],
        **kwargs: Any,
    ) -> None:
        """Static-control auto-record hook.

        v1 default disables static writes to avoid full-conversation pollution.
        """
        if not self.static_record_enabled:
            return

        calls = []
        if self.task_memory is not None:
            calls.append(self.task_memory.record(msgs, **kwargs))
        if self.tool_memory is not None:
            calls.append(self.tool_memory.record(msgs, **kwargs))
        if not calls:
            return

        results = await asyncio.gather(*calls, return_exceptions=True)
        for result in results:
            if isinstance(result, Exception):
                if not self.fail_open:
                    raise result
                logger.warning("[CompositeLTM] static record failed: %s", result)

    async def retrieve(
        self,
        msg: Msg | list[Msg] | None,
        limit: int = 5,
        **kwargs: Any,
    ) -> str:
        resolved_limit = max(1, int(limit or self.retrieve_limit))
        calls = []
        labels = []
        if self.task_memory is not None:
            labels.append("task_experience")
            calls.append(self.task_memory.retrieve(msg, limit=resolved_limit, **kwargs))
        if self.tool_memory is not None:
            labels.append("tool_experience")
            calls.append(self.tool_memory.retrieve(msg, limit=resolved_limit, **kwargs))

        if not calls:
            return ""

        results = await asyncio.gather(*calls, return_exceptions=True)
        sections = []
        for label, result in zip(labels, results):
            if isinstance(result, Exception):
                if not self.fail_open:
                    raise result
                logger.warning("[CompositeLTM] retrieve failed for %s: %s", label, result)
                continue
            text = str(result or "").strip()
            if not text:
                continue
            sections.append(f"<{label}>\n{text}\n</{label}>")

        return "\n\n".join(sections).strip()

    async def record_to_memory(
        self,
        thinking: str,
        content: list[str],
        **kwargs: Any,
    ) -> ToolResponse:
        buckets = bucketize_memory_content(
            content,
            write_gate_enabled=self.write_gate_enabled,
        )

        calls = []
        labels = []
        if self.task_memory is not None and buckets.task_items:
            labels.append("task")
            calls.append(
                self.task_memory.record_to_memory(
                    thinking=thinking,
                    content=buckets.task_items,
                    **kwargs,
                ),
            )
        if self.tool_memory is not None and buckets.tool_items:
            labels.append("tool")
            calls.append(
                self.tool_memory.record_to_memory(
                    thinking=thinking,
                    content=buckets.tool_items,
                    **kwargs,
                ),
            )

        if not calls:
            return _text_tool_response(
                "No eligible memories were recorded after write-gate filtering.",
                metadata={
                    "recorded_task": 0,
                    "recorded_tool": 0,
                    "dropped_count": buckets.dropped_count,
                    "drop_reasons": buckets.drop_reasons,
                },
            )

        results = await asyncio.gather(*calls, return_exceptions=True)
        lines = []
        failures = 0
        for label, result in zip(labels, results):
            if isinstance(result, Exception):
                failures += 1
                if not self.fail_open:
                    raise result
                logger.warning("[CompositeLTM] record_to_memory failed for %s: %s", label, result)
                lines.append(f"[{label}] record failed: {result}")
                continue
            text = _tool_response_text(result)
            if text:
                lines.append(f"[{label}] {text}")
            else:
                lines.append(f"[{label}] record succeeded")

        metadata = {
            "recorded_task": len(buckets.task_items),
            "recorded_tool": len(buckets.tool_items),
            "dropped_count": buckets.dropped_count,
            "drop_reasons": buckets.drop_reasons,
            "failures": failures,
        }
        return _text_tool_response("\n".join(lines), metadata=metadata)

    async def retrieve_from_memory(
        self,
        keywords: list[str],
        limit: int = 5,
        **kwargs: Any,
    ) -> ToolResponse:
        query_keywords = sanitize_keywords(
            keywords,
            write_gate_enabled=self.write_gate_enabled,
        )
        if not query_keywords:
            return _text_tool_response("No valid keywords to retrieve from memory.")

        resolved_limit = max(1, int(limit or self.retrieve_limit))
        task_keywords = self._task_retrieval_keywords(query_keywords)
        calls = []
        labels = []
        if self.task_memory is not None and task_keywords:
            labels.append("task_experience")
            calls.append(
                self.task_memory.retrieve_from_memory(
                    task_keywords,
                    limit=resolved_limit,
                    **kwargs,
                ),
            )
        if self.tool_memory is not None:
            labels.append("tool_experience")
            calls.append(
                self.tool_memory.retrieve_from_memory(
                    query_keywords,
                    limit=resolved_limit,
                    **kwargs,
                ),
            )

        if not calls:
            return _text_tool_response("No long-term memory backend configured.")

        results = await asyncio.gather(*calls, return_exceptions=True)
        sections = []
        for label, result in zip(labels, results):
            if isinstance(result, Exception):
                if not self.fail_open:
                    raise result
                logger.warning("[CompositeLTM] retrieve_from_memory failed for %s: %s", label, result)
                continue
            text = _tool_response_text(result)
            if not text:
                continue
            sections.append(f"<{label}>\n{text}\n</{label}>")

        if not sections:
            return _text_tool_response("No relevant long-term memory found.")
        return _text_tool_response("\n\n".join(sections))

    async def delete_from_memory(
        self,
        thinking: str,
        content_to_delete: list[str],
        **kwargs: Any,
    ) -> ToolResponse:
        """Delete specific memories that are incorrect, outdated, or harmful.

        Use this when you discover a memory that contradicts known facts,
        user preferences, or leads to repeated errors.

        Args:
            thinking (`str`):
                Your reasoning about why these memories should be deleted.
            content_to_delete (`list[str]`):
                List of memory text strings to match and remove. These should
                closely match the text of memories as seen in retrieval results.
        """
        if not content_to_delete:
            return _text_tool_response(
                "No content provided for deletion.",
                metadata={"deleted": 0},
            )

        if not self._qdrant_url or not self._qdrant_collection:
            msg = (
                "Memory deletion is not available: Qdrant backend is not configured. "
                "Set FLOW_QDRANT_HOST and FLOW_QDRANT_PORT environment variables."
            )
            if not self.fail_open:
                raise RuntimeError(msg)
            logger.warning("[CompositeLTM] %s", msg)
            return _text_tool_response(msg, metadata={"deleted": 0})

        try:
            from qdrant_client import AsyncQdrantClient
        except ImportError as exc:
            msg = "qdrant-client is not installed. Cannot delete memories."
            if not self.fail_open:
                raise RuntimeError(msg) from exc
            logger.warning("[CompositeLTM] %s", msg)
            return _text_tool_response(msg, metadata={"deleted": 0})

        try:
            deleted_ids = await self._find_and_delete_points(
                AsyncQdrantClient,
                content_to_delete,
            )
        except Exception as exc:
            msg = f"Memory deletion failed: {exc}"
            if not self.fail_open:
                raise
            logger.warning("[CompositeLTM] %s", msg)
            return _text_tool_response(msg, metadata={"deleted": 0})

        if not deleted_ids:
            return _text_tool_response(
                "No matching memories found for the provided content.",
                metadata={"deleted": 0},
            )

        return _text_tool_response(
            f"Successfully deleted {len(deleted_ids)} memory entries.",
            metadata={"deleted": len(deleted_ids), "deleted_ids": deleted_ids},
        )

    async def _find_and_delete_points(
        self,
        client_cls: type,
        content_to_delete: list[str],
    ) -> list[str]:
        """Scroll collection, match content text, and delete matching points."""
        client = client_cls(url=self._qdrant_url)
        try:
            return await self._scroll_match_delete(client, content_to_delete)
        finally:
            await client.close()

    async def _scroll_match_delete(
        self,
        client: Any,
        content_to_delete: list[str],
    ) -> list[str]:
        """Scroll all points and delete those matching any content string."""
        targets = {s.strip().lower() for s in content_to_delete if s and s.strip()}
        if not targets:
            return []

        matched_ids: list[str] = []
        offset = None

        while True:
            scroll_kwargs: dict[str, Any] = {
                "collection_name": self._qdrant_collection,
                "limit": _SCROLL_BATCH_SIZE,
                "with_payload": True,
                "with_vectors": False,
            }
            if offset is not None:
                scroll_kwargs["offset"] = offset

            points, next_offset = await client.scroll(**scroll_kwargs)

            for point in points:
                payload = point.payload or {}
                content_field = str(payload.get("content", "")).strip().lower()
                meta = payload.get("metadata", {})
                meta_content = str(meta.get("content", "") if isinstance(meta, dict) else "").strip().lower()

                if content_field in targets or meta_content in targets:
                    matched_ids.append(str(point.id))

            if next_offset is None or not points:
                break
            offset = next_offset

        if matched_ids:
            from qdrant_client.models import PointIdsList

            await client.delete(
                collection_name=self._qdrant_collection,
                points_selector=PointIdsList(points=matched_ids),
            )
            logger.info(
                "[CompositeLTM] Deleted %d memories from Qdrant collection=%s ids=%s",
                len(matched_ids),
                self._qdrant_collection,
                matched_ids,
            )

        return matched_ids
