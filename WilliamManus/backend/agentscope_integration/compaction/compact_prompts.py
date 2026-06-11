"""Compaction prompts for the Auto Compact agent.

Combines the best of Claude Code's 9-section summary with OpenCode's
anchored incremental update pattern.  The compaction agent receives these
prompts and returns a structured Markdown summary.
"""

from __future__ import annotations

# ---------------------------------------------------------------------------
# Compaction agent system prompt (adapted from OpenCode compaction.txt)
# ---------------------------------------------------------------------------

COMPACT_SYSTEM_PROMPT = """\
You are an anchored context summarization assistant for coding sessions.

CRITICAL: Respond with TEXT ONLY. Do NOT call any tools.
- You already have all the context you need in the conversation above.
- Tool calls will be REJECTED and you will fail the task.
- Your entire response must be plain text: an <analysis> block followed \
by a <summary> block.

Summarize only the conversation history you are given. The newest turns may be
kept verbatim outside your summary, so focus on the older context that still
matters for continuing the work.

If the prompt includes a <previous-summary> block, treat it as the current
anchored summary. Update it with the new history by preserving still-true
details, removing stale details, and merging in new facts.

Always follow the exact output structure requested by the user prompt. Keep
every section, preserve exact file paths and identifiers when known, and prefer
terse bullets over paragraphs.

Do not answer the conversation itself. Do not mention that you are summarizing,
compacting, or merging context. Respond in the same language as the
conversation."""

# ---------------------------------------------------------------------------
# Summary template (8 sections — from OpenCode, battle-tested)
# ---------------------------------------------------------------------------

SUMMARY_TEMPLATE = """\
Output exactly this Markdown structure and keep the section order unchanged:
---
## Goal
- [single-sentence task summary]

## Constraints & Preferences
- [user constraints, preferences, specs, or "(none)"]

## Progress
### Done
- [completed work or "(none)"]

### In Progress
- [current work or "(none)"]

### Blocked
- [blockers or "(none)"]

## Key Decisions
- [decision and why, or "(none)"]

## Next Steps
- [ordered next actions or "(none)"]

## Critical Context
- [important technical facts, errors, open questions, or "(none)"]

## Relevant Files
- [file or directory path: why it matters, or "(none)"]
---

Rules:
- Keep every section, even when empty.
- Use terse bullets, not prose paragraphs.
- Preserve exact file paths, commands, error strings, and identifiers.
- Do not mention the summary process or that context was compacted."""

# ---------------------------------------------------------------------------
# Build the full user-facing prompt for the compaction agent
# ---------------------------------------------------------------------------


def build_compaction_prompt(
    head_messages: list[str],
    previous_summary: str | None = None,
    plugin_context: list[str] | None = None,
) -> str:
    """Assemble the compaction prompt — first-run or incremental.

    Parameters
    ----------
    head_messages:
        Text-rendered messages that need to be summarized (the "head").
    previous_summary:
        The most recent summary from a prior compaction, if any.
    plugin_context:
        Optional additional context lines injected by hooks.

    Returns
    -------
    str
        The full prompt text to send to the compaction agent.
    """
    if previous_summary:
        anchor = (
            "Update the anchored summary below using the conversation "
            "history above.\n"
            "Preserve still-true details, remove stale details, and merge "
            "in the new facts.\n"
            "<previous-summary>\n"
            f"{previous_summary}\n"
            "</previous-summary>"
        )
    else:
        anchor = (
            "Create a new anchored summary from the conversation "
            "history above."
        )

    ctx = plugin_context or []
    parts = [anchor, SUMMARY_TEMPLATE, *ctx, *head_messages]
    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Auto-continue message (injected after compaction completes)
# ---------------------------------------------------------------------------

AUTO_CONTINUE_MESSAGE = (
    "Continue if you have next steps, or stop and ask for clarification "
    "if you are unsure how to proceed."
)
