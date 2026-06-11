"""Dedicated reranker op for ReMe task retrieval.

This op calls DashScope TextReRank (`qwen3-vl-rerank`) and does not use
chat-LLM reranking.
"""

from __future__ import annotations

import asyncio
import os
from typing import List

from flowllm.core.context import C
from flowllm.core.op import BaseAsyncOp
from loguru import logger

from reme_ai.schema.memory import BaseMemory


def _attr_or_key(value, name: str, default=None):
    if value is None:
        return default
    if isinstance(value, dict):
        return value.get(name, default)
    return getattr(value, name, default)

def _memory_to_document(memory: BaseMemory) -> str:
    return f"{memory.when_to_use or ''}\n{memory.content or ''}".strip()


@C.register_op()
class DashScopeTextRerankOp(BaseAsyncOp):
    """Rerank recalled memories with DashScope TextReRank API.

    Supported op params:
    - model_name: reranker model, default `qwen3-vl-rerank`
    - top_k: number of memories to keep (default 5)
    - timeout_seconds: API timeout; on timeout can fail-open (default 2.5)
    - fail_open: keep vector-order fallback on errors (default True)
    """

    file_path: str = __file__

    async def async_execute(self):
        memory_list: List[BaseMemory] = self.context.response.metadata["memory_list"]
        if not memory_list:
            logger.info("No recalled memory_list to rerank (dashscope rerank)")
            return

        query = str(getattr(self.context, "query", "") or "").strip()
        top_k = max(1, int(self.op_params.get("top_k", 5)))
        if not query:
            self.context.response.metadata["memory_list"] = memory_list[:top_k]
            return

        model_name = str(
            self.op_params.get("model_name")
            or os.environ.get("AGENTSCOPE_LTM_RERANK_MODEL")
            or "qwen3-vl-rerank",
        ).strip() or "qwen3-vl-rerank"
        timeout_seconds = float(self.op_params.get("timeout_seconds", 2.5))
        fail_open = bool(self.op_params.get("fail_open", True))

        documents = [_memory_to_document(memory) for memory in memory_list]
        if not any(documents):
            self.context.response.metadata["memory_list"] = memory_list[:top_k]
            return

        try:
            from dashscope import TextReRank

            def _invoke_rerank():
                return TextReRank.call(
                    model=model_name,
                    query=query,
                    documents=documents,
                    top_n=min(top_k, len(documents)),
                    api_key=os.environ.get("DASHSCOPE_API_KEY"),
                )

            if timeout_seconds > 0:
                response = await asyncio.wait_for(
                    asyncio.to_thread(_invoke_rerank),
                    timeout=timeout_seconds,
                )
            else:
                response = await asyncio.to_thread(_invoke_rerank)

            status_code = _attr_or_key(response, "status_code")
            if int(status_code or 0) != 200:
                raise RuntimeError(
                    f"DashScope rerank failed status_code={status_code} "
                    f"code={_attr_or_key(response, 'code')} "
                    f"message={_attr_or_key(response, 'message')}",
                )

            output = _attr_or_key(response, "output", {})
            results = _attr_or_key(output, "results", []) or []

            ranked_indices = []
            seen = set()
            for item in results:
                raw_index = _attr_or_key(item, "index")
                if raw_index is None:
                    continue
                try:
                    idx = int(raw_index)
                except (TypeError, ValueError):
                    continue
                if 0 <= idx < len(memory_list) and idx not in seen:
                    seen.add(idx)
                    ranked_indices.append(idx)

            reranked = [memory_list[i] for i in ranked_indices]
            for i, memory in enumerate(memory_list):
                if i not in seen:
                    reranked.append(memory)

            selected = reranked[:top_k]
            self.context.response.metadata["memory_list"] = selected
            logger.info(
                "DashScopeTextRerankOp selected %d/%d memories model=%s top_k=%d",
                len(selected),
                len(memory_list),
                model_name,
                top_k,
            )
        except Exception as exc:
            if fail_open:
                self.context.response.metadata["memory_list"] = memory_list[:top_k]
                logger.warning(
                    "DashScopeTextRerankOp failed; fallback to vector order "
                    "(model=%s, top_k=%d): %s",
                    model_name,
                    top_k,
                    exc,
                )
                return
            raise
