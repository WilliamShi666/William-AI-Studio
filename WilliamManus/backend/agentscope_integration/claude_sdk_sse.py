"""
Claude SDK SSE Adapter — converts Claude Agent SDK messages to WilliamManus SSE format.

Converts the dict messages streamed from the sandbox-resident Claude Agent Service
into the SSE envelope expected by the WilliamManus frontend.

Reference: agentscope_integration/streaming/sse_adapter.py for the target format.
"""

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from .streaming.sse_adapter import safe_json_dumps


class ClaudeSDKSSEAdapter:
    """Converts Claude SDK message dicts to frontend-compatible SSE dicts."""

    def __init__(
        self,
        thread_id: str,
        thread_run_id: str,
    ) -> None:
        self.thread_id = thread_id
        self.thread_run_id = thread_run_id
        self.sequence = 0
        # Track tool_use IDs so we can name tool results (Claude SDK result
        # messages only carry tool_use_id, not name).
        self._tool_names: Dict[str, str] = {}
        self._tool_use_blocks_by_index: Dict[int, Dict[str, Any]] = {}
        self._tool_argument_buffers: Dict[int, str] = {}
        self._subagent_tool_use_ids: set[str] = set()

    def convert(self, msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Convert a single Claude SDK message dict to SSE format, or None to skip."""
        msg_type = msg.get("type")

        if msg_type == "stream_event":
            return self._convert_stream_event(msg)
        if msg_type == "assistant":
            return self._convert_assistant(msg)
        if msg_type == "user":
            return self._convert_user(msg)
        if msg_type == "result":
            return self._convert_result(msg)
        # system.init, auth_status, task_notification etc. are internal
        return None

    # ── internal converters ──

    def _convert_stream_event(self, msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        event = msg.get("event", {})
        event_type = event.get("type")
        parent_tool_use_id = msg.get("parent_tool_use_id")
        subagent_meta = self._subagent_meta(parent_tool_use_id)

        if event_type == "content_block_delta":
            delta = event.get("delta", {})
            if delta.get("type") == "text_delta":
                return self._make_sse(
                    msg_type="assistant",
                    is_llm=True,
                    stream_status="chunk",
                    content={"role": "assistant", "content": delta.get("text", "")},
                    extra_meta=subagent_meta,
                )
            if delta.get("type") == "thinking_delta":
                return self._make_sse(
                    msg_type="assistant",
                    is_llm=True,
                    stream_status="reasoning_chunk",
                    content={"role": "assistant", "reasoning_content": delta.get("thinking", "")},
                    extra_meta={**subagent_meta, "subagent_activity_kind": "reasoning"} if subagent_meta else None,
                )
            if delta.get("type") == "input_json_delta":
                return self._convert_tool_input_delta(event, delta)
        elif event_type == "content_block_start":
            block = event.get("content_block", {})
            if block.get("type") == "tool_use":
                self._tool_names[block.get("id", "")] = block.get("name", "")
                block_index = event.get("index")
                if isinstance(block_index, int):
                    self._tool_use_blocks_by_index[block_index] = dict(block)
                    self._tool_argument_buffers[block_index] = safe_json_dumps(
                        block.get("input", {})
                    )
                tool_call = self._format_tool_call(block)
                is_agent_tool = block.get("name") in {"Agent", "Task"}
                extra_meta = {"tool_calls": [tool_call]}
                if is_agent_tool:
                    tool_use_id = str(block.get("id", ""))
                    if tool_use_id:
                        self._subagent_tool_use_ids.add(tool_use_id)
                    extra_meta.update({
                        "activity_owner": "claude_sdk_subagent",
                        "subagent_tool_call_id": block.get("id", ""),
                    })
                elif subagent_meta:
                    extra_meta.update(subagent_meta)
                return self._make_sse(
                    msg_type="assistant",
                    is_llm=True,
                    stream_status="tool_call_chunk",
                    content={"role": "assistant", "content": "", "tool_calls": [tool_call]},
                    extra_meta=extra_meta,
                )
        return None

    def _convert_tool_input_delta(
        self,
        event: Dict[str, Any],
        delta: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """Convert tool argument deltas into updated tool_call_chunk events.

        Claude Code often starts an Agent/Task tool_use with an empty input and
        then streams the real JSON arguments through input_json_delta chunks.
        Emitting the updated tool_call_chunk lets the frontend refine the
        teammate name and task description in the Shadow Clone panel.
        """
        block_index = event.get("index")
        if not isinstance(block_index, int):
            return None

        block = self._tool_use_blocks_by_index.get(block_index)
        if not block:
            return None

        partial_json = delta.get("partial_json")
        if not isinstance(partial_json, str) or not partial_json:
            return None

        existing = self._tool_argument_buffers.get(block_index, "")
        if existing == "{}":
            stripped_partial = partial_json.strip()
            updated_arguments = (
                stripped_partial
                if stripped_partial.startswith(("{", "["))
                else f"{{{stripped_partial}}}"
            )
        else:
            updated_arguments = f"{existing}{partial_json}"
        self._tool_argument_buffers[block_index] = updated_arguments

        tool_call = self._format_tool_call_with_arguments(block, updated_arguments)
        is_agent_tool = block.get("name") in {"Agent", "Task"}
        extra_meta = {"tool_calls": [tool_call]}
        if is_agent_tool:
            tool_use_id = str(block.get("id", ""))
            if tool_use_id:
                self._subagent_tool_use_ids.add(tool_use_id)
            extra_meta.update({
                "activity_owner": "claude_sdk_subagent",
                "subagent_tool_call_id": block.get("id", ""),
            })

        return self._make_sse(
            msg_type="assistant",
            is_llm=True,
            stream_status="tool_call_chunk",
            content={"role": "assistant", "content": "", "tool_calls": [tool_call]},
            extra_meta=extra_meta,
        )


    @staticmethod
    def _subagent_meta(parent_tool_use_id: Any) -> Dict[str, Any]:
        if not parent_tool_use_id:
            return {}
        return {
            "activity_owner": "claude_sdk_subagent",
            "parent_tool_use_id": str(parent_tool_use_id),
        }

    def _convert_assistant(self, msg: Dict[str, Any]) -> Dict[str, Any]:
        message = msg.get("message", {})
        content_blocks = message.get("content", [])
        parent_tool_use_id = msg.get("parent_tool_use_id")
        text = ""
        reasoning = ""
        tool_calls = []

        has_agent_tool = False
        for block in content_blocks:
            block_type = block.get("type")
            if block_type == "text":
                text += block.get("text", "")
            elif block_type == "thinking":
                reasoning += block.get("thinking", "")
            elif block_type == "tool_use":
                tool_use_id = str(block.get("id", ""))
                tool_name = str(block.get("name", ""))
                self._tool_names[tool_use_id] = tool_name
                if tool_name in {"Agent", "Task"} and tool_use_id:
                    self._subagent_tool_use_ids.add(tool_use_id)
                    has_agent_tool = True
                tool_calls.append(self._format_tool_call(block))

        content: Dict[str, Any] = {"role": "assistant", "content": text}
        if reasoning:
            content["reasoning_content"] = reasoning
        if tool_calls:
            content["tool_calls"] = tool_calls

        extra_meta = self._subagent_meta(parent_tool_use_id)
        if tool_calls:
            extra_meta["tool_calls"] = tool_calls
        if has_agent_tool:
            first_agent_tool_call = next(
                (
                    tool_call
                    for tool_call in tool_calls
                    if tool_call.get("function", {}).get("name") in {"Agent", "Task"}
                ),
                None,
            )
            if first_agent_tool_call:
                extra_meta.update({
                    "activity_owner": "claude_sdk_subagent",
                    "subagent_tool_call_id": first_agent_tool_call.get("id", ""),
                })

        return self._make_sse(
            msg_type="assistant",
            is_llm=True,
            stream_status="tool_call_chunk" if has_agent_tool and not text else "complete",
            content=content,
            is_last=True,
            extra_meta=extra_meta,
        )

    def _convert_user(self, msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        message = msg.get("message", {})
        content_blocks = message.get("content", [])
        parent_tool_use_id = msg.get("parent_tool_use_id")
        top_level_tool_use_result = msg.get("tool_use_result")

        if top_level_tool_use_result is not None and parent_tool_use_id:
            tool_name = self._tool_names.get(parent_tool_use_id, "")
            result_text = (
                top_level_tool_use_result
                if isinstance(top_level_tool_use_result, str)
                else safe_json_dumps(top_level_tool_use_result)
            )
            extra_meta = (
                self._subagent_meta(parent_tool_use_id)
                if parent_tool_use_id in self._subagent_tool_use_ids
                else None
            )
            return self._make_sse(
                msg_type="tool",
                is_llm=False,
                stream_status="complete",
                content={
                    "tool_name": tool_name,
                    "tool_call_id": parent_tool_use_id,
                    "result": result_text,
                },
                is_last=True,
                extra_meta=extra_meta,
            )

        for block in content_blocks:
            if block.get("type") == "tool_result":
                tool_use_id = block.get("tool_use_id", "")
                tool_name = self._tool_names.get(tool_use_id, "")
                raw_content = block.get("content", "")
                result_text = raw_content if isinstance(raw_content, str) else safe_json_dumps(raw_content)

                extra_meta = (
                    self._subagent_meta(tool_use_id)
                    if tool_use_id in self._subagent_tool_use_ids
                    else None
                )

                return self._make_sse(
                    msg_type="tool",
                    is_llm=False,
                    stream_status="complete",
                    content={
                        "tool_name": tool_name,
                        "tool_call_id": tool_use_id,
                        "result": result_text,
                    },
                    is_last=True,
                    extra_meta=extra_meta,
                )
        return None

    def _convert_result(self, msg: Dict[str, Any]) -> Dict[str, Any]:
        is_error = msg.get("is_error", False)
        return {
            "type": "status",
            "status": "failed" if is_error else "completed",
            "message": msg.get("result", ""),
        }

    # ── helpers ──

    def _make_sse(
        self,
        *,
        msg_type: str,
        is_llm: bool,
        stream_status: str,
        content: Dict[str, Any],
        is_last: bool = False,
        extra_meta: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        now = datetime.now(timezone.utc).isoformat()
        metadata: Dict[str, Any] = {
            "stream_status": stream_status,
            "thread_run_id": self.thread_run_id,
        }
        if extra_meta:
            metadata.update(extra_meta)

        sse_msg: Dict[str, Any] = {
            "sequence": self.sequence,
            "message_id": str(uuid.uuid4()) if is_last else None,
            "thread_id": self.thread_id,
            "type": msg_type,
            "is_llm_message": is_llm,
            "content": safe_json_dumps(content),
            "metadata": safe_json_dumps(metadata),
            "created_at": now,
            "updated_at": now,
        }

        self.sequence += 1
        return sse_msg

    @staticmethod
    def _format_tool_call(block: Dict[str, Any]) -> Dict[str, Any]:
        return ClaudeSDKSSEAdapter._format_tool_call_with_arguments(
            block,
            safe_json_dumps(block.get("input", {})),
        )

    @staticmethod
    def _format_tool_call_with_arguments(
        block: Dict[str, Any],
        arguments: str,
    ) -> Dict[str, Any]:
        return {
            "id": block.get("id", ""),
            "type": "function",
            "function": {
                "name": block.get("name", ""),
                "arguments": arguments,
            },
        }
