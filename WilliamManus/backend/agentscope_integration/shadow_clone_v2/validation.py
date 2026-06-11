"""Validation helpers for Shadow Clone V2 agent-facing tools and keys."""

from __future__ import annotations

import json
from typing import Any

_UNSAFE_KEY_CHARS = frozenset(":*?[]{}")


def require_key_part(value: str, name: str) -> str:
    """Return a Redis-key-safe identifier part or raise a clear boundary error."""
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{name} is required")
    if any(char.isspace() for char in normalized):
        raise ValueError(f"{name} must not contain whitespace")
    unsafe = sorted(char for char in normalized if char in _UNSAFE_KEY_CHARS)
    if unsafe:
        joined = "".join(unsafe)
        raise ValueError(
            f"{name} must not contain Redis key delimiters/globs: {joined}"
        )
    return normalized


def require_non_empty_text(value: str, name: str) -> str:
    """Return non-empty human text, preserving internal whitespace."""
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{name} is required")
    return normalized


def enforce_max_text(value: str, name: str, max_chars: int) -> str:
    """Validate a bounded text field."""
    normalized = require_non_empty_text(value, name)
    if len(normalized) > max_chars:
        raise ValueError(f"{name} exceeds {max_chars} characters")
    return normalized


def validate_json_payload(
    payload: dict[str, Any] | None,
    *,
    max_bytes: int,
) -> dict[str, Any]:
    """Return a JSON-safe payload bounded by serialized byte size."""
    safe_payload = dict(payload or {})
    try:
        serialized = json.dumps(safe_payload, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise ValueError("payload must be JSON serializable") from exc
    if len(serialized.encode("utf-8")) > max_bytes:
        raise ValueError(f"payload exceeds {max_bytes} bytes")
    return safe_payload
