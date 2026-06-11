"""Shared tool-call/message sequence sanitizer for OpenAI-style chat payloads."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ToolMessageSanitizationStats:
    """Counters describing how many malformed entries were removed."""

    dropped_empty_tool_messages: int = 0
    dropped_orphan_tool_messages: int = 0
    stripped_unmatched_assistant_tool_calls: int = 0
    dropped_tool_call_only_assistant_messages: int = 0

    def has_changes(self) -> bool:
        return any(
            (
                self.dropped_empty_tool_messages,
                self.dropped_orphan_tool_messages,
                self.stripped_unmatched_assistant_tool_calls,
                self.dropped_tool_call_only_assistant_messages,
            ),
        )


def _should_normalize_empty_content(msg: dict[str, Any]) -> bool:
    """Check if an assistant message needs content='' to satisfy API requirements."""
    if msg.get("role") != "assistant":
        return False
    content = msg.get("content")
    tool_calls = msg.get("tool_calls")
    if content is not None:
        return False
    if tool_calls:
        return False
    return True


def _has_meaningful_content(msg: dict[str, Any]) -> bool:
    content = msg.get("content")
    if content is None:
        has_reasoning = str(msg.get("reasoning_content") or "").strip()
        return bool(has_reasoning)

    if isinstance(content, str):
        if content.strip():
            return True
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, str) and block.strip():
                return True
            if not isinstance(block, dict):
                continue
            block_type = str(block.get("type") or "")
            if block_type == "text":
                if str(block.get("text") or "").strip():
                    return True
            elif block:
                return True
    elif isinstance(content, dict):
        text_value = content.get("text") or content.get("content")
        if text_value is None:
            if content:
                return True
        elif str(text_value).strip():
            return True
    elif str(content).strip():
        return True

    has_reasoning = str(msg.get("reasoning_content") or "").strip()
    return bool(has_reasoning)


def _extract_valid_tool_calls(msg: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    raw_calls = msg.get("tool_calls")
    if not isinstance(raw_calls, list):
        return [], 0

    valid_calls: list[dict[str, Any]] = []
    invalid_count = 0
    for call in raw_calls:
        if not isinstance(call, dict):
            invalid_count += 1
            continue
        call_id = str(call.get("id") or "").strip()
        if not call_id:
            invalid_count += 1
            continue
        valid_calls.append(call)
    return valid_calls, invalid_count


def sanitize_tool_message_sequence(
    messages: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], ToolMessageSanitizationStats]:
    """Sanitize assistant/tool pairing so every tool result matches a preceding call."""

    sanitized: list[dict[str, Any]] = []
    idx = 0
    dropped_empty = 0
    dropped_orphan = 0
    stripped_unmatched_calls = 0
    dropped_tool_call_only_assistant = 0

    while idx < len(messages):
        msg = messages[idx]
        role = str(msg.get("role") or "").strip().lower()

        if role not in {"assistant", "tool"}:
            sanitized.append(msg)
            idx += 1
            continue

        if role == "tool":
            tool_call_id = str(msg.get("tool_call_id") or "").strip()
            if not tool_call_id:
                dropped_empty += 1
            else:
                dropped_orphan += 1
            idx += 1
            continue

        valid_calls, invalid_call_count = _extract_valid_tool_calls(msg)
        if not valid_calls:
            if invalid_call_count:
                stripped_unmatched_calls += invalid_call_count
                assistant_msg = dict(msg)
                assistant_msg.pop("tool_calls", None)
                if _has_meaningful_content(assistant_msg):
                    if _should_normalize_empty_content(assistant_msg):
                        assistant_msg["content"] = ""
                    sanitized.append(assistant_msg)
                else:
                    dropped_tool_call_only_assistant += 1
            else:
                if _should_normalize_empty_content(msg):
                    msg = dict(msg)
                    msg["content"] = ""
                sanitized.append(msg)
            idx += 1
            continue

        unique_call_ids: list[str] = []
        seen_ids: set[str] = set()
        duplicate_call_count = 0
        for call in valid_calls:
            call_id = str(call.get("id") or "").strip()
            if call_id in seen_ids:
                duplicate_call_count += 1
                continue
            seen_ids.add(call_id)
            unique_call_ids.append(call_id)
        call_ids = set(unique_call_ids)

        matched_ids: set[str] = set()
        matched_tool_messages: list[dict[str, Any]] = []
        scan_idx = idx + 1
        while scan_idx < len(messages):
            next_msg = messages[scan_idx]
            if str(next_msg.get("role") or "").strip().lower() != "tool":
                break

            tool_call_id = str(next_msg.get("tool_call_id") or "").strip()
            if not tool_call_id:
                dropped_empty += 1
                scan_idx += 1
                continue

            if tool_call_id in call_ids:
                if tool_call_id in matched_ids:
                    dropped_orphan += 1
                else:
                    matched_ids.add(tool_call_id)
                    matched_tool_messages.append(next_msg)
            else:
                dropped_orphan += 1
            scan_idx += 1

        kept_calls: list[dict[str, Any]] = []
        kept_ids: set[str] = set()
        for call in valid_calls:
            call_id = str(call.get("id") or "").strip()
            if call_id in matched_ids and call_id not in kept_ids:
                kept_calls.append(call)
                kept_ids.add(call_id)

        unresolved_count = (
            invalid_call_count
            + duplicate_call_count
            + max(0, len(call_ids) - len(matched_ids))
        )
        if unresolved_count > 0:
            stripped_unmatched_calls += unresolved_count

        assistant_msg = msg
        if len(kept_calls) != len(valid_calls):
            assistant_msg = dict(msg)
            if kept_calls:
                assistant_msg["tool_calls"] = kept_calls
            else:
                assistant_msg.pop("tool_calls", None)

        if not assistant_msg.get("tool_calls"):
            if _has_meaningful_content(assistant_msg):
                if _should_normalize_empty_content(assistant_msg):
                    assistant_msg = dict(assistant_msg)
                    assistant_msg["content"] = ""
                sanitized.append(assistant_msg)
            else:
                dropped_tool_call_only_assistant += 1
        else:
            sanitized.append(assistant_msg)
            sanitized.extend(matched_tool_messages)

        idx = scan_idx

    return sanitized, ToolMessageSanitizationStats(
        dropped_empty_tool_messages=dropped_empty,
        dropped_orphan_tool_messages=dropped_orphan,
        stripped_unmatched_assistant_tool_calls=stripped_unmatched_calls,
        dropped_tool_call_only_assistant_messages=dropped_tool_call_only_assistant,
    )
