"""Auto Compact — LLM-based context summarisation triggered at 90K tokens.

Performs head/tail split on message history, delegates to a dedicated
compact agent (no tools, cheap model), stores the structured summary,
and injects an auto-continue message so the orchestrator resumes work
seamlessly.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

AUTOCOMPACT_TRIGGER_TOKENS = 90_000
"""Default token threshold that triggers auto-compact."""

AUTOCOMPACT_BUFFER = 20_000
"""Tokens reserved for model output + post-compact growth."""

DEFAULT_TAIL_TURNS = 2
"""Number of recent user turns kept verbatim (tail)."""

MIN_PRESERVE_RECENT_TOKENS = 2_000
"""Floor for the tail preservation budget."""

MAX_PRESERVE_RECENT_TOKENS = 8_000
"""Ceiling for the tail preservation budget."""

MAX_CONSECUTIVE_FAILURES = 3
"""Circuit breaker: stop trying after this many consecutive failures."""

TOOL_OUTPUT_MAX_CHARS = 2_000
"""Individual tool output truncation limit when building head text."""


# ---------------------------------------------------------------------------
# Head / Tail split
# ---------------------------------------------------------------------------


@dataclass
class HeadTailSplit:
    """Result of splitting message history for compaction."""

    head: List[Dict[str, Any]]
    """Messages to be summarised by the compact agent."""

    tail: List[Dict[str, Any]]
    """Messages to be kept verbatim (most recent turns)."""

    tail_start_id: Optional[str] = None
    """Message id where the tail begins (for later context reconstruction)."""


def _estimate_tokens(text: str) -> int:
    """Conservative token estimate: chars / 4 for English/code."""
    return max(1, len(text or "") // 4)


def _row_text(row: Dict[str, Any]) -> str:
    """Extract display text from a message row for token estimation."""
    role = str(row.get("role", "") or row.get("type", ""))
    content = row.get("content", "")
    if isinstance(content, str):
        body = content
    elif isinstance(content, dict):
        body = content.get("result", "") or content.get("content", "") or ""
    else:
        body = str(content)
    tool_name = str(row.get("tool_name", "") or "")
    prefix = f"[{role}]" if not tool_name else f"[tool:{tool_name}]"
    return f"{prefix} {body}"


def select_head_tail(
    messages: List[Dict[str, Any]],
    *,
    tail_turns: int = DEFAULT_TAIL_TURNS,
    usable_context: int = AUTOCOMPACT_TRIGGER_TOKENS,
) -> HeadTailSplit:
    """Split *messages* into head (to summarise) and tail (keep verbatim).

    Algorithm (from OpenCode):
    1. Group messages into user-turns (each turn = user msg → next user msg).
    2. Calculate tail budget = clamp(usable * 0.25, 2000, 8000) tokens.
    3. Greedily include turns from newest to oldest until budget exhausted.
    4. If a single turn exceeds budget, try to split within the turn.

    Returns
    -------
    HeadTailSplit
    """
    if not messages:
        return HeadTailSplit(head=[], tail=[], tail_start_id=None)

    budget = max(
        MIN_PRESERVE_RECENT_TOKENS,
        min(MAX_PRESERVE_RECENT_TOKENS, int(usable_context * 0.25)),
    )

    # Group into user turns
    turns: List[List[Dict[str, Any]]] = []
    current_turn: List[Dict[str, Any]] = []
    for row in messages:
        role = str(row.get("role", "") or row.get("type", "")).lower()
        current_turn.append(row)
        if role == "user" and len(current_turn) > 1:
            # End previous turn, start new one
            turns.append(current_turn[:-1])
            current_turn = [current_turn[-1]]
    if current_turn:
        turns.append(current_turn)

    if not turns:
        return HeadTailSplit(head=list(messages), tail=[], tail_start_id=None)

    recent_turns = turns[-tail_turns:] if len(turns) >= tail_turns else turns[:]
    tail_messages: List[Dict[str, Any]] = []
    remaining_budget = budget

    for turn in reversed(recent_turns):
        turn_text = " ".join(_row_text(r) for r in turn)
        turn_tokens = _estimate_tokens(turn_text)

        if turn_tokens <= remaining_budget:
            tail_messages = turn + tail_messages
            remaining_budget -= turn_tokens
        elif remaining_budget > 0 and len(turn) > 1:
            # Try splitting within the turn
            for split_idx in range(1, len(turn)):
                prefix_text = " ".join(_row_text(r) for r in turn[:split_idx])
                if _estimate_tokens(prefix_text) <= remaining_budget:
                    tail_messages = turn[split_idx:] + tail_messages
                    remaining_budget = 0
                    break
            else:
                break
        else:
            break

    tail_start_id = None
    if tail_messages:
        tail_start_id = tail_messages[0].get("id")

    head_count = len(messages) - len(tail_messages)
    head = list(messages[:head_count]) if head_count > 0 else []

    logger.info(
        "[AutoCompact] Head/Tail split: head=%d messages, tail=%d messages, "
        "budget=%d tokens",
        len(head), len(tail_messages), budget,
    )

    return HeadTailSplit(head=head, tail=tail_messages, tail_start_id=tail_start_id)


# ---------------------------------------------------------------------------
# Head-message rendering (for the compact agent prompt)
# ---------------------------------------------------------------------------


def _truncate_tool_output(text: str, max_chars: int = TOOL_OUTPUT_MAX_CHARS) -> str:
    """Truncate a tool output to *max_chars* with a clear marker."""
    if not text:
        return ""
    if len(text) <= max_chars:
        return text
    omitted = len(text) - max_chars
    return (
        text[:max_chars]
        + f"\n[Tool output truncated for compaction: omitted {omitted} chars]"
    )


def render_head_messages(
    head: List[Dict[str, Any]],
    *,
    max_tool_chars: int = TOOL_OUTPUT_MAX_CHARS,
    strip_media: bool = True,
) -> List[str]:
    """Convert head message rows into compact text lines for the prompt.

    Tool outputs are truncated, media attachments are replaced with stubs.
    """
    lines: List[str] = []
    for row in head:
        role = str(row.get("role", "") or row.get("type", "")).lower()
        content = row.get("content", "")

        if isinstance(content, dict):
            if role == "tool" or row.get("type") == "tool":
                tool_name = content.get("tool_name", "")
                tool_result = content.get("result", "")
                truncated = _truncate_tool_output(
                    str(tool_result), max_tool_chars,
                )
                lines.append(f"[tool:{tool_name}] {truncated}")
            else:
                text = content.get("content", "") or content.get("text", "") or ""
                lines.append(f"[{role}] {text}")
        elif isinstance(content, str):
            lines.append(f"[{role}] {content}")
        else:
            lines.append(f"[{role}] {str(content)}")

    # Strip media placeholders
    if strip_media:
        lines = [
            l for l in lines
            if not any(
                tag in l.lower()
                for tag in ("[image]", "[audio]", "[video]", "[media]", "base64,")
            )
        ]

    return lines


# ---------------------------------------------------------------------------
# Auto Compact manager
# ---------------------------------------------------------------------------


@dataclass
class AutoCompactConfig:
    """Runtime configuration for auto-compact."""

    enabled: bool = True
    trigger_tokens: int = AUTOCOMPACT_TRIGGER_TOKENS
    tail_turns: int = DEFAULT_TAIL_TURNS
    compact_model_key: str = "deepseek-v4-flash"
    shadow_mode: bool = False
    """When *True*, only log — do not actually compact."""


@dataclass
class AutoCompactManager:
    """Orchestrates auto-compact for an agent.

    Intended to be instantiated by ``AgentScopeRunner`` and called after
    each reasoning step.
    """

    config: AutoCompactConfig = field(default_factory=AutoCompactConfig)
    _consecutive_failures: int = 0
    _cumulative_new_tokens: int = 0
    _previous_summary: Optional[str] = None
    _compaction_count: int = 0

    # ------------------------------------------------------------------
    # Circuit breaker
    # ------------------------------------------------------------------

    @property
    def circuit_open(self) -> bool:
        return self._consecutive_failures >= MAX_CONSECUTIVE_FAILURES

    def _record_success(self) -> None:
        self._consecutive_failures = 0
        self._cumulative_new_tokens = 0
        self._compaction_count += 1

    def _record_failure(self) -> None:
        self._consecutive_failures += 1
        logger.warning(
            "[AutoCompact] Consecutive failure %d/%d",
            self._consecutive_failures,
            MAX_CONSECUTIVE_FAILURES,
        )

    # ------------------------------------------------------------------
    # Trigger check
    # ------------------------------------------------------------------

    def should_compact(self, new_tokens: int) -> bool:
        """Return *True* if auto-compact should be triggered.

        Tracks cumulative **new** tokens (prompt_tokens minus KV-cached
        tokens) rather than total prompt_tokens.  With DeepSeek KV cache
        hitting 97%+, total prompt_tokens ≈ 30k but new content is only
        500–1500 tokens per round.  Accumulating total prompt_tokens would
        falsely trigger compaction every 3 rounds; accumulating new tokens
        triggers only when real context growth crosses the threshold.
        """
        if not self.config.enabled:
            return False
        if self.circuit_open:
            logger.debug("[AutoCompact] Circuit breaker open — skipping")
            return False
        self._cumulative_new_tokens += new_tokens
        trigger = self._cumulative_new_tokens >= self.config.trigger_tokens
        if trigger:
            logger.info(
                "[AutoCompact] Threshold reached: cumulative_new=%d trigger=%d",
                self._cumulative_new_tokens,
                self.config.trigger_tokens,
            )
        return trigger

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    async def compact(
        self,
        *,
        messages: List[Dict[str, Any]],
        current_tokens: int,
        memory: Any,
        model_factory: Any,
        formatter_factory: Any,
    ) -> Optional[str]:
        """Run auto-compact.  Caller must have already checked *should_compact*.

        Returns the summary text if compaction was performed, or *None*.
        """
        # NOTE: should_compact() is NOT called here — the hook in runner.py
        # already checked it.  Calling it again would double-count tokens
        # (once with new_tokens, once with prompt_tokens).

        if self.config.shadow_mode:
            logger.info(
                "[AutoCompact] SHADOW MODE: would compact at %d tokens, "
                "%d messages",
                current_tokens,
                len(messages),
            )
            return None

        logger.info(
            "[AutoCompact] Triggered at %d tokens (%d messages)",
            current_tokens,
            len(messages),
        )

        try:
            from .compact_agent import CompactAgent
            from .compact_prompts import (
                COMPACT_SYSTEM_PROMPT,
                build_compaction_prompt,
            )

            # 1. Head/tail split
            split = select_head_tail(
                messages,
                tail_turns=self.config.tail_turns,
                usable_context=current_tokens,
            )

            if not split.head:
                logger.warning("[AutoCompact] No head messages to compact")
                return None

            # 2. Render head text
            head_lines = render_head_messages(split.head)
            if not head_lines:
                return None

            # 3. Build prompt
            user_prompt = build_compaction_prompt(
                head_messages=head_lines,
                previous_summary=self._previous_summary,
            )

            # 4. Run compact agent
            agent = CompactAgent(model_key=self.config.compact_model_key)
            result = await agent.compact(
                system_prompt=COMPACT_SYSTEM_PROMPT,
                user_prompt=user_prompt,
                model_factory=model_factory,
                formatter_factory=formatter_factory,
                previous_summary=self._previous_summary,
            )

            # 5. Store summary
            self._previous_summary = result.summary_text

            # Store on memory if supported
            if hasattr(memory, "update_compressed_summary"):
                await memory.update_compressed_summary(result.summary_text)

            # Mark compacted messages
            if hasattr(memory, "update_messages_mark") and split.tail_start_id:
                try:
                    await memory.update_messages_mark(
                        new_mark="compacted",
                        msg_ids=[split.tail_start_id],
                    )
                except Exception:
                    logger.debug(
                        "[AutoCompact] Message mark update skipped",
                        exc_info=True,
                    )

            self._record_success()

            logger.info(
                "[AutoCompact] Complete: summary=%d chars, "
                "tail=%d msgs, head=%d msgs, compaction #%d",
                len(result.summary_text),
                len(split.tail),
                len(split.head),
                self._compaction_count,
            )

            return result.summary_text

        except Exception:
            logger.error("[AutoCompact] Failed", exc_info=True)
            self._record_failure()
            return None
