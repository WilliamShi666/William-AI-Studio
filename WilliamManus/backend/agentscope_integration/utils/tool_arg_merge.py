"""Helpers for merging streamed tool-call payloads across provider modes."""

from __future__ import annotations

import json
import os
import re
from typing import Any, Mapping


def merge_stream_text(previous: str, incoming: str) -> str:
    """Merge streamed text fields that may be delta or cumulative snapshots."""
    if not incoming:
        return previous or ""
    if not previous:
        return incoming
    if incoming.startswith(previous):
        return incoming
    if previous.startswith(incoming):
        return previous

    overlap = min(len(previous), len(incoming), 64)
    for size in range(overlap, 0, -1):
        if previous.endswith(incoming[:size]):
            return previous + incoming[size:]
    return previous + incoming


def merge_tool_arguments(previous: str, incoming: str) -> str:
    """Merge provider tool-call argument chunks supporting delta and cumulative modes."""
    if not incoming:
        return previous or ""
    if not previous:
        return incoming

    # Some providers stream cumulative JSON snapshots rather than pure deltas.
    # Only collapse chunks when both sides clearly look like the same growing
    # JSON object.  Do not apply generic suffix/prefix overlap de-duplication:
    # true delta streams can legitimately split on repeated characters
    # ("045" + "5", "occur" + "rences"), and treating those as overlap drops
    # bytes from tool-call arguments.
    if previous.lstrip().startswith("{") and incoming.lstrip().startswith("{"):
        shared_prefix = len(os.path.commonprefix([previous, incoming]))
        min_len = min(len(previous), len(incoming))
        # Tiny incoming fragments such as "{" can be legitimate content deltas
        # inside a JSON string (`r"^a{2,4}"`).  Treat startswith as cumulative
        # only after the compared prefix is long enough to identify the same
        # JSON object snapshot; otherwise append the byte exactly.
        cumulative_prefix_floor = 16
        if (
            incoming.startswith(previous)
            or (
                previous.startswith(incoming)
                and len(incoming) >= cumulative_prefix_floor
            )
            or shared_prefix >= max(cumulative_prefix_floor, int(min_len * 0.6))
        ):
            return incoming if len(incoming) >= len(previous) else previous

    return previous + incoming


def is_write_file_tool_name(name: Any) -> bool:
    normalized = str(name or "").replace("-", "_").lower()
    return (
        normalized in {"write_file", "writefile"}
        or ("write" in normalized and "file" in normalized)
    )


def _decode_partial_json_string(raw_value: str) -> str:
    trailing_backslashes = re.search(r"\\+$", raw_value)
    safe_value = raw_value
    if trailing_backslashes and len(trailing_backslashes.group(0)) % 2 == 1:
        safe_value = raw_value[:-1]

    try:
        return json.loads(f'"{safe_value}"')
    except Exception:
        return (
            safe_value.replace("\\n", "\n")
            .replace("\\t", "\t")
            .replace("\\r", "\r")
            .replace('\\"', '"')
            .replace("\\\\", "\\")
        )


_JSON_FIELD_TERMINATOR_RE = re.compile(
    r"^\s*(?:}|,\s*}|,\s*\"(?:path|file_path|target_file|file|content|file_contents|"
    r"encoding|append|overwrite|create_dirs|mode|permissions|newline|charset|"
    r"binary|mkdir_p|recursive|force)\"\s*:|,\s*$)",
    re.DOTALL,
)
_WRITE_FILE_PATH_KEYS = ("file_path", "path", "target_file", "file")
_WRITE_FILE_CONTENT_KEYS = ("file_contents", "content")
_WRITE_FILE_BOOL_KEYS = (
    "append",
    "overwrite",
    "mkdir_p",
    "create_dirs",
    "recursive",
    "force",
)
_WRITE_FILE_STR_KEYS = (
    "encoding",
    "newline",
    "charset",
    "mode",
    "permissions",
)
_PSEUDO_JSON_FRAGMENT_RE = re.compile(
    r'",\s*"[^"\n]{1,120}"\s*:\s*"',
    re.DOTALL,
)
_WRITE_FILE_ALLOWED_KEYS = {
    "path",
    "content",
    *_WRITE_FILE_BOOL_KEYS,
    *_WRITE_FILE_STR_KEYS,
}
_WRITE_FILE_BARE_FILENAME_ALLOWLIST = {
    "makefile",
    "dockerfile",
    "readme",
    "license",
    "procfile",
    "rakefile",
}


def _looks_like_json_string_terminator(tail: str) -> bool:
    if tail == "":
        return True
    return bool(_JSON_FIELD_TERMINATOR_RE.match(tail))


def _read_lenient_json_string(raw_text: str, start_idx: int) -> str:
    parts: list[str] = []
    idx = start_idx
    while idx < len(raw_text):
        current = raw_text[idx]
        if current == "\\":
            if idx + 1 < len(raw_text):
                parts.append(current)
                parts.append(raw_text[idx + 1])
                idx += 2
                continue
            parts.append(current)
            idx += 1
            break

        if current == '"':
            tail = raw_text[idx + 1 :]
            if _looks_like_json_string_terminator(tail):
                return "".join(parts)

        parts.append(current)
        idx += 1

    return "".join(parts)


def _extract_lenient_json_string_field(
    raw_arguments: str,
    field_names: tuple[str, ...],
) -> str | None:
    if not raw_arguments:
        return None

    field_group = "|".join(re.escape(name) for name in field_names)
    match = re.search(
        rf'"(?:{field_group})"\s*:\s*"',
        raw_arguments,
        re.DOTALL,
    )
    if not match:
        return None

    raw_value = _read_lenient_json_string(raw_arguments, match.end())
    return _decode_partial_json_string(raw_value)


def extract_partial_write_file_input(raw_arguments: str) -> dict[str, Any]:
    """Extract path/content from incomplete write_file JSON argument strings."""
    if not raw_arguments:
        return {}

    partial: dict[str, Any] = {}
    decoded_path = _extract_lenient_json_string_field(
        raw_arguments,
        _WRITE_FILE_PATH_KEYS,
    )
    if decoded_path:
        partial["path"] = decoded_path

    decoded_content = _extract_lenient_json_string_field(
        raw_arguments,
        _WRITE_FILE_CONTENT_KEYS,
    )
    if decoded_content is not None:
        partial["content"] = decoded_content

    return partial


def _normalize_write_file_content(value: Any) -> str:
    if isinstance(value, str):
        return value
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        try:
            return json.dumps(value, ensure_ascii=False)
        except Exception:
            return str(value)
    return str(value)


def _write_content_quality_score(value: str) -> int:
    penalty = len(_PSEUDO_JSON_FRAGMENT_RE.findall(value))
    return len(value) - (penalty * 80)


def is_plausible_write_file_path(value: Any) -> bool:
    """Return whether a candidate path looks like an actual file path."""
    if not isinstance(value, str):
        return False

    normalized = value.strip()
    if not normalized:
        return False
    if len(normalized) > 1024:
        return False
    if any(char in normalized for char in ("\n", "\r", "\t", '"', "'")):
        return False
    if "://" in normalized:
        return False

    lowered = normalized.lower()
    if lowered in _WRITE_FILE_BARE_FILENAME_ALLOWLIST:
        return True

    if normalized.startswith(("/", "./", "../", "~")):
        return True
    if "/" in normalized or "\\" in normalized:
        return True
    if "." in normalized:
        return True
    return False


def sanitize_write_file_input(
    payload: Mapping[str, Any] | None,
    *,
    raw_arguments: str | None = None,
) -> dict[str, Any]:
    """Return a normalized write_file payload with only allowed fields."""
    sanitized: dict[str, Any] = {}
    mapping: Mapping[str, Any] = payload or {}

    path_value: Any = None
    for key in _WRITE_FILE_PATH_KEYS:
        candidate = mapping.get(key)
        if is_plausible_write_file_path(candidate):
            path_value = candidate
            break
    if isinstance(path_value, str):
        sanitized["path"] = path_value.strip()

    content_value: Any = None
    for key in _WRITE_FILE_CONTENT_KEYS:
        if key in mapping:
            content_value = mapping.get(key)
            break
    if content_value is not None:
        sanitized["content"] = _normalize_write_file_content(content_value)

    for key in _WRITE_FILE_BOOL_KEYS:
        value = mapping.get(key)
        if isinstance(value, bool):
            sanitized[key] = value

    for key in _WRITE_FILE_STR_KEYS:
        value = mapping.get(key)
        if isinstance(value, str) and value:
            sanitized[key] = value

    if raw_arguments:
        recovered = extract_partial_write_file_input(raw_arguments)
        recovered_path = recovered.get("path")
        if is_plausible_write_file_path(recovered_path):
            recovered_path_str = str(recovered_path).strip()
            current_path = sanitized.get("path")
            if not isinstance(current_path, str) or len(recovered_path_str) >= len(current_path):
                sanitized["path"] = recovered_path_str
        if "content" in recovered:
            recovered_content = _normalize_write_file_content(recovered.get("content"))
            current_content = _normalize_write_file_content(sanitized.get("content"))
            if not current_content:
                sanitized["content"] = recovered_content
            else:
                recovered_score = _write_content_quality_score(recovered_content)
                current_score = _write_content_quality_score(current_content)
                if recovered_score > (current_score + 16):
                    sanitized["content"] = recovered_content

    return sanitized


def _try_load_json_mapping(raw_arguments: str) -> Mapping[str, Any] | None:
    if not isinstance(raw_arguments, str) or not raw_arguments.strip():
        return None
    try:
        loaded = json.loads(raw_arguments)
    except Exception:
        return None
    if isinstance(loaded, Mapping):
        return loaded
    return None


def _score_write_file_payload(payload: Mapping[str, Any], raw_arguments: str) -> int:
    path_value = payload.get("path")
    content_value = _normalize_write_file_content(payload.get("content"))

    score = 0
    if isinstance(path_value, str) and path_value.strip():
        score += 400
    if content_value:
        score += max(0, _write_content_quality_score(content_value))
    score += min(len(raw_arguments), 4096) // 16
    return score


def _write_payload_has_material_gain(
    baseline: Mapping[str, Any] | None,
    candidate: Mapping[str, Any],
) -> bool:
    """Return whether candidate meaningfully improves executable write payload quality."""
    if not baseline:
        return True

    baseline_path = baseline.get("path")
    candidate_path = candidate.get("path")
    if (
        isinstance(candidate_path, str)
        and candidate_path.strip()
        and not isinstance(baseline_path, str)
    ):
        return True

    baseline_content = _normalize_write_file_content(baseline.get("content"))
    candidate_content = _normalize_write_file_content(candidate.get("content"))
    baseline_score = _write_content_quality_score(baseline_content)
    candidate_score = _write_content_quality_score(candidate_content)
    return candidate_score >= (baseline_score + 4)


def merge_write_file_arguments(previous: str, incoming: str) -> str:
    """Merge write_file chunks while preferring the cleanest recoverable payload."""
    merged = merge_tool_arguments(previous, incoming)

    candidates: list[str] = []
    for candidate in (merged, incoming, previous):
        if isinstance(candidate, str) and candidate and candidate not in candidates:
            candidates.append(candidate)

    best_payload: Mapping[str, Any] | None = None
    best_raw: str | None = None
    best_is_parseable = False
    best_score = -1

    for candidate in candidates:
        parsed = _try_load_json_mapping(candidate)
        sanitized = sanitize_write_file_input(
            parsed or {},
            raw_arguments=candidate,
        )
        if not sanitized:
            continue
        score = _score_write_file_payload(sanitized, candidate)
        if parsed is not None:
            # Parseable snapshots are useful, but should not dominate longer richer raw accumulations.
            score += 8
            unknown_keys = [key for key in parsed.keys() if key not in _WRITE_FILE_ALLOWED_KEYS]
            if unknown_keys:
                score -= min(200, len(unknown_keys) * 60)
        if candidate == incoming and _write_payload_has_material_gain(best_payload, sanitized):
            score += 8
        if best_raw and len(candidate) < int(len(best_raw) * 0.8):
            if not _write_payload_has_material_gain(best_payload, sanitized):
                continue
        if score > best_score:
            best_score = score
            best_payload = sanitized
            best_raw = candidate
            best_is_parseable = parsed is not None

    if best_payload is None:
        return merged

    if not best_is_parseable:
        # Keep raw accumulation for incomplete streams; defer repair/finalization to later chunks.
        return merged

    # Do not regress to much shorter parseable payloads if merged raw text still carries more recoverable content.
    if best_raw and len(merged) > 0 and len(best_raw) < int(len(merged) * 0.8):
        merged_partial = extract_partial_write_file_input(merged)
        merged_content = _normalize_write_file_content(merged_partial.get("content"))
        best_content = _normalize_write_file_content(best_payload.get("content"))
        if _write_content_quality_score(merged_content) >= (_write_content_quality_score(best_content) + 32):
            return merged

    canonical = json.dumps(dict(best_payload), ensure_ascii=False)
    if best_raw == merged:
        return merged if len(merged) >= len(canonical) else canonical
    return canonical
