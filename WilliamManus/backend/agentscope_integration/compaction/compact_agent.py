"""CompactAgent — a restricted agent for context compaction.

No tools, hidden from the user, uses a dedicated small/fast model
(``deepseek-v4-flash`` with ``reasoning_effort=high`` by default).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Structured output
# ---------------------------------------------------------------------------


@dataclass
class CompactResult:
    """Result returned by the compaction agent."""

    summary_text: str
    """The structured Markdown summary (the <summary> block content)."""

    analysis_text: str = ""
    """The scratch-pad analysis (the <analysis> block content), stripped
    before injection into context."""

    previous_summary: Optional[str] = None
    """The previous summary that was used as anchor, if any."""

    token_usage: Optional[Dict[str, int]] = None
    """Token usage from the compaction API call."""


# ---------------------------------------------------------------------------
# CompactAgent
# ---------------------------------------------------------------------------


class CompactAgent:
    """A minimal agent that summarises conversation history.

    Parameters
    ----------
    model_key:
        Model key for the compaction model.  Default ``deepseek-v4-flash``
        (cheapest model with sufficient reasoning capability).
    """

    def __init__(
        self,
        model_key: str = "deepseek-v4-flash",
    ) -> None:
        self.model_key = model_key

    async def compact(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        model_factory: Any,
        formatter_factory: Any,
        previous_summary: Optional[str] = None,
    ) -> CompactResult:
        """Generate a structured summary.

        Parameters
        ----------
        system_prompt:
            The compaction system prompt (see ``compact_prompts.py``).
        user_prompt:
            The assembled user prompt containing head messages and the
            summary template.
        model_factory:
            Callable ``(model_key, stream, reasoning_effort) -> ChatModel``.
        formatter_factory:
            Callable ``(model_key) -> Formatter``.
        previous_summary:
            The previous summary for incremental compaction, if any.

        Returns
        -------
        CompactResult
        """
        import re

        from agentscope.message import Msg

        # Build model with reasoning for better summary quality.
        # ModelFactory.create() returns (model, formatter) tuple — unpack if needed.
        try:
            created = model_factory(
                self.model_key,
                stream=False,
                reasoning_effort="high",
            )
            if isinstance(created, tuple):
                model, _formatter = created
            else:
                model = created
        except TypeError:
            created = model_factory(self.model_key, stream=False)
            if isinstance(created, tuple):
                model, _formatter = created
            else:
                model = created

        try:
            formatter = formatter_factory(self.model_key)
        except Exception:
            formatter = None

        # Format the prompt
        if formatter is not None:
            prompt = await formatter.format(
                msgs=[
                    Msg("system", system_prompt, "system"),
                    Msg("user", user_prompt, "user"),
                ],
            )
        else:
            prompt = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ]

        # Call the model (no tools)
        response = await model(prompt)

        # Extract text content
        full_text = ""
        if hasattr(response, "content"):
            for block in response.content:
                if hasattr(block, "text"):
                    full_text += block.text
                elif isinstance(block, dict) and block.get("type") == "text":
                    full_text += block["text"]
        elif isinstance(response, str):
            full_text = response

        # Parse <analysis> and <summary> blocks
        analysis_text = ""
        summary_text = full_text

        analysis_match = re.search(
            r"<analysis>(.*?)</analysis>", full_text, re.DOTALL,
        )
        if analysis_match:
            analysis_text = analysis_match.group(1).strip()

        summary_match = re.search(
            r"<summary>(.*?)</summary>", full_text, re.DOTALL,
        )
        if summary_match:
            summary_text = summary_match.group(1).strip()
        else:
            # No <summary> tag — use everything after <analysis>
            if analysis_match:
                summary_text = full_text[analysis_match.end():].strip()

        # Extract token usage
        token_usage = None
        if hasattr(response, "usage") and response.usage:
            usage = response.usage
            token_usage = {
                "input_tokens": getattr(usage, "input_tokens", 0) or 0,
                "output_tokens": getattr(usage, "output_tokens", 0) or 0,
            }

        logger.info(
            "[CompactAgent] Summary generated: %d chars summary, "
            "%d chars analysis, model=%s",
            len(summary_text),
            len(analysis_text),
            self.model_key,
        )

        return CompactResult(
            summary_text=summary_text,
            analysis_text=analysis_text,
            previous_summary=previous_summary,
            token_usage=token_usage,
        )
