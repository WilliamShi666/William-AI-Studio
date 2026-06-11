"""Micro Compact — lightweight pre-LLM tool output pruning.

Runs as a ``pre_reasoning`` hook on ReActAgent, before every LLM API call.
Walks message history backwards, protects the most recent PRUNE_PROTECT chars
of tool outputs, and replaces older ones with a placeholder string.

Skill results are NEVER pruned.  Worker agents exclude micro-compact entirely
via a simple enabled flag on the memory instance.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants (env-overridable via ContextBuilderSettings)
# ---------------------------------------------------------------------------

PRUNE_MINIMUM = 1_000        # minimum total chars saved to bother executing
PRUNE_PROTECT = 30_000       # cumulative chars of recent tool output to protect
PRUNE_PLACEHOLDER = "[Old tool result content cleared]"

# Tool *kinds* whose outputs are eligible for pruning.
COMPACTABLE_TOOL_KINDS: Set[str] = {
    "code",
    "web_search",
    "media",
    "computer_use",
}

# Individual tool *names* that are always protected, even if their kind is
# compactable.  Skills, task-list, delegation, LTM and cross-subagent notes
# must remain visible.
PROTECTED_TOOL_NAMES: Set[str] = {
    # SandboxSkillTool  (skill results are critical reference material)
    "get_available_skills",
    "load_skill",
    "load_reference",
    "list_skill_scripts",
    "run_skill_script",
    # TaskListTool
    "create_tasks",
    "view_tasks",
    "update_tasks",
    "delete_tasks",
    # Orchestrator delegation
    "delegate_to_worker",
    # Shadow Clone cross-subagent
    "send_peer_note",
    "read_full_result",
    # LTM
    "retrieve_from_memory",
    "record_to_memory",
    "delete_from_memory",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _tool_kind_from_name(tool_name: str) -> Optional[str]:
    """Map a tool function name to its backend kind string.

    The mapping matches the ``_tool_factories`` layout in
    ``ToolkitAdapter.__init__`` (toolkit_adapter.py:134-137).
    """
    code = {
        "execute_command", "read_file", "write_file", "edit_file",
        "list_dir", "make_dir", "upload_file", "download_file",
        "expose_port",
    }
    web = {"web_search", "scrape_webpage", "download_images"}
    media = {"understand_media"}
    computer_use = {
        "screenshot", "click", "typing", "scroll", "press",
        "move_to", "drag_to",
    }

    if tool_name in code:
        return "code"
    if tool_name in web:
        return "web_search"
    if tool_name in media:
        return "media"
    if tool_name in computer_use:
        return "computer_use"
    return None


def _is_compactable(tool_name: str) -> bool:
    """Return *True* if a tool result is eligible for pruning."""
    if tool_name in PROTECTED_TOOL_NAMES:
        return False
    kind = _tool_kind_from_name(tool_name)
    return kind is not None and kind in COMPACTABLE_TOOL_KINDS


# ---------------------------------------------------------------------------
# Prune logic
# ---------------------------------------------------------------------------

class MicroCompactResult:
    """Summary of a single prune pass."""

    __slots__ = ("pruned_count", "saved_chars")

    def __init__(self, pruned_count: int = 0, saved_chars: int = 0) -> None:
        self.pruned_count = pruned_count
        self.saved_chars = saved_chars

    def __repr__(self) -> str:
        return (
            f"MicroCompactResult(pruned={self.pruned_count}, "
            f"saved_chars={self.saved_chars})"
        )


def prune_tool_outputs(
    rows: List[Dict[str, Any]],
    *,
    protect: int = PRUNE_PROTECT,
    minimum: int = PRUNE_MINIMUM,
) -> List[Dict[str, Any]]:
    """Walk *rows* newest→oldest and mark stale tool outputs for compaction.

    Parameters
    ----------
    rows:
        Message rows from the DB, in **chronological** order (oldest first).
        Each row is a dict with keys ``role``, ``content``, ``metadata``,
        ``tool_name``, and optionally ``id``.
    protect:
        Cumulative chars of tool-output content to protect (default 3 000).
    minimum:
        Minimum total chars that would be freed before actually marking
        anything (default 100).

    Returns
    -------
    list of dict
        The same rows list (mutated in place).  Callers can also inspect
        ``sum(len(r["content"]) for r in compacted_rows)`` for logging.
    """
    if not rows:
        return rows

    # Walk backwards so we can easily stop when the protection budget is
    # exhausted.
    protected_chars = 0
    compacted: List[Dict[str, Any]] = []
    savable_chars = 0

    for row in reversed(rows):
        # DB rows use "type" for message kind ("tool"/"user"/"assistant");
        # "role" may differ. Check "type" first.
        row_type = str(row.get("type", "") or row.get("role", "")).lower()
        if row_type not in ("tool",):
            continue

        # tool_name lives in content.tool_name (DB nested JSON)
        # Supabase may return JSONB as a JSON string — parse if needed.
        content = row.get("content", {})
        if isinstance(content, str) and content.startswith("{"):
            try:
                import json as _json
                content = _json.loads(content)
            except Exception:
                pass  # keep as string if parse fails
        if isinstance(content, dict):
            tool_name = str(content.get("tool_name", "") or "")
        else:
            tool_name = str(row.get("tool_name", "") or "")
        if not tool_name or not _is_compactable(tool_name):
            continue

        # Already compacted — stop here (this is a compaction boundary).
        meta = row.get("metadata")
        if isinstance(meta, dict) and meta.get("time_compacted"):
            break

        # content_val may already be parsed from above
        content_val = content if isinstance(content, dict) else row.get("content", "")
        if isinstance(content_val, str) and content_val.startswith("{"):
            try:
                import json as _json2
                content_val = _json2.loads(content_val)
            except Exception:
                pass
        if isinstance(content_val, dict):
            # DB row format: {"tool_name": "...", "result": "..."}
            content_str = str(content_val.get("result", "") or "")
        elif not isinstance(content_val, str):
            content_str = str(content_val)
        else:
            content_str = content_val
        content_len = len(content_str)

        if protected_chars < protect:
            protected_chars += content_len
            continue

        # Beyond protection zone — mark for compaction.
        # Supabase returns metadata as a JSON string; parse if needed.
        meta = row.get("metadata", {})
        if isinstance(meta, str) and meta.startswith("{"):
            try:
                import json as _json3
                meta = _json3.loads(meta)
            except Exception:
                meta = {}
        if not isinstance(meta, dict):
            meta = {}
        from datetime import datetime, timezone

        meta["time_compacted"] = (
            datetime.now(timezone.utc).isoformat()
        )
        row["metadata"] = meta
        compacted.append(row)
        savable_chars += content_len

    if savable_chars < minimum:
        # Roll back — not enough to justify
        for row in compacted:
            if isinstance(row.get("metadata"), dict):
                row["metadata"].pop("time_compacted", None)
        return rows

    if compacted:
        logger.info(
            "[MicroCompact] Pruned %d tool outputs, saving ~%d chars",
            len(compacted),
            savable_chars,
        )

    return rows


# ---------------------------------------------------------------------------
# Reasoning content pruning
# ---------------------------------------------------------------------------


def prune_reasoning_content(
    rows: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Strip *reasoning_content* from assistant messages that have no tool_calls.

    Per DeepSeek docs, reasoning_content from non-tool-call turns is ignored
    by the API.  Keeping it only wastes context (50-70% of prompt tokens in
    long conversations).  Tool-call assistant messages keep their
    reasoning_content (required by the API).

    This complements the MoonshotChatFormatter fix which handles new
    messages; this function cleans already-stored history.
    """
    for row in rows:
        role = str(row.get("role", "")).lower()
        if role != "assistant":
            continue
        if row.get("tool_calls"):
            continue
        row.pop("reasoning_content", None)
    return rows


# ---------------------------------------------------------------------------
# Hook factory
# ---------------------------------------------------------------------------

def make_micro_compact_hook(
    enabled: bool = True,
) -> Any:
    """Return an async ``pre_reasoning`` hook for ReActAgent.

    The hook calls ``agent.memory.prune_old_tool_outputs()`` before every
    reasoning step.  When *enabled* is ``False`` the hook is a no-op so
    callers (e.g. worker) can disable without branching code paths.
    """

    async def _hook(agent: Any, kwargs: dict) -> Optional[dict]:
        if not enabled:
            return None
        memory = getattr(agent, "memory", None)
        if memory is None:
            logger.warning("[MicroCompact] agent.memory is None — skipping")
            return None
        if not hasattr(memory, "prune_old_tool_outputs"):
            logger.warning(
                "[MicroCompact] memory %r has no prune_old_tool_outputs",
                type(memory).__name__,
            )
            return None
        try:
            pruned = await memory.prune_old_tool_outputs()
            if pruned > 0:
                logger.info("[MicroCompact] pruned %d outputs", pruned)
        except Exception:
            logger.warning(
                "[MicroCompact] prune failed in hook: %s",
                type(memory).__name__,
                exc_info=True,
            )
        return None  # never modify kwargs

    return _hook
