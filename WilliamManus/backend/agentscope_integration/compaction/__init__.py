"""Compaction sub-package for WilliamManus context engineering."""

from .auto_compact import AutoCompactManager, HeadTailSplit
from .compact_agent import CompactAgent
from .compact_prompts import (
    COMPACT_SYSTEM_PROMPT,
    SUMMARY_TEMPLATE,
    build_compaction_prompt,
)

__all__ = [
    "AutoCompactManager",
    "CompactAgent",
    "HeadTailSplit",
    "COMPACT_SYSTEM_PROMPT",
    "SUMMARY_TEMPLATE",
    "build_compaction_prompt",
]
