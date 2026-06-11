"""Write-gating and sanitization policy for long-term memory."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Sequence, Tuple


SENSITIVE_PATTERNS = (
    re.compile(r"(?i)\b(api[_-]?key|secret|password|passwd|token)\b"),
    re.compile(r"(?i)\bbearer\s+[a-z0-9\-_\.]{8,}"),
    re.compile(r"(?i)\bsk-[a-z0-9\-_]{12,}\b"),
)

MIN_TASK_CHARS = 24
MIN_TOOL_OUTPUT_CHARS = 8
MAX_ITEM_CHARS = 4000


@dataclass
class MemoryContentBuckets:
    """Validated task/tool memory payloads plus filtering diagnostics."""

    task_items: List[str] = field(default_factory=list)
    tool_items: List[str] = field(default_factory=list)
    dropped_count: int = 0
    drop_reasons: Dict[str, int] = field(default_factory=dict)

    def drop(self, reason: str) -> None:
        self.dropped_count += 1
        self.drop_reasons[reason] = self.drop_reasons.get(reason, 0) + 1


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def contains_sensitive_payload(value: str) -> bool:
    text = str(value or "")
    return any(pattern.search(text) for pattern in SENSITIVE_PATTERNS)


def _fingerprint(value: str) -> str:
    return hashlib.sha1(_normalize_text(value).lower().encode("utf-8")).hexdigest()  # nosec B324


def _safe_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _safe_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _parse_tool_payload(raw: str) -> Tuple[bool, str]:
    """Parse and normalize a tool-execution JSON string."""

    try:
        payload = json.loads(raw)
    except Exception:
        return False, raw

    if not isinstance(payload, dict):
        return False, raw

    tool_name = str(
        payload.get("tool_name")
        or payload.get("tool")
        or payload.get("name")
        or "",
    ).strip()
    if not tool_name:
        return False, raw

    output = str(payload.get("output", "") or "").strip()
    if len(output) < MIN_TOOL_OUTPUT_CHARS:
        return False, raw

    normalized = {
        "create_time": str(
            payload.get("create_time")
            or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
        ),
        "tool_name": tool_name,
        "input": payload.get("input") if isinstance(payload.get("input"), dict) else {"raw": str(payload.get("input", ""))},
        "output": output[:MAX_ITEM_CHARS],
        "token_cost": _safe_int(payload.get("token_cost"), 0),
        "success": bool(payload.get("success", True)),
        "time_cost": _safe_float(payload.get("time_cost"), 0.0),
    }
    return True, json.dumps(normalized, ensure_ascii=False)


def bucketize_memory_content(
    content: Sequence[str],
    *,
    write_gate_enabled: bool = True,
) -> MemoryContentBuckets:
    """Split memory writes into validated task/tool payloads."""

    buckets = MemoryContentBuckets()
    seen = set()

    for raw_item in content or []:
        item = str(raw_item or "").strip()
        if not item:
            buckets.drop("empty")
            continue

        if write_gate_enabled and contains_sensitive_payload(item):
            buckets.drop("sensitive")
            continue

        is_tool_payload, normalized_item = _parse_tool_payload(item)
        normalized = normalized_item.strip()
        if not normalized:
            buckets.drop("empty_normalized")
            continue

        if write_gate_enabled and not is_tool_payload and len(normalized) < MIN_TASK_CHARS:
            buckets.drop("too_short")
            continue

        if len(normalized) > MAX_ITEM_CHARS:
            normalized = normalized[:MAX_ITEM_CHARS]

        fp = _fingerprint(normalized)
        if fp in seen:
            buckets.drop("duplicate")
            continue
        seen.add(fp)

        if is_tool_payload:
            buckets.tool_items.append(normalized)
        else:
            buckets.task_items.append(normalized)

    return buckets


def sanitize_keywords(
    keywords: Sequence[str],
    *,
    write_gate_enabled: bool = True,
) -> List[str]:
    """Normalize retrieval keywords and drop unsafe tokens."""

    sanitized: List[str] = []
    for keyword in keywords or []:
        value = _normalize_text(str(keyword or ""))
        if not value:
            continue
        if write_gate_enabled and contains_sensitive_payload(value):
            continue
        sanitized.append(value[:128])
    return sanitized

