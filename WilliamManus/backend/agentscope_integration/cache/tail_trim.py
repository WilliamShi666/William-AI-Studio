"""TailTrim — cache-friendly message history compaction.

Preserves the prefix (system prompt + seed rows + first user message)
for KV cache while removing middle messages.  Only the tail needs
fresh encoding.

Strategy:
1. Identify the stable prefix — all leading system messages PLUS the
   first user message (which carries the original task objective).
   These bytes anchor DeepSeek's prefix cache.
2. Preserve the tail (most recent K user turns).
3. Remove messages from the MIDDLE only — old tool calls and
   intermediate assistant replies.
4. Prefix bytes stay identical → cache hit preserved.
5. First user message in prefix → agent never loses sight of the
   original task, preventing task drift.

This is the primary cache-preservation mechanism for WilliamManus.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


@dataclass
class TailTrimConfig:
    """Configuration for TailTrim behaviour."""

    keep_tail_turns: int = 6
    """Number of complete user/assistant turns to preserve at the tail."""

    min_prefix_messages: int = 3
    """Minimum prefix messages to preserve (system prompts, seeds, first user)."""

    target_max_messages: int = 80
    """Soft limit — target after trim."""

    trigger_message_count: int = 120
    """Total messages before considering trim."""


@dataclass
class TailTrimResult:
    """Result of a TailTrim operation."""

    trimmed_messages: List[Dict[str, Any]]
    """The compacted message list."""

    removed_count: int

    removed_range: Tuple[int, int]
    """(start_index, end_index) of removed messages."""

    prefix_preserved: bool

    summary: str
    """Human-readable summary of what was removed."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_system_or_seed(msg: Dict[str, Any]) -> bool:
    return str(msg.get("role", "")).lower() == "system"


def _is_user_turn_start(msg: Dict[str, Any]) -> bool:
    return str(msg.get("role", "")).lower() == "user"


def _find_turn_boundaries(messages: List[Dict[str, Any]]) -> List[int]:
    """Return indices of user messages (turn starts)."""
    return [
        i for i, msg in enumerate(messages)
        if _is_user_turn_start(msg)
    ]


# ---------------------------------------------------------------------------
# Main algorithm
# ---------------------------------------------------------------------------


def tail_trim(
    messages: List[Dict[str, Any]],
    config: Optional[TailTrimConfig] = None,
) -> TailTrimResult:
    """Remove middle messages while preserving prefix and tail.

    1. Identify prefix boundary — all system/seed messages at the start
       PLUS the first user message (carries the original task objective,
       prevents task drift).
    2. Count turns from the end — preserve the last ``keep_tail_turns``.
    3. Remove everything in between.
    4. The prefix is untouched → cache hit preserved.
    5. First user message stays in prefix → agent always sees the
       original request.

    Returns a ``TailTrimResult`` describing what was done.
    """
    cfg = config or TailTrimConfig()

    total = len(messages)
    if total <= cfg.trigger_message_count and total <= cfg.target_max_messages:
        return TailTrimResult(
            trimmed_messages=list(messages),
            removed_count=0,
            removed_range=(0, 0),
            prefix_preserved=True,
            summary="No trim needed — under threshold.",
        )

    # 1. Find prefix boundary — system messages
    prefix_end = 0
    for i, msg in enumerate(messages):
        if _is_system_or_seed(msg):
            prefix_end = i + 1
        else:
            break

    # 1a. Include first user message as seed (carries original task objective)
    if prefix_end < total and messages[prefix_end].get("role") == "user":
        prefix_end += 1

    prefix_end = max(prefix_end, cfg.min_prefix_messages)

    # 2. Find tail start (keep last N turns)
    turn_indices = _find_turn_boundaries(messages)
    tail_turns = min(cfg.keep_tail_turns, len(turn_indices))
    if tail_turns == 0:
        tail_start = total
    else:
        tail_start = turn_indices[-tail_turns]

    # Ensure tail doesn't overlap prefix
    tail_start = max(tail_start, prefix_end)

    # 3. Build trimmed message list
    prefix = list(messages[:prefix_end])
    tail = list(messages[tail_start:])
    removed = list(messages[prefix_end:tail_start])

    trimmed = prefix + tail

    removed_summary = (
        f"TailTrim: removed {len(removed)} messages "
        f"(indices {prefix_end}–{tail_start - 1}), "
        f"kept {len(prefix)} prefix + {len(tail)} tail = {len(trimmed)} total"
    )

    logger.info("[TailTrim] %s", removed_summary)

    return TailTrimResult(
        trimmed_messages=trimmed,
        removed_count=len(removed),
        removed_range=(prefix_end, tail_start),
        prefix_preserved=True,
        summary=removed_summary,
    )
