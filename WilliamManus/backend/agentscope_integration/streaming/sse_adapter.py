"""
SSE Adapter for AgentScope Integration

Converts AgentScope Msg objects to the SSE format expected by the frontend.

Frontend expected SSE format:
{
    "sequence": int,
    "message_id": str | null,
    "thread_id": str,
    "type": "assistant" | "tool" | "user",
    "is_llm_message": bool,
    "content": JSON string,
    "metadata": JSON string with stream_status,
    "created_at": ISO timestamp,
    "updated_at": ISO timestamp
}

stream_status values:
- "chunk": Text streaming in progress
- "reasoning_chunk": Thinking/reasoning streaming
- "tool_call_chunk": Tool call being generated
- "tool_result_chunk": Tool result streaming
- "complete": Message complete
"""

import hashlib
import json
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, Optional, List

from agentscope.message import Msg


class SafeJSONEncoder(json.JSONEncoder):
    """JSON encoder that handles non-serializable objects like Enums."""
    def default(self, obj):
        if isinstance(obj, Enum):
            return obj.value
        if isinstance(obj, bytes):
            return obj.decode('utf-8', errors='replace')
        try:
            return super().default(obj)
        except TypeError:
            return str(obj)


def safe_json_dumps(obj, **kwargs) -> str:
    """Safely serialize object to JSON string, handling non-serializable types."""
    kwargs.setdefault('ensure_ascii', False)
    kwargs.setdefault('cls', SafeJSONEncoder)
    return json.dumps(obj, **kwargs)


class SSEAdapter:
    """
    Converts AgentScope messages to frontend-compatible SSE format.
    
    This adapter ensures that AgentScope's streaming output is compatible
    with the existing frontend without any frontend modifications.
    """
    
    def __init__(
        self,
        thread_id: str,
        thread_run_id: str,
        route_metadata: Optional[Dict[str, str]] = None,
    ):
        """
        Initialize the SSE adapter.
        
        Args:
            thread_id: Thread ID for this conversation
            thread_run_id: Run ID for this agent execution
        """
        self.thread_id = thread_id
        self.thread_run_id = thread_run_id
        self.sequence = 0
        self.route_metadata = dict(route_metadata or {})
    
    def convert(self, msg: Msg, is_last: bool) -> Dict:
        """
        Convert an AgentScope Msg to SSE format.
        
        Args:
            msg: AgentScope Msg object
            is_last: Whether this is the final chunk of the message
            
        Returns:
            Dict in SSE format
        """
        now = datetime.now(timezone.utc).isoformat()
        
        # Determine message type and stream status
        msg_type, stream_status = self._determine_type_and_status(msg, is_last)
        
        # Build content
        content = self._build_content(msg)
        
        # Build metadata
        metadata = self._build_metadata(msg, stream_status)
        
        sse_msg = {
            "sequence": self.sequence,
            "message_id": str(uuid.uuid4()) if is_last else None,
            "thread_id": self.thread_id,
            "type": msg_type,
            "is_llm_message": msg.role == "assistant",
            "content": safe_json_dumps(content),
            "metadata": safe_json_dumps(metadata),
            "created_at": now,
            "updated_at": now,
        }
        
        self.sequence += 1
        return sse_msg
    
    def _determine_type_and_status(self, msg: Msg, is_last: bool) -> tuple:
        """
        Determine message type and stream status.
        
        Args:
            msg: AgentScope Msg object
            is_last: Whether this is the final chunk
            
        Returns:
            Tuple of (msg_type, stream_status)
        """
        blocks = self._get_content_blocks(msg)
        
        # Check for different block types
        tool_use_blocks = [b for b in blocks if b.get("type") == "tool_use"]
        tool_result_blocks = [b for b in blocks if b.get("type") == "tool_result"]
        thinking_blocks = [b for b in blocks if b.get("type") == "thinking"]
        text_content = msg.get_text_content() if hasattr(msg, "get_text_content") else ""
        has_text_content = bool(str(text_content or "").strip())

        if tool_result_blocks:
            msg_type = "tool"
            stream_status = "complete" if is_last else "tool_result_chunk"
        elif tool_use_blocks:
            msg_type = "assistant"
            stream_status = "complete" if is_last else "tool_call_chunk"
        elif has_text_content:
            msg_type = "assistant" if msg.role == "assistant" else msg.role
            stream_status = "complete" if is_last else "chunk"
        elif thinking_blocks:
            msg_type = "assistant"
            stream_status = "complete" if is_last else "reasoning_chunk"
        else:
            msg_type = "assistant" if msg.role == "assistant" else msg.role
            stream_status = "complete" if is_last else "chunk"
        
        return msg_type, stream_status
    
    def _build_content(self, msg: Msg) -> Dict:
        """
        Build content dict from AgentScope Msg.
        
        Args:
            msg: AgentScope Msg object
            
        Returns:
            Content dict
        """
        blocks = self._get_content_blocks(msg)
        
        # Extract text content
        text_content = msg.get_text_content() if hasattr(msg, 'get_text_content') else ""
        text_content = text_content or ""
        
        # Extract reasoning content
        thinking_blocks = [b for b in blocks if b.get("type") == "thinking"]
        reasoning_content = thinking_blocks[0].get("thinking", "") if thinking_blocks else None
        
        # Extract tool calls
        tool_use_blocks = [b for b in blocks if b.get("type") == "tool_use"]
        tool_calls = None
        if tool_use_blocks:
            tool_calls = [
                {
                    "id": tb.get("id"),
                    "type": "function",
                    "function": {
                        "name": tb.get("name"),
                        "arguments": json.dumps(tb.get("input", {}), ensure_ascii=False),
                    }
                }
                for tb in tool_use_blocks
            ]
        
        # Extract tool results
        tool_result_blocks = [b for b in blocks if b.get("type") == "tool_result"]
        if tool_result_blocks:
            tr = tool_result_blocks[0]
            output = tr.get("output", [])
            if isinstance(output, list) and output:
                result_text = output[0].get("text", "") if isinstance(output[0], dict) else str(output[0])
            else:
                result_text = str(output)
            
            return {
                "tool_name": tr.get("name"),
                "tool_call_id": tr.get("id"),
                "result": result_text,
            }
        
        # Build standard content
        content = {
            "role": msg.role,
            "content": text_content,
        }
        
        if reasoning_content:
            content["reasoning_content"] = reasoning_content
        
        if tool_calls:
            content["tool_calls"] = tool_calls
        
        return content
    
    def _build_metadata(self, msg: Msg, stream_status: str) -> Dict:
        """
        Build metadata dict.
        
        Args:
            msg: AgentScope Msg object
            stream_status: Current stream status
            
        Returns:
            Metadata dict
        """
        metadata = {
            "stream_status": stream_status,
            "thread_run_id": self.thread_run_id,
        }
        if self.route_metadata:
            metadata.update(self.route_metadata)

        msg_metadata = getattr(msg, "metadata", None)
        if isinstance(msg_metadata, dict):
            # Keep a small, stable relay contract for cache/context reuse in
            # post-run review without exposing arbitrary model payload.
            relay_keys = (
                "model_key",
                "resolved_model_key",
                "cache_contract_version",
                "kv_cache_session_id",
                "kv_cache_enabled",
                "kv_cache_breakpoint_mode",
                "kv_cache_contract_version",
                "kv_cache_prompt_tokens",
                "kv_cache_completion_tokens",
                "kv_cache_cached_input_tokens",
                "kv_cache_hit_rate",
            )
            for key in relay_keys:
                value = msg_metadata.get(key)
                if value is None:
                    continue
                if isinstance(value, (str, int, float, bool)):
                    metadata[key] = value
        
        # Add tool calls to metadata for tool_call_chunk status
        if stream_status == "tool_call_chunk":
            blocks = self._get_content_blocks(msg)
            tool_use_blocks = [b for b in blocks if b.get("type") == "tool_use"]
            
            if tool_use_blocks:
                metadata["tool_calls"] = [
                    {
                        "id": tb.get("id"),
                        "function": {
                            "name": tb.get("name"),
                            "arguments": self._serialize_tool_call_arguments_for_metadata(tb),
                        },
                        "trace": self._build_tool_trace(tb),
                    }
                    for tb in tool_use_blocks
                ]

        return metadata

    def _build_tool_trace(self, tool_block: Dict) -> Dict[str, Optional[str] | int]:
        function_arguments = json.dumps(
            tool_block.get("input", {}),
            ensure_ascii=False,
        )
        raw_arguments = tool_block.get("raw_arguments")
        raw_arguments_text = raw_arguments if isinstance(raw_arguments, str) else ""
        args_source = str(tool_block.get("arguments_source") or "parsed_input")
        return {
            "trace_source_stage": "sse_adapter",
            "trace_tool_call_id": str(tool_block.get("id", "") or ""),
            "trace_args_len": len(function_arguments),
            "trace_args_hash": hashlib.sha1(
                function_arguments.encode("utf-8"),
            ).hexdigest()[:12],
            "trace_args_source": args_source,
            "trace_raw_args_len": len(raw_arguments_text),
            "trace_raw_args_hash": hashlib.sha1(
                raw_arguments_text.encode("utf-8"),
            ).hexdigest()[:12] if raw_arguments_text else "",
        }

    def _serialize_tool_call_arguments_for_metadata(self, tool_block: Dict) -> str:
        """Prefer raw accumulated arguments for metadata when available."""
        raw_arguments = tool_block.get("raw_arguments")
        if isinstance(raw_arguments, str) and raw_arguments:
            return raw_arguments
        return json.dumps(tool_block.get("input", {}), ensure_ascii=False)
    
    def _get_content_blocks(self, msg: Msg) -> List[Dict]:
        """
        Get content blocks from a Msg.
        
        Args:
            msg: AgentScope Msg object
            
        Returns:
            List of content block dicts
        """
        if hasattr(msg, 'get_content_blocks'):
            blocks = msg.get_content_blocks()
            # Convert to dicts if needed
            return [b if isinstance(b, dict) else dict(b) for b in blocks]
        return []
    
    def reset_sequence(self):
        """Reset the sequence counter."""
        self.sequence = 0
