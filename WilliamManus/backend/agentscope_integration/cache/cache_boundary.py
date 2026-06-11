"""CacheBoundaryHook — protects KV cache prefix integrity.

Freezes system-prompt and tool-definition bytes on the first API call,
then detects drift on subsequent calls that would silently invalidate
the DeepSeek prefix cache.

LangChain calls this pattern "Middleware"; AgentScope calls it "Hook".
Same thing — an interceptor that wraps the model call.

Usage (in OpenRouterChatModel.__call__)::

    boundary = CacheBoundaryHook()
    boundary.freeze(system_messages, tools)
    # ... later ...
    ok, warnings = boundary.check(system_messages, tools)
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)


class CacheBoundaryHook:
    """Detects cache-breaking drift in system prompts and tool definitions."""

    def __init__(self) -> None:
        self._frozen_system_hash: str = ""
        self._frozen_tool_hash: str = ""
        self._frozen: bool = False
        self._drift_count: int = 0

    # ----------------------------------------------------------------
    # Canonical helpers
    # ----------------------------------------------------------------

    @staticmethod
    def canonical_json_dumps(obj: Any) -> str:
        """Serialize to deterministic JSON — sorted keys, compact."""
        return json.dumps(
            obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        )

    @staticmethod
    def _hash_bytes(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()[:16]

    @staticmethod
    def hash_system_messages(messages: List[Dict[str, Any]]) -> str:
        """Hash all system-role messages for drift detection."""
        parts: List[str] = []
        for msg in messages:
            if isinstance(msg, dict) and msg.get("role") == "system":
                content = msg.get("content", "")
                if isinstance(content, str):
                    parts.append(content)
                elif isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            parts.append(str(block.get("text", "")))
        if not parts:
            return ""
        combined = "\n".join(parts).encode("utf-8")
        return CacheBoundaryHook._hash_bytes(combined)

    @staticmethod
    def hash_tools(tools: Optional[List[Dict[str, Any]]]) -> str:
        """Hash canonicalized tool definitions."""
        if not tools:
            return ""
        # Stable sort by function name
        sorted_tools = sorted(
            tools,
            key=lambda t: (
                t.get("function", {}).get("name", "")
                if isinstance(t, dict)
                else ""
            ),
        )
        raw = CacheBoundaryHook.canonical_json_dumps(sorted_tools)
        return CacheBoundaryHook._hash_bytes(raw.encode("utf-8"))

    # ----------------------------------------------------------------
    # Freeze / check
    # ----------------------------------------------------------------

    def freeze(self, system_hash: str, tool_hash: str) -> None:
        """Record the baseline hashes after the first successful call."""
        self._frozen_system_hash = system_hash
        self._frozen_tool_hash = tool_hash
        self._frozen = True
        logger.debug(
            "[CacheBoundary] Frozen: system=%s, tools=%s",
            system_hash, tool_hash,
        )

    def check(
        self,
        system_hash: str,
        tool_hash: str,
    ) -> Tuple[bool, List[str]]:
        """Check for drift. Returns ``(ok, warnings)``."""
        warnings: List[str] = []
        if not self._frozen:
            return True, warnings

        if (
            system_hash
            and self._frozen_system_hash
            and system_hash != self._frozen_system_hash
        ):
            self._drift_count += 1
            warnings.append(
                f"KV_CACHE_SYSTEM_DRIFT: system prompt hash changed "
                f"(drift_count={self._drift_count}). "
                f"This WILL break the KV cache prefix."
            )

        if (
            tool_hash
            and self._frozen_tool_hash
            and tool_hash != self._frozen_tool_hash
        ):
            self._drift_count += 1
            warnings.append(
                f"KV_CACHE_TOOL_DRIFT: tool definitions hash changed "
                f"(drift_count={self._drift_count}). "
                f"This WILL break the KV cache prefix."
            )

        return len(warnings) == 0, warnings
