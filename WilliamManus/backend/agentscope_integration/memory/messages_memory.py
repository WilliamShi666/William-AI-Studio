"""
Messages Table Memory Adapter for AgentScope

This module provides a custom Memory implementation that reuses the existing
PostgreSQL 'messages' table instead of creating AgentScope's default tables.

The messages table has the following structure:
- message_id: UUID (auto-generated)
- thread_id: UUID (foreign key to threads)
- type: str (e.g., 'user', 'assistant', 'tool', 'browser_state')
- content: JSONB (message content)
- is_llm_message: bool
- metadata: JSONB (additional metadata)
- created_at: timestamp
- updated_at: timestamp
- agent_id: UUID (optional)
- agent_version_id: UUID (optional)
"""

import json
import os
import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional, Any, Dict

from agentscope.message import (
    Msg,
    TextBlock,
    ToolUseBlock,
    ToolResultBlock,
    ThinkingBlock,
)

from utils.logger import logger
from agentscope_integration.utils.tool_message_sanitizer import (
    ToolMessageSanitizationStats,
    sanitize_tool_message_sequence,
)
from agentscope_integration.memory.micro_compact import PRUNE_PLACEHOLDER
from utils.agent_run_context import get_agent_run_context

DEFAULT_CACHE_CONTRACT_VERSION = "v1"
SUPPORTED_CACHE_CONTRACT_VERSIONS = {"v1"}

SKILL_TOOL_NAMES = {
    "get_available_skills",
    "load_skill",
    "load_reference",
    "list_skill_scripts",
    "run_skill_script",
}
TERMINAL_AGENT_RUN_STATUSES = {"completed", "failed", "stopped", "error"}


class SafeJSONEncoder(json.JSONEncoder):
    """JSON encoder that handles non-serializable objects like Enums."""

    def default(self, obj):
        if isinstance(obj, Enum):
            return sanitize_db_payload(obj.value)
        if isinstance(obj, bytes):
            return strip_nul_chars(obj.decode("utf-8", errors="replace"))
        if isinstance(obj, bytearray):
            return strip_nul_chars(bytes(obj).decode("utf-8", errors="replace"))
        try:
            return super().default(obj)
        except TypeError:
            return strip_nul_chars(str(obj))


def strip_nul_chars(value: str) -> str:
    """Strip embedded NUL characters that PostgreSQL text/jsonb cannot store."""
    if not value:
        return value
    # "\x00" and "\u0000" represent the same code point; a single replacement is enough.
    return value.replace("\x00", "")


def sanitize_db_payload(value: Any) -> Any:
    """Recursively sanitize payload values before persisting to PostgreSQL."""
    if isinstance(value, str):
        return strip_nul_chars(value)
    if isinstance(value, dict):
        sanitized: Dict[Any, Any] = {}
        for key, item in value.items():
            sanitized_key = strip_nul_chars(key) if isinstance(key, str) else key
            sanitized[sanitized_key] = sanitize_db_payload(item)
        return sanitized
    if isinstance(value, list):
        return [sanitize_db_payload(item) for item in value]
    if isinstance(value, tuple):
        return [sanitize_db_payload(item) for item in value]
    if isinstance(value, set):
        return [sanitize_db_payload(item) for item in value]
    if isinstance(value, bytes):
        return strip_nul_chars(value.decode("utf-8", errors="replace"))
    if isinstance(value, bytearray):
        return strip_nul_chars(bytes(value).decode("utf-8", errors="replace"))
    if isinstance(value, Enum):
        return sanitize_db_payload(value.value)
    return value


def safe_json_dumps(obj, **kwargs) -> str:
    """Safely serialize object to JSON string, handling non-serializable types."""
    kwargs.setdefault("ensure_ascii", False)
    kwargs.setdefault("cls", SafeJSONEncoder)
    return json.dumps(sanitize_db_payload(obj), **kwargs)


class MessagesTableMemory:
    """
    Custom Memory adapter that reuses the existing 'messages' table.

    This class provides AgentScope-compatible memory operations while
    storing data in the existing PostgreSQL messages table.

    Supports two modes:
    1. Orchestrator mode (exclude_tool_calls=True):
       - Loads entire thread history
       - Keeps complete tool calls/results for recent historical runs
       - Summarizes older tool history into compact text context
       - Keeps tool calls/results from the current run (if available) so the
         agent can see its own tool outputs

    2. Worker mode (thread_run_id set):
       - Only loads messages from current task execution
       - Includes full tool call details for debugging
       - Can drop tool_call-only messages when drop_tool_call_only=True
    """

    def __init__(
        self,
        db_client,
        thread_id: str,
        project_id: str = None,
        thread_run_id: str = None,
        exclude_tool_calls: bool = False,
        keep_tool_results: bool = False,
        drop_tool_call_only: bool = False,
        retain_recent_tool_runs: int = 0,
        retain_complete_tool_runs: Optional[int] = None,
        enable_tool_history_summary: bool = True,
        tool_history_summary_max_runs: int = 8,
        tool_history_summary_max_chars: int = 8000,
        kv_cache_canonical_json: Optional[bool] = None,
        kv_cache_contract_version: Optional[str] = None,
        agent_id: str = None,
        agent_version_id: str = None,
        enable_micro_compact: bool = True,
        enable_tail_trim: bool = False,
        tail_trim_keep_turns: int = 8,
    ):
        """
        Initialize the memory adapter.

        Args:
            db_client: Supabase/PostgreSQL client
            thread_id: Thread ID for this conversation
            project_id: Project ID (optional)
            thread_run_id: Thread run ID for filtering current task only (optional)
                          When set, only messages from this run are loaded.
                          Used by Worker to see only current task context.
            exclude_tool_calls: If True, exclude messages with tool_calls from get_memory()
                               Used by Orchestrator to avoid "function response parts" errors.
            keep_tool_results: If True, keep tool result messages even when exclude_tool_calls is enabled.
            drop_tool_call_only: If True, drop assistant messages that only contain tool_calls without text.
            retain_recent_tool_runs: Backwards-compatible alias of retain_complete_tool_runs.
            retain_complete_tool_runs: Number of recent historical runs whose raw tool
                                       calls/results should be preserved.
            enable_tool_history_summary: If True, summarize tool calls/results from runs
                                         older than retain_complete_tool_runs.
            tool_history_summary_max_runs: Max number of older runs to summarize.
            tool_history_summary_max_chars: Max chars for synthesized summary message.
            kv_cache_canonical_json: If True, serialize JSON deterministically for
                                     better provider-side prefix cache reuse.
            kv_cache_contract_version: Cache metadata contract version.
            agent_id: Agent ID (optional)
            agent_version_id: Agent version ID (optional)
            enable_tail_trim: If True, remove middle messages while keeping prefix and
                             tail turns — preserves KV cache for both ends.
            tail_trim_keep_turns: Number of recent user turns preserved when trimming.
        """
        self.db_client = db_client
        self.thread_id = thread_id
        self.project_id = project_id
        self.thread_run_id = thread_run_id
        self.exclude_tool_calls = exclude_tool_calls
        self.keep_tool_results = keep_tool_results
        self.drop_tool_call_only = drop_tool_call_only
        if retain_complete_tool_runs is None:
            retain_complete_tool_runs = (
                retain_recent_tool_runs
                if retain_recent_tool_runs and retain_recent_tool_runs > 0
                else int(os.getenv("AGENTSCOPE_RETAIN_COMPLETE_TOOL_RUNS", "3"))
            )
        self.retain_complete_tool_runs = max(0, int(retain_complete_tool_runs or 0))
        # Keep alias for older references/log parsing.
        self.retain_recent_tool_runs = self.retain_complete_tool_runs
        self.enable_tool_history_summary = bool(enable_tool_history_summary)
        self.tool_history_summary_max_runs = max(
            0, int(tool_history_summary_max_runs or 0)
        )
        self.tool_history_summary_max_chars = max(
            0,
            int(tool_history_summary_max_chars or 0),
        )

        self.agent_id = agent_id
        self.agent_version_id = agent_version_id
        self._compressed_summary: str = ""
        self._messages_cache: List[Msg] = []

        self._max_memory_messages = int(
            os.getenv("AGENTSCOPE_MEMORY_MAX_MESSAGES", "300")
        )
        self._max_memory_chars = int(os.getenv("AGENTSCOPE_MEMORY_MAX_CHARS", "600000"))
        self._max_tool_result_chars = int(
            os.getenv("AGENTSCOPE_TOOL_RESULT_MAX_CHARS", "12000")
        )
        self.enable_micro_compact = bool(enable_micro_compact)
        self.enable_tail_trim = bool(enable_tail_trim)
        self.tail_trim_keep_turns = max(2, int(tail_trim_keep_turns))
        if kv_cache_canonical_json is None:
            kv_cache_canonical_json = self._env_flag(
                "AGENTSCOPE_KV_CACHE_CANONICAL_JSON",
                True,
            )
        self._kv_cache_canonical_json = bool(kv_cache_canonical_json)
        if kv_cache_contract_version is None:
            kv_cache_contract_version = os.getenv(
                "AGENTSCOPE_KV_CACHE_CONTRACT_VERSION",
                DEFAULT_CACHE_CONTRACT_VERSION,
            )
        normalized_contract = (
            str(
                kv_cache_contract_version or DEFAULT_CACHE_CONTRACT_VERSION,
            )
            .strip()
            .lower()
        )
        if normalized_contract not in SUPPORTED_CACHE_CONTRACT_VERSIONS:
            normalized_contract = DEFAULT_CACHE_CONTRACT_VERSION
        self._kv_cache_contract_version = normalized_contract

    @staticmethod
    def _env_flag(name: str, default: bool) -> bool:
        value = os.environ.get(name)
        if value is None:
            return default
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    def _coerce_max(self, value: int) -> Optional[int]:
        if value and value > 0:
            return value
        return None

    def _stable_json_dumps(
        self,
        obj: Any,
        *,
        canonical: bool = False,
    ) -> str:
        canonical_enabled = bool(canonical and self._kv_cache_canonical_json)
        kwargs: Dict[str, Any] = {}
        if canonical_enabled:
            kwargs["sort_keys"] = True
            kwargs["separators"] = (",", ":")
        return safe_json_dumps(obj, **kwargs)

    def _serialize_db_json(self, value: Any) -> str:
        return self._stable_json_dumps(value, canonical=True)

    def _truncate_tool_result(self, content: Any) -> Any:
        if not isinstance(content, dict):
            return content
        result = content.get("result")
        if result is None:
            return content
        max_chars = self._coerce_max(self._max_tool_result_chars)
        if not max_chars:
            return content
        if not isinstance(result, str):
            result_str = str(result)
        else:
            result_str = result
        if len(result_str) <= max_chars:
            return content
        truncated = (
            result_str[:max_chars]
            + f"...<truncated {len(result_str) - max_chars} chars>"
        )
        updated = dict(content)
        updated["result"] = truncated
        updated["truncated"] = True
        updated["original_length"] = len(result_str)
        return updated

    def _parse_metadata(self, value: Any) -> Dict[str, Any]:
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                return {}
        return {}

    def _parse_content(self, value: Any) -> Dict[str, Any]:
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                text_value = value.strip()
                return {"content": text_value} if text_value else {}
        if value is None:
            return {}
        return {"content": str(value)}

    def _normalize_row(self, row: Dict[str, Any]) -> Dict[str, Any]:
        normalized = dict(row)
        normalized["metadata"] = self._parse_metadata(row.get("metadata", {}))
        normalized["content"] = self._parse_content(row.get("content", {}))
        return normalized

    def _extract_thread_run_id(self, row: Dict[str, Any]) -> str:
        metadata = row.get("metadata", {})
        if not isinstance(metadata, dict):
            return ""
        run_id = metadata.get("thread_run_id")
        if not run_id:
            return ""
        return str(run_id).strip()

    async def _load_rows_from_db(
        self,
        mark: Optional[str],
        max_messages: Optional[int],
    ) -> List[Dict[str, Any]]:
        query = (
            self.db_client.table("messages").select("*").eq("thread_id", self.thread_id)
        )
        if mark:
            query = query.contains("metadata", {"marks": mark})
        if self.thread_run_id:
            query = query.contains("metadata", {"thread_run_id": self.thread_run_id})
        if max_messages:
            query = query.order("created_at", desc=True).limit(max_messages)
        else:
            query = query.order("created_at")

        result = await query.execute()
        rows = result.data or []
        if max_messages:
            rows = list(reversed(rows))
        return [self._normalize_row(row) for row in rows]

    def _row_has_tool_activity(self, row: Dict[str, Any]) -> bool:
        row_type = row.get("type")
        if row_type == "tool":
            return True
        if row_type != "assistant":
            return False
        content = row.get("content", {})
        if not isinstance(content, dict):
            return False
        tool_calls = content.get("tool_calls")
        return isinstance(tool_calls, list) and len(tool_calls) > 0

    def _row_to_pairing_message(self, row: Dict[str, Any]) -> Dict[str, Any]:
        """Build a provider-style message shape for pairing sanitization."""
        content = row.get("content", {})
        if not isinstance(content, dict):
            content = self._parse_content(content)

        row_type = str(row.get("type") or "").strip().lower()
        if row_type == "tool":
            return {
                "role": "tool",
                "tool_call_id": str(content.get("tool_call_id") or ""),
                "name": str(content.get("tool_name") or ""),
                "content": str(content.get("result") or ""),
                "_row_ref": row,
            }

        role = str(row.get("role") or "").strip().lower()
        if role not in {"assistant", "user", "system", "tool"}:
            role = row_type if row_type in {"assistant", "user", "system"} else "user"

        message: Dict[str, Any] = {
            "role": role,
            "content": content.get("content", ""),
            "_row_ref": row,
        }
        reasoning_content = content.get("reasoning_content")
        if reasoning_content:
            message["reasoning_content"] = reasoning_content
        tool_calls = content.get("tool_calls")
        if isinstance(tool_calls, list):
            message["tool_calls"] = tool_calls
        return message

    def _sanitize_memory_rows_for_tool_pairing(
        self,
        rows: List[Dict[str, Any]],
    ) -> tuple[List[Dict[str, Any]], ToolMessageSanitizationStats]:
        """Ensure replayed row sequence has valid assistant/tool-call pairing."""
        if not rows:
            return rows, ToolMessageSanitizationStats()

        pairing_messages = [self._row_to_pairing_message(row) for row in rows]
        sanitized_messages, stats = sanitize_tool_message_sequence(pairing_messages)

        sanitized_rows: List[Dict[str, Any]] = []
        for message in sanitized_messages:
            row_ref = message.get("_row_ref")
            if not isinstance(row_ref, dict):
                continue

            if str(message.get("role") or "").strip().lower() != "assistant":
                sanitized_rows.append(row_ref)
                continue

            original_content = row_ref.get("content", {})
            if not isinstance(original_content, dict):
                original_content = self._parse_content(original_content)

            updated_content = dict(original_content)
            updated_tool_calls = message.get("tool_calls")
            if isinstance(updated_tool_calls, list) and updated_tool_calls:
                updated_content["tool_calls"] = updated_tool_calls
            else:
                updated_content.pop("tool_calls", None)

            updated_row = dict(row_ref)
            updated_row["content"] = updated_content
            sanitized_rows.append(updated_row)

        return sanitized_rows, stats

    def _short_text(self, value: Any, max_chars: int) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            text = value
        elif isinstance(value, (dict, list)):
            text = safe_json_dumps(value)
        else:
            text = str(value)
        text = " ".join(text.split())
        if len(text) <= max_chars:
            return text
        return text[:max_chars] + f"...<+{len(text) - max_chars} chars>"

    def _estimate_payload_size(self, content: Any) -> int:
        try:
            if isinstance(content, dict):
                return len(safe_json_dumps(content))
            return len(str(content))
        except Exception:
            return len(str(content))

    def _estimate_rows_chars(self, rows: List[Dict[str, Any]]) -> int:
        total_chars = 0
        for row in rows:
            total_chars += self._estimate_payload_size(row.get("content", {}))
        return total_chars

    def _build_tool_summary_lines(
        self,
        row: Dict[str, Any],
        run_id: str,
    ) -> List[str]:
        content = row.get("content", {})
        if not isinstance(content, dict):
            return []
        run_label = run_id[:8] if run_id else "unknown"
        row_type = row.get("type")
        lines: List[str] = []
        if row_type == "assistant":
            tool_calls = content.get("tool_calls")
            if not isinstance(tool_calls, list):
                return []
            for call in tool_calls:
                if not isinstance(call, dict):
                    continue
                function = call.get("function", {})
                if not isinstance(function, dict):
                    function = {}
                tool_name = function.get("name") or "unknown_tool"
                args_value = function.get("arguments", "")
                args_text = self._short_text(args_value, 160)
                lines.append(f"run={run_label} tool={tool_name} args={args_text}")
            return lines

        if row_type == "tool":
            tool_name = str(content.get("tool_name") or "unknown_tool")
            result_text = self._short_text(content.get("result", ""), 220)
            lowered = result_text.lower()
            is_error = any(
                token in lowered
                for token in ("error", "failed", "exception", "traceback")
            )
            outcome = "error" if is_error else "ok"
            lines.append(
                f"run={run_label} tool={tool_name} outcome={outcome} result={result_text}"
            )
        return lines

    def _build_tool_history_summary_message(self, lines: List[str]) -> Optional[Msg]:
        if not lines or not self.enable_tool_history_summary:
            return None

        body = "\n".join(f"- {line}" for line in lines)
        max_chars = self._coerce_max(self.tool_history_summary_max_chars)
        if max_chars:
            marker = "\n...<tool history summary truncated>"
            if len(body) > max_chars:
                cut = max(0, max_chars - len(marker))
                body = body[:cut] + marker

        summary_text = (
            "<tool-history-summary>\n"
            "Older tool executions (compact):\n"
            f"{body}\n"
            "</tool-history-summary>"
        )
        return Msg("user", summary_text, "user")

    async def _get_memory_kv_ready(
        self,
        rows: List[Dict[str, Any]],
        current_run_id: str,
        mark: Optional[str],
        exclude_mark: Optional[str],
        max_messages: Optional[int],
    ) -> List[Msg]:
        messages: List[Msg] = []
        skipped_by_mark = 0
        skipped_tool_call_only = 0
        skipped_orphan_tool_results = 0
        skipped_malformed_tool_results = 0
        stripped_unmatched_assistant_tool_calls = 0
        skipped_parse_error = 0
        candidate_rows: List[Dict[str, Any]] = []

        for row in rows:
            metadata = row.get("metadata", {})
            content = row.get("content", {})

            if exclude_mark:
                msg_marks = metadata.get("marks", "")
                if msg_marks == exclude_mark or (
                    isinstance(msg_marks, list) and exclude_mark in msg_marks
                ):
                    skipped_by_mark += 1
                    continue

            tool_calls = (
                content.get("tool_calls") if isinstance(content, dict) else None
            )
            has_tool_calls = isinstance(tool_calls, list) and len(tool_calls) > 0

            if self.drop_tool_call_only and has_tool_calls:
                content_text = str(content.get("content") or "")
                reasoning_text = str(content.get("reasoning_content") or "")
                if not content_text.strip() and not reasoning_text.strip():
                    skipped_tool_call_only += 1
                    continue

            candidate_rows.append(row)

        candidate_rows, sanitized_stats = self._sanitize_memory_rows_for_tool_pairing(
            candidate_rows,
        )
        skipped_orphan_tool_results += sanitized_stats.dropped_orphan_tool_messages
        skipped_malformed_tool_results += sanitized_stats.dropped_empty_tool_messages
        stripped_unmatched_assistant_tool_calls += (
            sanitized_stats.stripped_unmatched_assistant_tool_calls
        )
        skipped_tool_call_only += (
            sanitized_stats.dropped_tool_call_only_assistant_messages
        )

        for row in candidate_rows:
            msg = self._row_to_msg(row)
            if msg:
                messages.append(msg)
            else:
                skipped_parse_error += 1

        logger.info(
            "[MessagesTableMemory] Loaded %s messages "
            "(skipped_by_mark=%s, skipped_tool_call_only=%s, skipped_orphan_tool_results=%s, "
            "skipped_malformed_tool_results=%s, stripped_unmatched_assistant_tool_calls=%s, "
            "skipped_parse_error=%s) "
            "for thread_id=%s",
            len(messages),
            skipped_by_mark,
            skipped_tool_call_only,
            skipped_orphan_tool_results,
            skipped_malformed_tool_results,
            stripped_unmatched_assistant_tool_calls,
            skipped_parse_error,
            self.thread_id,
        )

        return messages

    async def add(self, msg: Msg, marks: str = None) -> Optional[Dict]:
        """
        Add a message to the messages table.

        Args:
            msg: AgentScope Msg object
            marks: Optional mark/tag for the message

        Returns:
            Inserted message data or None
        """
        terminal_status = await self._current_agent_run_terminal_status()
        if terminal_status:
            logger.warning(
                "[MessagesTableMemory] Skipping message add for terminal run: "
                "thread_id=%s thread_run_id=%s status=%s role=%s",
                self.thread_id,
                self.thread_run_id,
                terminal_status,
                getattr(msg, "role", None),
            )
            return None

        # Convert AgentScope Msg to messages table format
        msg_type = self._get_message_type(msg)
        content = self._build_content(msg)
        metadata = self._build_metadata(msg, marks)

        data_to_insert = {
            "thread_id": self.thread_id,
            "project_id": self.project_id,
            "type": msg_type,
            "role": msg.role or "user",  # Required NOT NULL field
            "content": (
                self._serialize_db_json(content)
                if isinstance(content, dict)
                else sanitize_db_payload(content)
            ),
            "is_llm_message": msg.role == "assistant",
            "metadata": (
                self._serialize_db_json(metadata)
                if isinstance(metadata, dict)
                else sanitize_db_payload(metadata)
            ),
        }

        if self.agent_id:
            data_to_insert["agent_id"] = self.agent_id
        if self.agent_version_id:
            data_to_insert["agent_version_id"] = self.agent_version_id

        try:
            # Note: insert() is an async method that directly returns QueryResult
            # No need to call .execute() after insert()
            result = await self.db_client.table("messages").insert(data_to_insert)

            if result.data and len(result.data) > 0:
                # Add to cache
                self._messages_cache.append(msg)
                logger.debug(
                    f"[MessagesTableMemory] Added message: type={msg_type}, role={msg.role}, "
                    f"thread_id={self.thread_id}, thread_run_id={self.thread_run_id}"
                )
                return result.data[0]
            return None
        except Exception as e:
            logger.error(f"[MessagesTableMemory] Failed to add message: {e}")
            raise

    async def _current_agent_run_terminal_status(self) -> Optional[str]:
        agent_run_id = str(self.thread_run_id or "").strip()
        if not agent_run_id:
            context_agent_run_id, _ = get_agent_run_context()
            agent_run_id = str(context_agent_run_id or "").strip()
        if not agent_run_id:
            return None
        try:
            result = (
                await self.db_client.table("agent_runs")
                .select("status")
                .eq("agent_run_id", agent_run_id)
                .limit(1)
                .execute()
            )
            rows = list(getattr(result, "data", None) or [])
            if not rows:
                return None
            status = str(rows[0].get("status") or "").strip().lower()
            return status if status in TERMINAL_AGENT_RUN_STATUSES else None
        except Exception as terminal_check_error:
            logger.warning(
                "[MessagesTableMemory] Failed to check terminal state for run=%s: %s",
                agent_run_id,
                terminal_check_error,
            )
            return None

    def _is_skill_related_row(self, row: Dict[str, Any]) -> bool:
        """Check if a memory row is skill-related (load_skill, run_skill_script, etc.).

        Skill messages contain reference code that agents depend on in
        subsequent turns. They must survive ``exclude_tool_calls`` filtering.
        """
        content = row.get("content", {})
        if not isinstance(content, dict):
            return False
        row_type = row.get("type")
        if row_type == "tool":
            tool_name = content.get("tool_name", "")
            return tool_name in SKILL_TOOL_NAMES
        if row_type == "assistant":
            tool_calls = content.get("tool_calls")
            if isinstance(tool_calls, list):
                for tc in tool_calls:
                    if isinstance(tc, dict):
                        if tc.get("name", "") in SKILL_TOOL_NAMES:
                            return True
                        func = tc.get("function", {})
                        if (
                            isinstance(func, dict)
                            and func.get("name", "") in SKILL_TOOL_NAMES
                        ):
                            return True
        return False

    def _is_human_user_memory_msg(self, msg: Msg) -> bool:
        if getattr(msg, "role", "") != "user":
            return False
        if str(getattr(msg, "name", "") or "").strip().lower() == "tool":
            return False
        try:
            blocks = (
                msg.get_content_blocks() if hasattr(msg, "get_content_blocks") else []
            )
        except Exception:
            blocks = []
        if any(
            block.get("type") == "tool_result"
            for block in blocks
            if isinstance(block, dict)
        ):
            return False
        return True

    def _apply_tail_trim(self, messages: List[Msg]) -> List[Msg]:
        """Remove middle messages, keeping prefix (system) and tail (recent turns).

        Preserves KV cache for both ends: the system-prefix doesn't change,
        and the tail stays at a stable position.  Only the middle is removed,
        which would otherwise grow linearly with conversation length.
        """
        keep_turns = self.tail_trim_keep_turns

        # 1. Identify prefix (system messages at the start).  Also preserve
        # the first real human user message as the task contract.  Tool results
        # are represented as role='user' locally, so they must not count as
        # user-turn anchors; otherwise long tool-heavy runs can trim away the
        # original request while keeping only recent tool observations.
        prefix_end = 0
        for i, msg in enumerate(messages):
            if getattr(msg, "role", "") == "system":
                prefix_end = i + 1
            else:
                break
        first_human_user_index = next(
            (
                i
                for i, msg in enumerate(messages[prefix_end:], start=prefix_end)
                if self._is_human_user_memory_msg(msg)
            ),
            None,
        )
        if first_human_user_index is not None:
            prefix_end = max(prefix_end, first_human_user_index + 1)

        # 2. Find user-turn boundaries from the end.
        # With exclude_tool_calls=True, user messages may be sparse.
        # Fall back to message-count-based trimming when turns are too few.
        user_indices = [
            i for i, msg in enumerate(messages) if self._is_human_user_memory_msg(msg)
        ]

        if len(user_indices) > keep_turns:
            tail_start = user_indices[-keep_turns]
        else:
            # Not enough user turns — keep the last 10 messages.
            tail_start = max(prefix_end, len(messages) - 10)

        tail_start = max(tail_start, prefix_end)

        removed = len(messages) - prefix_end - (len(messages) - tail_start)
        logger.info(
            "[TailTrim] users=%d keep_turns=%d prefix=%d tail_start=%d removed=%d",
            len(user_indices),
            keep_turns,
            prefix_end,
            tail_start,
            removed,
        )
        if removed <= 0:
            return messages

        trimmed = messages[:prefix_end] + messages[tail_start:]
        logger.info(
            "[TailTrim] Removed %d middle messages, kept %d prefix + %d tail = %d total (keep_turns=%d)",
            removed,
            prefix_end,
            len(messages) - tail_start,
            len(trimmed),
            keep_turns,
        )
        return trimmed

    async def prune_old_tool_outputs(self) -> int:
        """Micro Compact: mark stale tool outputs with ``time_compacted``."""
        if not self.enable_micro_compact:
            return 0
        try:
            from .micro_compact import PRUNE_MINIMUM, PRUNE_PROTECT, prune_tool_outputs

            result = (
                await self.db_client.table("messages")
                .select("*")
                .eq("thread_id", self.thread_id)
                .eq("type", "tool")
                .order("created_at", desc=False)
                .execute()
            )

            rows = result.data if result.data else []
            if not rows:
                logger.info(
                    "[MessagesTableMemory] Micro Compact: no tool rows "
                    "for thread=%s",
                    self.thread_id,
                )
                return 0

            active_rows = []
            for r in rows:
                meta = r.get("metadata")
                if isinstance(meta, str):
                    try:
                        import json as _json

                        meta = _json.loads(meta)
                    except Exception:
                        meta = {}
                if isinstance(meta, dict) and meta.get("time_compacted"):
                    continue
                active_rows.append(r)

            if not active_rows:
                return 0

            pruned_rows = prune_tool_outputs(
                active_rows,
                protect=PRUNE_PROTECT,
                minimum=PRUNE_MINIMUM,
            )

            compacted_count = 0
            missing_id = 0
            for row in pruned_rows:
                meta = row.get("metadata")
                if isinstance(meta, dict) and meta.get("time_compacted"):
                    row_id = row.get("message_id") or row.get("id")
                    if not row_id:
                        missing_id += 1
                        continue
                    await self.db_client.table("messages").eq(
                        "message_id", str(row_id)
                    ).update({"metadata": self._serialize_db_json(meta)})
                    compacted_count += 1

            logger.info(
                "[MessagesTableMemory] Micro Compact pruned %d tool outputs "
                "(missing_id=%d) for thread=%s",
                compacted_count,
                missing_id,
                self.thread_id,
            )
            return compacted_count
        except Exception as exc:
            logger.error(
                "[MessagesTableMemory] Micro Compact prune failed: %s: %s",
                type(exc).__name__,
                exc,
            )
            import traceback

            logger.error(traceback.format_exc())
            return 0

    async def get_memory(
        self,
        mark: str = None,
        exclude_mark: str = None,
        prepend_summary: bool = True,
        **kwargs: Any,
    ) -> List[Msg]:
        """
        Get messages from the messages table.

        Filtering logic:
        1. If thread_run_id is set, only return messages from that run
           (used by Worker to see only current task)
        2. If exclude_tool_calls is True, keep full tool history only for recent
           runs and summarize older tool history
        3. If mark is set, only return messages with that mark
        4. If exclude_mark is set, exclude messages with that mark
           (used by AgentScope's memory compression)

        Args:
            mark: Optional filter by mark (stored in metadata)
            exclude_mark: Optional exclude messages with this mark (for compression)
            prepend_summary: Whether to prepend the compressed summary

        Returns:
            List of AgentScope Msg objects
        """
        logger.debug(
            f"[MessagesTableMemory] get_memory called: thread_id={self.thread_id}, "
            f"thread_run_id={self.thread_run_id}, exclude_tool_calls={self.exclude_tool_calls}, "
            f"keep_tool_results={self.keep_tool_results}, "
            f"drop_tool_call_only={self.drop_tool_call_only}, "
            f"retain_complete_tool_runs={self.retain_complete_tool_runs}, "
            f"enable_tool_history_summary={self.enable_tool_history_summary}, "
            f"mark={mark}, exclude_mark={exclude_mark}"
        )

        try:
            max_messages = self._coerce_max(self._max_memory_messages)
            max_chars = self._coerce_max(self._max_memory_chars)

            rows = await self._load_rows_from_db(
                mark=mark,
                max_messages=max_messages,
            )
            if not rows:
                logger.debug(
                    f"[MessagesTableMemory] No messages found for thread_id={self.thread_id}"
                )
                return []
            logger.debug(f"[MessagesTableMemory] Found {len(rows)} raw messages in DB")

            current_run_id = str(
                getattr(self, "_save_thread_run_id", None) or self.thread_run_id or ""
            ).strip()

            # Convert to AgentScope Msg objects with filtering
            messages = []
            skipped_tool = 0
            skipped_tool_calls = 0
            skipped_tool_call_only = 0
            skipped_orphan_tool_results = 0
            skipped_malformed_tool_results = 0
            stripped_unmatched_assistant_tool_calls = 0
            skill_exempt = 0  # skill rows exempted from exclude_tool_calls filter
            skipped_parse_error = 0
            skipped_by_mark = 0
            skipped_by_budget = 0
            skipped_old_tool_results = 0

            retained_run_ids = set()
            if (
                self.exclude_tool_calls
                and self.keep_tool_results
                and self.retain_complete_tool_runs > 0
            ):
                for row in reversed(rows):
                    run_id = self._extract_thread_run_id(row)
                    if (
                        not run_id
                        or run_id == current_run_id
                        or run_id in retained_run_ids
                    ):
                        continue
                    if not self._row_has_tool_activity(row):
                        continue
                    retained_run_ids.add(run_id)
                    if len(retained_run_ids) >= self.retain_complete_tool_runs:
                        break

            summary_run_ids: List[str] = []
            if (
                self.exclude_tool_calls
                and self.enable_tool_history_summary
                and self.tool_history_summary_max_runs > 0
            ):
                for row in reversed(rows):
                    run_id = self._extract_thread_run_id(row)
                    if (
                        not run_id
                        or run_id == current_run_id
                        or run_id in retained_run_ids
                        or run_id in summary_run_ids
                    ):
                        continue
                    if not self._row_has_tool_activity(row):
                        continue
                    summary_run_ids.append(run_id)
                    if len(summary_run_ids) >= self.tool_history_summary_max_runs:
                        break
            summary_run_id_set = set(summary_run_ids)
            summary_lines: List[str] = []
            filtered_rows: List[Dict[str, Any]] = []

            for row in rows:
                metadata = row.get("metadata", {})
                content = row.get("content", {})

                # Apply exclude_mark filter (for memory compression)
                if exclude_mark:
                    msg_marks = metadata.get("marks", "")
                    if msg_marks == exclude_mark or (
                        isinstance(msg_marks, list) and exclude_mark in msg_marks
                    ):
                        skipped_by_mark += 1
                        continue

                row_type = row.get("type")
                run_id = self._extract_thread_run_id(row)
                is_current_run = bool(current_run_id and run_id == current_run_id)
                tool_calls = (
                    content.get("tool_calls") if isinstance(content, dict) else None
                )
                has_tool_calls = isinstance(tool_calls, list) and len(tool_calls) > 0
                is_tool_row = row_type == "tool"
                has_tool_activity = is_tool_row or (
                    row_type == "assistant" and has_tool_calls
                )

                # Apply exclude_tool_calls filter (Orchestrator mode)
                # Keep tool calls/results from the current run so the agent
                # can see its own tool outputs and avoid repeated calls.
                is_skill_row = self._is_skill_related_row(row)
                if self.exclude_tool_calls and not is_current_run:
                    if is_skill_row:
                        skill_exempt += 1
                if self.exclude_tool_calls and not is_current_run and not is_skill_row:
                    if is_tool_row and not self.keep_tool_results:
                        skipped_tool += 1
                        continue

                    if has_tool_activity and run_id not in retained_run_ids:
                        if run_id in summary_run_id_set:
                            summary_lines.extend(
                                self._build_tool_summary_lines(row, run_id),
                            )
                        if is_tool_row:
                            skipped_old_tool_results += 1
                        else:
                            skipped_tool_calls += 1
                        continue

                # Drop tool_call-only messages to satisfy Gemini parts requirements
                if self.drop_tool_call_only and has_tool_calls:
                    content_text = str(content.get("content") or "")
                    reasoning_text = str(content.get("reasoning_content") or "")
                    if not content_text.strip() and not reasoning_text.strip():
                        skipped_tool_call_only += 1
                        if (
                            self.exclude_tool_calls
                            and not is_current_run
                            and run_id in summary_run_id_set
                        ):
                            summary_lines.extend(
                                self._build_tool_summary_lines(row, run_id),
                            )
                        continue

                filtered_rows.append(row)

            if max_chars:
                selected_rows = []
                total_chars = 0
                for row in reversed(filtered_rows):
                    payload_size = self._estimate_payload_size(row.get("content", {}))
                    if total_chars + payload_size > max_chars:
                        skipped_by_budget += 1
                        continue
                    selected_rows.append(row)
                    total_chars += payload_size
                filtered_rows = list(reversed(selected_rows))

            filtered_rows, pairing_stats = self._sanitize_memory_rows_for_tool_pairing(
                filtered_rows,
            )
            skipped_orphan_tool_results += pairing_stats.dropped_orphan_tool_messages
            skipped_malformed_tool_results += pairing_stats.dropped_empty_tool_messages
            stripped_unmatched_assistant_tool_calls += (
                pairing_stats.stripped_unmatched_assistant_tool_calls
            )
            skipped_tool_call_only += (
                pairing_stats.dropped_tool_call_only_assistant_messages
            )

            for row in filtered_rows:
                msg = self._row_to_msg(row)
                if msg:
                    messages.append(msg)
                else:
                    skipped_parse_error += 1

            summary_msg = self._build_tool_history_summary_message(summary_lines)
            if summary_msg:
                messages = [summary_msg, *messages]

            logger.info(
                f"[MessagesTableMemory] Loaded {len(messages)} messages "
                f"(skipped: {skipped_tool} tool, {skipped_old_tool_results} old_tool_results, "
                f"{skipped_tool_calls} tool_calls, {skipped_tool_call_only} tool_call_only, "
                f"{skipped_orphan_tool_results} orphan_tool_results, "
                f"{skipped_malformed_tool_results} malformed_tool_results, "
                f"{stripped_unmatched_assistant_tool_calls} unmatched_assistant_tool_calls, "
                f"{skipped_by_mark} by_mark, {skipped_by_budget} by_budget, "
                f"{skill_exempt} skill_exempt, "
                f"{skipped_parse_error} parse errors, retained_runs={sorted(retained_run_ids)}, "
                f"summary_runs={summary_run_ids}, "
                f"max_chars={max_chars}) "
                f"for thread_id={self.thread_id}"
            )

            # TailTrim: remove middle messages while preserving prefix + tail.
            # This keeps KV cache hits high for both the system-prefix AND the
            # tail (most recent turns stay at a stable position).
            tt_trigger = self.tail_trim_keep_turns * 2
            logger.info(
                "[TailTrim] enable=%s msgs=%d keep_turns=%d trigger=%d",
                self.enable_tail_trim,
                len(messages),
                self.tail_trim_keep_turns,
                tt_trigger,
            )
            if self.enable_tail_trim and len(messages) > tt_trigger:
                messages = self._apply_tail_trim(messages)

            prefix_messages: List[Msg] = []
            if prepend_summary and self._compressed_summary:
                prefix_messages.append(
                    Msg(
                        "user",
                        self._compressed_summary,
                        "user",
                    ),
                )

            return [*prefix_messages, *messages]
        except Exception as e:
            logger.error(f"[MessagesTableMemory] Failed to get memory: {e}")
            return []

    async def update_compressed_summary(self, summary: str) -> None:
        """Update the compressed summary of the memory."""
        self._compressed_summary = summary

    async def update_messages_mark(
        self,
        new_mark: str | None,
        old_mark: str | None = None,
        msg_ids: List[str] | None = None,
    ) -> int:
        """
        Update marks for messages in the storage.

        Args:
            new_mark: Mark to set; if None, remove the old mark.
            old_mark: Only update messages with this mark (optional).
            msg_ids: Agentscope message IDs to update (optional).
        """
        try:
            result = (
                await self.db_client.table("messages")
                .select("message_id, metadata")
                .eq("thread_id", self.thread_id)
                .execute()
            )

            if not result.data:
                return 0

            updated_count = 0
            for row in result.data:
                metadata_raw = row.get("metadata", "{}")
                if isinstance(metadata_raw, str):
                    try:
                        metadata = json.loads(metadata_raw)
                    except Exception:
                        metadata = {}
                else:
                    metadata = metadata_raw or {}

                agentscope_msg_id = metadata.get("agentscope_msg_id")
                if msg_ids is not None and agentscope_msg_id not in msg_ids:
                    continue

                marks_value = metadata.get("marks")
                if isinstance(marks_value, list):
                    marks = [m for m in marks_value if m]
                elif isinstance(marks_value, str) and marks_value:
                    marks = [marks_value]
                else:
                    marks = []

                if old_mark is not None and old_mark not in marks:
                    continue

                if new_mark is None:
                    if old_mark in marks:
                        marks.remove(old_mark)
                    else:
                        continue
                else:
                    if old_mark is not None and old_mark in marks:
                        marks.remove(old_mark)
                    if new_mark not in marks:
                        marks.append(new_mark)

                if len(marks) == 0:
                    metadata.pop("marks", None)
                elif len(marks) == 1:
                    metadata["marks"] = marks[0]
                else:
                    metadata["marks"] = marks

                await self.db_client.table("messages").eq(
                    "message_id", row["message_id"]
                ).update(
                    {
                        "metadata": safe_json_dumps(metadata),
                    }
                )
                updated_count += 1

            return updated_count
        except Exception as e:
            logger.error(f"[MessagesTableMemory] Failed to update marks: {e}")
            return 0

    async def delete_by_mark(self, mark: str) -> int:
        """
        Delete messages with a specific mark.

        Args:
            mark: Mark to filter by

        Returns:
            Number of deleted messages
        """
        try:
            # Get messages with this mark
            result = (
                await self.db_client.table("messages")
                .select("message_id")
                .eq("thread_id", self.thread_id)
                .contains("metadata", {"marks": mark})
                .execute()
            )

            if not result.data:
                return 0

            # Delete them
            # Note: delete() is an async method, so we need to call eq() first, then delete()
            message_ids = [row["message_id"] for row in result.data]
            for msg_id in message_ids:
                await self.db_client.table("messages").eq("message_id", msg_id).delete()

            return len(message_ids)
        except Exception as e:
            logger.error(f"[MessagesTableMemory] Failed to delete by mark: {e}")
            return 0

    async def clear(self):
        """Clear all messages for this thread (use with caution)."""
        try:
            # Note: delete() is an async method, so we need to call eq() first, then delete()
            await self.db_client.table("messages").eq(
                "thread_id", self.thread_id
            ).delete()
            self._messages_cache.clear()
        except Exception as e:
            logger.error(f"[MessagesTableMemory] Failed to clear: {e}")
            raise

    def _get_message_type(self, msg: Msg) -> str:
        """Determine message type from AgentScope Msg."""
        blocks = msg.get_content_blocks() if hasattr(msg, "get_content_blocks") else []

        # Check for tool-related blocks
        tool_use_blocks = [b for b in blocks if b.get("type") == "tool_use"]
        tool_result_blocks = [b for b in blocks if b.get("type") == "tool_result"]

        if tool_result_blocks:
            return "tool"
        elif tool_use_blocks:
            return "assistant"  # Tool calls are part of assistant messages
        elif msg.role == "user":
            return "user"
        elif msg.role == "assistant":
            return "assistant"
        elif msg.role == "system":
            return "system"
        else:
            return msg.role or "unknown"

    def _build_content(self, msg: Msg) -> Dict:
        """Build content dict from AgentScope Msg."""
        blocks = msg.get_content_blocks() if hasattr(msg, "get_content_blocks") else []

        content = {
            "role": msg.role,
            "content": msg.get_text_content() or "",
        }

        metadata_media_refs: List[Dict[str, Any]] = []
        msg_metadata = getattr(msg, "metadata", None)
        if isinstance(msg_metadata, dict):
            raw_refs = msg_metadata.get("media_refs")
            if isinstance(raw_refs, list):
                for ref in raw_refs:
                    if not isinstance(ref, dict):
                        continue
                    kind = str(ref.get("kind") or "").strip().lower()
                    path = str(ref.get("path") or "").strip()
                    mime_type = str(ref.get("mime_type") or "").strip().lower()
                    filename = str(ref.get("filename") or "").strip()
                    if (
                        kind == "image"
                        and path.startswith("/workspace/")
                        and mime_type.startswith("image/")
                        and filename
                    ):
                        clean_ref = {
                            "kind": "image",
                            "path": path,
                            "mime_type": mime_type,
                            "filename": filename,
                        }
                        sha256 = str(ref.get("sha256") or "").strip().lower()
                        if sha256:
                            clean_ref["sha256"] = sha256
                        metadata_media_refs.append(clean_ref)

        if metadata_media_refs:
            content["media_refs"] = metadata_media_refs

        # Add thinking/reasoning content
        thinking_blocks = [b for b in blocks if b.get("type") == "thinking"]
        if thinking_blocks:
            content["reasoning_content"] = thinking_blocks[0].get("thinking", "")

        # Add tool calls
        tool_use_blocks = [b for b in blocks if b.get("type") == "tool_use"]
        if tool_use_blocks:
            content["tool_calls"] = [
                {
                    "id": tb.get("id"),
                    "type": "function",
                    "function": {
                        "name": tb.get("name"),
                        "arguments": self._stable_json_dumps(
                            tb.get("input", {}),
                            canonical=True,
                        ),
                    },
                }
                for tb in tool_use_blocks
            ]

        # Add tool results
        tool_result_blocks = [b for b in blocks if b.get("type") == "tool_result"]
        if tool_result_blocks:
            tr = tool_result_blocks[0]
            output = tr.get("output", [])
            if isinstance(output, list) and output:
                result_text = output[0].get("text", "")
            else:
                result_text = str(output)

            content = {
                "tool_name": tr.get("name"),
                "tool_call_id": tr.get("id"),
                "result": result_text,
            }

        return content

    def _build_metadata(self, msg: Msg, marks: str = None) -> Dict:
        """Build metadata dict from AgentScope Msg."""
        metadata = {
            "agentscope_msg_id": msg.id,
            "timestamp": (
                msg.timestamp
                if hasattr(msg, "timestamp")
                else datetime.now(timezone.utc).isoformat()
            ),
        }

        # Add thread_run_id for filtering (used by Worker)
        # Check both thread_run_id and _save_thread_run_id (for Orchestrator which
        # uses _save_thread_run_id for saving but not for loading)
        save_run_id = getattr(self, "_save_thread_run_id", None) or self.thread_run_id
        if save_run_id:
            metadata["thread_run_id"] = save_run_id

        if marks:
            metadata["marks"] = marks

        if hasattr(msg, "metadata") and msg.metadata:
            metadata.update(msg.metadata)

        return metadata

    def _row_to_msg(self, row: Dict) -> Optional[Msg]:
        """Convert a database row to AgentScope Msg."""
        try:
            msg_type = row.get("type", "unknown")
            content_raw = row.get("content", "{}")
            metadata_raw = row.get("metadata", "{}")

            # Parse content
            if isinstance(content_raw, str):
                content = json.loads(content_raw)
            else:
                content = content_raw

            if isinstance(metadata_raw, str):
                metadata = json.loads(metadata_raw)
            elif isinstance(metadata_raw, dict):
                metadata = dict(metadata_raw)
            else:
                metadata = {}

            # Determine role.
            # Tool result observations intentionally use role='user' in
            # AgentScope memory.  The OpenAI-compatible formatters still emit
            # ToolResultBlock as provider-level role='tool', while the local
            # ReAct loop sees observations as high-salience user turns instead
            # of low-salience system metadata.
            if msg_type == "user":
                role = "user"
            elif msg_type == "assistant":
                role = "assistant"
            elif msg_type == "tool":
                role = "user"
            elif msg_type == "system":
                role = "system"
            else:
                role = "user"

            # Build content blocks
            content_blocks = []

            # Text content
            text_content = content.get("content", "")
            if text_content:
                content_blocks.append(TextBlock(type="text", text=text_content))

            # Reasoning content
            reasoning = content.get("reasoning_content")
            if reasoning:
                content_blocks.append(
                    ThinkingBlock(type="thinking", thinking=reasoning)
                )

            # Tool calls
            tool_calls = content.get("tool_calls", [])
            for tc in tool_calls:
                func = tc.get("function", {})
                args = func.get("arguments", "{}")
                if isinstance(args, str):
                    args = json.loads(args)
                content_blocks.append(
                    ToolUseBlock(
                        type="tool_use",
                        id=tc.get("id", str(uuid.uuid4())),
                        name=func.get("name", ""),
                        input=args,
                    )
                )

            # Tool results
            if msg_type == "tool":
                tool_name = content.get("tool_name", "")
                if metadata.get("time_compacted"):
                    tool_result = PRUNE_PLACEHOLDER
                else:
                    tool_result = content.get("result", "")
                tool_call_id = str(content.get("tool_call_id") or "").strip()
                if not tool_call_id:
                    logger.warning(
                        "[MessagesTableMemory] Skipping tool row with empty tool_call_id",
                    )
                    return None
                content_blocks.append(
                    ToolResultBlock(
                        type="tool_result",
                        id=tool_call_id,
                        name=tool_name,
                        output=[TextBlock(type="text", text=tool_result)],
                    )
                )

            media_refs_raw = content.get("media_refs", [])
            clean_media_refs: List[Dict[str, str]] = []
            if isinstance(media_refs_raw, list) and media_refs_raw:
                media_lines = []
                for ref in media_refs_raw:
                    if not isinstance(ref, dict):
                        continue
                    kind = str(ref.get("kind") or "").strip().lower()
                    path = str(ref.get("path") or "").strip()
                    mime_type = str(ref.get("mime_type") or "").strip().lower()
                    filename = str(ref.get("filename") or "").strip()
                    if (
                        kind == "image"
                        and path.startswith("/workspace/")
                        and mime_type.startswith("image/")
                        and filename
                    ):
                        media_lines.append(f"- {filename} ({path})")
                        clean_ref = {
                            "kind": "image",
                            "path": path,
                            "mime_type": mime_type,
                            "filename": filename,
                        }
                        sha256 = str(ref.get("sha256") or "").strip().lower()
                        if sha256:
                            clean_ref["sha256"] = sha256
                        clean_media_refs.append(clean_ref)

                if media_lines:
                    content_blocks.append(
                        TextBlock(
                            type="text",
                            text=(
                                "Previously uploaded image references:\n"
                                + "\n".join(media_lines)
                            ),
                        ),
                    )

            # Create Msg
            msg = Msg(
                name=msg_type,
                content=content_blocks if content_blocks else text_content,
                role=role,
            )
            restored_metadata = dict(metadata) if isinstance(metadata, dict) else {}
            if clean_media_refs:
                restored_metadata["media_refs"] = clean_media_refs
            if restored_metadata:
                msg.metadata = {
                    **(getattr(msg, "metadata", {}) or {}),
                    **restored_metadata,
                }

            return msg
        except Exception as e:
            content_preview = str(row.get("content", ""))[:100]
            logger.warning(
                f"[MessagesTableMemory] Failed to convert row to Msg: {e}, "
                f"row_type={row.get('type')}, content_preview={content_preview}"
            )
            return None

    def state_dict(self) -> Dict:
        """Export memory state (for compatibility)."""
        return {
            "thread_id": self.thread_id,
            "project_id": self.project_id,
            "messages_count": len(self._messages_cache),
        }

    def load_state_dict(self, state: Dict):
        """Load memory state (for compatibility)."""
        # State is stored in database, nothing to load
        pass
