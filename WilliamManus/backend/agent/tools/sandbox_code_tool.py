import asyncio
import base64
import difflib
import hashlib
import json
import math
import os
import posixpath
import re
import shlex
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

import structlog

from agent.run_status_projection import build_response_list_key
from agentpress.tool import ToolResult
from sandbox.tool_base import SandboxToolsBase
from services import redis
from utils.agent_run_context import (
    get_agent_run_context,
    get_agent_run_execution_context,
)
from utils.config import config
from utils.logger import logger

if TYPE_CHECKING:
    from agentpress.adk_thread_manager import ADKThreadManager


MAX_OUTPUT_CHARS = int(os.getenv("AGENTSCOPE_TOOL_OUTPUT_MAX_CHARS", "8000"))
READ_FILE_MAX_BYTES = int(os.getenv("AGENTSCOPE_READ_FILE_MAX_BYTES", "50000"))
WRITE_STREAM_MIN_CHUNK_CHARS = int(
    os.getenv("AGENTSCOPE_WRITE_STREAM_MIN_CHUNK_CHARS", "1"),
)
WRITE_STREAM_TOKEN_MODE_MAX_CHARS = int(
    os.getenv("AGENTSCOPE_WRITE_STREAM_TOKEN_MODE_MAX_CHARS", "256"),
)
WRITE_STREAM_MAX_UPDATES = int(os.getenv("AGENTSCOPE_WRITE_STREAM_MAX_UPDATES", "20"))
WRITE_STREAM_HIGH_BACKLOG_THRESHOLD = int(
    os.getenv("AGENTSCOPE_WRITE_STREAM_HIGH_BACKLOG_THRESHOLD", "4000"),
)
WRITE_STREAM_HIGH_BACKLOG_MAX_UPDATES = max(
    1,
    int(os.getenv("AGENTSCOPE_WRITE_STREAM_HIGH_BACKLOG_MAX_UPDATES", "4")),
)
WRITE_STREAM_MAX_CHARS = int(os.getenv("AGENTSCOPE_WRITE_STREAM_MAX_CHARS", "50000"))
WRITE_STREAM_CHUNK_DELAY_SEC = float(os.getenv("WRITE_STREAM_CHUNK_DELAY_SEC", "0.02"))
WRITE_FILE_STATUS_ENABLED = os.getenv(
    "AGENTSCOPE_WRITE_FILE_STATUS_ENABLED", "true"
).lower() in {"1", "true", "yes", "on"}
REDIS_PUSH_TIMEOUT_SECONDS = float(
    os.getenv("AGENTSCOPE_REDIS_WRITE_TIMEOUT_SECONDS", "3.0")
)
RUN_WRITE_LIVENESS_CACHE_SECONDS = max(
    0.0,
    float(os.getenv("AGENTSCOPE_RUN_WRITE_LIVENESS_CACHE_SECONDS", "0.25")),
)
CODE_GUARD_ENABLED = os.getenv("AGENTSCOPE_CODE_GUARD_ENABLED", "true").lower() in {
    "1",
    "true",
    "yes",
    "on",
}
CODE_GUARD_LANGS = {
    lang.strip().lower().lstrip(".")
    for lang in os.getenv("AGENTSCOPE_CODE_GUARD_LANGS", "py").split(",")
    if lang.strip()
}
CODE_GUARD_AUTOFORMAT = os.getenv(
    "AGENTSCOPE_CODE_GUARD_AUTOFORMAT", "true"
).lower() in {"1", "true", "yes", "on"}
CODE_GUARD_MAX_FILE_KB = int(os.getenv("AGENTSCOPE_CODE_GUARD_MAX_FILE_KB", "512"))
CODE_GUARD_TAB_WIDTH = max(1, int(os.getenv("AGENTSCOPE_CODE_GUARD_TAB_WIDTH", "4")))
CODE_GUARD_MAX_REPAIR_PASSES = max(
    1,
    int(os.getenv("AGENTSCOPE_CODE_GUARD_MAX_REPAIR_PASSES", "6")),
)
EDIT_FILE_MAX_DIAGNOSTIC_SUGGESTIONS = int(
    os.getenv("AGENTSCOPE_EDIT_FILE_MAX_DIAGNOSTIC_SUGGESTIONS", "5"),
)
EDIT_FILE_DIAGNOSTIC_PREVIEW_CHARS = int(
    os.getenv("AGENTSCOPE_EDIT_FILE_DIAGNOSTIC_PREVIEW_CHARS", "200"),
)
EDIT_FILE_MAX_OLD_TEXT_BYTES = int(
    os.getenv("AGENTSCOPE_EDIT_FILE_MAX_OLD_TEXT_BYTES", "65536"),
)
READ_FILE_BINARY_SAMPLE_BYTES = 4096
READ_FILE_BINARY_CONTROL_CHAR_THRESHOLD = 0.20
READ_FILE_BINARY_REPLACEMENT_CHAR_THRESHOLD = 0.01
READ_FILE_LINE_NUMBERS = os.getenv(
    "AGENTSCOPE_READ_FILE_LINE_NUMBERS", "true"
).lower() in {"1", "true", "yes", "on"}
_DASHSCOPE_COMMAND_ENV_KEYS = (
    "DASHSCOPE_API_KEY",
    "DASHSCOPE_BASE_URL",
    "DASHSCOPE_VISION_MODEL",
)

_run_write_liveness_cache: Dict[str, Tuple[float, bool]] = {}


def _truncate(text: str, limit: int = MAX_OUTPUT_CHARS) -> str:
    """Head-only truncation for general tool output."""
    if text and len(text) > limit:
        return text[:limit] + f"...<truncated {len(text) - limit} chars>"
    return text or ""


def _truncate_head_tail(
    text: str,
    limit: int = MAX_OUTPUT_CHARS,
    *,
    head_chars: int = 500,
    tail_chars: int = 500,
) -> str:
    """Middle-out truncation that preserves both head and tail.

    Useful for command output where errors appear at the end.
    If *text* is short enough, return it unchanged.  Otherwise keep
    *head_chars* from the start and *tail_chars* from the end, replacing
    the middle with a truncation marker.
    """
    if not text:
        return ""
    if len(text) <= limit:
        return text
    head = text[:head_chars]
    tail = text[-tail_chars:]
    omitted = len(text) - head_chars - tail_chars
    return f"{head}\n...<truncated {omitted} chars>...\n{tail}"


def _build_command_envs(envs: Optional[Dict[str, str]]) -> Optional[Dict[str, str]]:
    """Prepare per-command env map while keeping DashScope vars available."""
    merged_envs: Dict[str, str] = {}
    if envs:
        for key, value in envs.items():
            if value is None:
                continue
            merged_envs[str(key)] = str(value)

    for dashscope_key in _DASHSCOPE_COMMAND_ENV_KEYS:
        dashscope_value = getattr(config, dashscope_key, None)
        if dashscope_value and dashscope_key not in merged_envs:
            merged_envs[dashscope_key] = str(dashscope_value)

    return merged_envs or None


def _looks_like_binary_content(raw_bytes: bytes, decoded_text: str) -> bool:
    if not raw_bytes:
        return False
    if b"\x00" in raw_bytes:
        return True

    sample = raw_bytes[:READ_FILE_BINARY_SAMPLE_BYTES]
    if not sample:
        return False

    control_count = sum(
        1 for value in sample if value < 32 and value not in (9, 10, 13)
    )
    if (control_count / max(1, len(sample))) >= READ_FILE_BINARY_CONTROL_CHAR_THRESHOLD:
        return True

    replacement_count = decoded_text.count("\ufffd")
    if (
        replacement_count > 0
        and (replacement_count / max(1, len(decoded_text)))
        >= READ_FILE_BINARY_REPLACEMENT_CHAR_THRESHOLD
    ):
        return True

    return False


def _coerce_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{field_name} must be an integer, got boolean")
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        candidate = value.strip()
        if not candidate:
            raise ValueError(f"{field_name} must be an integer, got empty string")
        try:
            return int(candidate)
        except ValueError as exc:
            raise ValueError(f"{field_name} must be an integer, got {value!r}") from exc
    raise ValueError(f"{field_name} must be an integer, got {type(value).__name__}")


def _normalize_whitespace(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _preview_visible_whitespace(
    text: str, limit: int = EDIT_FILE_DIAGNOSTIC_PREVIEW_CHARS
) -> str:
    preview = _truncate(text, limit=limit)
    return (
        preview.replace("\t", "\\t")
        .replace("\r", "\\r")
        .replace("\n", "\\n")
        .replace(" ", "·")
    )


def _build_anchor_diagnostics(
    original_content: str,
    old_text: str,
    *,
    max_suggestions: int = EDIT_FILE_MAX_DIAGNOSTIC_SUGGESTIONS,
) -> Dict[str, Any]:
    diagnostics: Dict[str, Any] = {
        "anchor_preview": _preview_visible_whitespace(old_text),
    }

    normalized_old_text = _normalize_whitespace(old_text)
    if normalized_old_text:
        diagnostics["whitespace_normalized_occurrences"] = _normalize_whitespace(
            original_content,
        ).count(normalized_old_text)
    else:
        diagnostics["whitespace_normalized_occurrences"] = 0

    target_head = next(
        (line for line in old_text.splitlines() if line.strip()),
        old_text.strip(),
    )
    target_head = target_head.strip()

    near_matches = []
    if target_head:
        for line_number, line in enumerate(original_content.splitlines(), start=1):
            candidate = line.strip()
            if not candidate:
                continue
            similarity = difflib.SequenceMatcher(
                None,
                target_head,
                candidate,
            ).ratio()
            if similarity < 0.45:
                continue
            near_matches.append(
                {
                    "line": line_number,
                    "similarity": round(similarity, 3),
                    "preview": _preview_visible_whitespace(line),
                },
            )

        near_matches.sort(key=lambda item: item["similarity"], reverse=True)
        diagnostics["near_line_matches"] = near_matches[: max(1, max_suggestions)]
    else:
        diagnostics["near_line_matches"] = []

    return diagnostics


# ── Multi-layer anchor normalization ───────────────────────────────────

_CURLY_QUOTE_MAP = {
    "‘": "'",  # LEFT SINGLE QUOTATION MARK
    "’": "'",  # RIGHT SINGLE QUOTATION MARK
    "“": '"',  # LEFT DOUBLE QUOTATION MARK
    "”": '"',  # RIGHT DOUBLE QUOTATION MARK
}


def _normalize_line_endings(text: str) -> "tuple[str, bool]":
    """Convert \\r\\n and \\r to \\n. Returns (normalized, changed)."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return normalized, normalized != text


def _strip_trailing_whitespace_per_line(text: str) -> "tuple[str, bool]":
    """Strip trailing spaces/tabs from each line. Preserves leading whitespace
    and line structure. Returns (normalized, changed)."""
    lines = text.split("\n")
    modified = False
    result_lines = []
    for line in lines:
        stripped = line.rstrip(" \t")
        if stripped != line:
            modified = True
        result_lines.append(stripped)
    return "\n".join(result_lines), modified


def _normalize_quotes(text: str) -> "tuple[str, bool]":
    """Convert curly quotes to straight quotes. Returns (normalized, changed)."""
    result = text
    modified = False
    for curly, straight in _CURLY_QUOTE_MAP.items():
        new_result = result.replace(curly, straight)
        if new_result != result:
            modified = True
            result = new_result
    return result, modified


def _find_original_span(
    original: str,
    old_text: str,
    normalized_func: "callable",
) -> "Optional[str]":
    """Map a normalized-match back to the original content.

    Applies `normalized_func` to `original` to find where `old_text` matches,
    then walks both texts in lockstep to extract the corresponding original span.
    Only works when the normalization does not change total character count
    (e.g. curly-quote → straight-quote). For count-changing normalizations
    (line endings, trailing whitespace), the caller must handle separately.
    """
    normalized_original, _ = normalized_func(original)
    idx = normalized_original.find(old_text)
    if idx == -1:
        return None
    # Check if normalization preserves character positions 1:1
    if len(original) != len(normalized_original):
        return None
    return original[idx : idx + len(old_text)]


def _find_actual_old_text(
    file_content: str,
    old_text: str,
    expected_occurrences: int,
    file_path: str = "",
) -> "tuple[Optional[str], int, list[dict]]":
    """Multi-layer progressive matching for old_text in file_content.

    Each layer that matches expected_occurrences returns immediately
    with the *original* file text at the match position so that
    str.replace() targets the exact bytes.
    """
    diagnostics: list = []
    exact_count = file_content.count(old_text)

    # L1: Exact match
    diagnostics.append({"layer": "exact", "occurrences": exact_count})
    if exact_count == expected_occurrences:
        return old_text, exact_count, diagnostics

    # L2: Line-ending normalization (\\r\\n / \\r -> \\n)
    nl_old, _ = _normalize_line_endings(old_text)
    nl_file, _ = _normalize_line_endings(file_content)
    count = nl_file.count(nl_old)
    diagnostics.append({"layer": "line_endings_normalized", "occurrences": count})
    if count == expected_occurrences:
        # Try \\r\\n variant first, then \\n as-is
        crlf_old = old_text.replace("\n", "\r\n")
        if file_content.count(crlf_old) == expected_occurrences:
            return crlf_old, count, diagnostics
        if file_content.count(old_text) == expected_occurrences:
            return old_text, count, diagnostics

    # L3: Trailing whitespace stripping (skip .md/.mdx)
    skip_trailing = file_path.endswith((".md", ".mdx"))
    if not skip_trailing:
        tws_old, _ = _strip_trailing_whitespace_per_line(old_text)
        tws_file, _ = _strip_trailing_whitespace_per_line(file_content)
        count = tws_file.count(tws_old)
        diagnostics.append(
            {"layer": "trailing_whitespace_stripped", "occurrences": count}
        )
        if count == expected_occurrences:
            actual = _match_by_normalized_lines(file_content, old_text, tws_old)
            if actual is not None:
                return actual, count, diagnostics

    # L4: Curly-quote normalization
    q_old, _ = _normalize_quotes(old_text)
    q_file, _ = _normalize_quotes(file_content)
    count = q_file.count(q_old)
    diagnostics.append({"layer": "quotes_normalized", "occurrences": count})
    if count == expected_occurrences and count != exact_count:
        actual = _find_original_span(file_content, q_old, _normalize_quotes)
        if actual is not None:
            return actual, count, diagnostics

    # L5: Combined line-ending + trailing-whitespace
    if not skip_trailing:
        combined_old, _ = _strip_trailing_whitespace_per_line(nl_old)
        combined_file, _ = _strip_trailing_whitespace_per_line(nl_file)
        count = combined_file.count(combined_old)
        diagnostics.append({"layer": "combined_normalized", "occurrences": count})
        if count == expected_occurrences:
            actual = _match_by_normalized_lines(file_content, old_text, combined_old)
            if actual is not None:
                return actual, count, diagnostics

    # L6: Fuzzy line-level matching — triggered when aggressive whitespace
    # normalization says the text should exist but L1-L5 couldn't map it.
    anchor_diag = _build_anchor_diagnostics(file_content, old_text)
    wsn = anchor_diag.get("whitespace_normalized_occurrences", 0)
    near = anchor_diag.get("near_line_matches", [])
    if wsn == expected_occurrences and near and near[0].get("similarity", 0) >= 0.90:
        actual = _fuzzy_line_match(file_content, old_text, near)
        if actual is not None:
            count = file_content.count(actual)
            diagnostics.append({"layer": "fuzzy_line_match", "occurrences": count})
            if count == expected_occurrences:
                return actual, count, diagnostics

    final_count = diagnostics[-1]["occurrences"] if diagnostics else exact_count
    return None, final_count, diagnostics


def _fuzzy_line_match(
    original: str,
    old_text: str,
    near_matches: list,
) -> "Optional[str]":
    """Try to find old_text in original by matching line-by-line starting
    from the best near_line_match position. Handles indentation differences
    and extra/missing blank lines."""
    if not near_matches or not old_text:
        return None

    best = near_matches[0]
    start_line = best.get("line", 1) - 1  # 0-based
    old_lines = old_text.split("\n")
    orig_lines = original.split("\n")

    if start_line < 0 or start_line >= len(orig_lines):
        return None

    n = len(old_lines)
    # Try a window around the best match line (±2 lines)
    for offset in (0, -1, 1, -2, 2):
        candidate_start = start_line + offset
        if candidate_start < 0 or candidate_start + n > len(orig_lines):
            continue
        window = orig_lines[candidate_start : candidate_start + n]
        # Quick check: first line must be similar
        if _line_similarity(old_lines[0], window[0]) < 0.80:
            continue
        # Check overall similarity
        overall = _lines_similarity(old_lines, window)
        if overall >= 0.85:
            return "\n".join(window)

    return None


def _line_similarity(a: str, b: str) -> float:
    """Similarity of two lines, ignoring leading/trailing whitespace."""
    a_norm = a.strip()
    b_norm = b.strip()
    if a_norm == b_norm:
        return 1.0
    if not a_norm or not b_norm:
        return 0.0
    return difflib.SequenceMatcher(None, a_norm, b_norm).ratio()


def _lines_similarity(a_lines: list, b_lines: list) -> float:
    """Average similarity across all line pairs."""
    if len(a_lines) != len(b_lines):
        return 0.0
    if not a_lines:
        return 1.0
    total = sum(_line_similarity(la, lb) for la, lb in zip(a_lines, b_lines))
    return total / len(a_lines)


def _match_by_normalized_lines(
    original: str,
    old_text: str,
    normalized_old: str,
) -> "Optional[str]":
    """Find the original span of old_text by matching line-by-line against
    a normalized version of the file. Handles trailing-whitespace and
    combined line-ending+whitespace normalization."""
    norm_lines = normalized_old.split("\n")
    orig_lines = original.split("\n")
    # Build normalized original lines using same normalization that produced
    # normalized_old — we compare line-by-line after stripping trailing whitespace
    # and normalizing line endings on each original line
    norm_orig_lines = [line.replace("\r", "").rstrip(" \t") for line in orig_lines]
    n = len(norm_lines)
    for i in range(len(norm_orig_lines) - n + 1):
        if norm_orig_lines[i : i + n] == norm_lines:
            return "\n".join(orig_lines[i : i + n])
    return None


def _classify_anchor_failure(
    actual_count: int,
    expected_occurrences: int,
    diagnostics: list,
    *,
    original_content: str = "",
    anchor_diag: dict | None = None,
    old_text: str = "",
) -> "tuple[str, str]":
    """Classify anchor match failure into structured error code + message."""
    normalized_ok = any(
        d.get("occurrences", 0) == expected_occurrences for d in diagnostics
    )

    anchor_diag = anchor_diag or {}
    near_matches = anchor_diag.get("near_line_matches", [])
    wsn = anchor_diag.get("whitespace_normalized_occurrences", 0)

    if actual_count == 0 and not normalized_ok:
        msg_parts = [
            "String to replace not found in file (even after quote and "
            "whitespace normalization). Use read_file to verify the exact "
            "content, then copy the text verbatim as old_text.\n"
            f"String: {old_text[:500]}{'...' if len(old_text) > 500 else ''}",
        ]
        if wsn == expected_occurrences:
            msg_parts.append(
                "Your text matches after ignoring whitespace — check "
                "indentation and trailing spaces."
            )
        if near_matches:
            lines_hint = ", ".join(f"line {m['line']}" for m in near_matches[:3])
            msg_parts.append(
                f"Nearest matches at {lines_hint}. Try copying text from "
                f"line {near_matches[0]['line']} of read_file output."
            )
        return ("ANCHOR_NOT_FOUND", " ".join(msg_parts))

    if normalized_ok and actual_count != expected_occurrences:
        msg = (
            "old_text matched after whitespace normalization but the exact "
            "file span could not be pinpointed for replacement."
        )
        if near_matches:
            msg += (
                f" Nearest match at line {near_matches[0]['line']}. "
                "Use read_file to copy the exact text at that location."
            )
        return ("ANCHOR_MAPPING_FAILED", msg)

    if actual_count > 1 and expected_occurrences == 1:
        msg = (
            f"Found {actual_count} matches of old_text but expected_occurrences=1. "
            f"To replace ALL occurrences, set expected_occurrences={actual_count}. "
            f"To replace only one, add more surrounding context lines to old_text "
            f"so it uniquely identifies the target."
        )
        if original_content and old_text and len(original_content) < 100_000:
            positions = _find_match_positions(original_content, old_text)
            if positions:
                lines = original_content.split("\n")
                context_parts = []
                for idx, pos in enumerate(positions[:4], 1):
                    ctx = "\n".join(lines[max(0, pos - 1) : pos + 2])
                    context_parts.append(f"  Match {idx} (line {pos + 1}):\n{ctx}")
                if context_parts:
                    msg += "\n\nMatch locations:\n" + "\n".join(context_parts)
        return ("ANCHOR_MULTIPLE_MATCHES", msg)

    return (
        "ANCHOR_COUNT_MISMATCH",
        f"Expected {expected_occurrences} occurrence(s) of old_text but found "
        f"{actual_count} after normalization. Check your expected_occurrences "
        f"value or re-read the file to verify the exact text.",
    )


def _find_match_positions(content: str, search: str) -> "list[int]":
    """Find all line indices (0-based) where search appears in content."""
    if not search or not content:
        return []
    lines = content.split("\n")
    return [i for i, line in enumerate(lines) if search in line]


def _split_lines_keep_newline(text: str) -> Tuple[List[str], bool]:
    trailing_newline = text.endswith("\n")
    lines = text.split("\n")
    if trailing_newline and lines:
        lines = lines[:-1]
    return lines, trailing_newline


def _join_lines_with_newline(lines: List[str], trailing_newline: bool) -> str:
    joined = "\n".join(lines)
    if trailing_newline:
        return joined + "\n"
    return joined


def _leading_spaces(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _build_syntax_error_payload(exc: SyntaxError) -> Dict[str, Any]:
    return {
        "error_type": exc.__class__.__name__,
        "message": exc.msg,
        "line": exc.lineno,
        "offset": exc.offset,
        "text": (exc.text or "").strip(),
    }


def _validate_python_syntax(
    content: str, filename: str
) -> tuple[bool, Optional[Dict[str, Any]]]:
    try:
        compile(content, filename, "exec")
        return True, None
    except SyntaxError as exc:
        return False, _build_syntax_error_payload(exc)


def _normalize_python_content(content: str) -> tuple[str, Dict[str, bool]]:
    normalized = content.replace("\r\n", "\n").replace("\r", "\n")
    had_newline_normalization = normalized != content
    lines, trailing_newline = _split_lines_keep_newline(normalized)
    tab_normalized = False
    normalized_lines: List[str] = []
    for line in lines:
        leading_len = len(line) - len(line.lstrip(" \t"))
        leading = line[:leading_len]
        trailing = line[leading_len:]
        if "\t" in leading:
            tab_normalized = True
            leading = leading.replace("\t", " " * CODE_GUARD_TAB_WIDTH)
        normalized_lines.append(leading + trailing)

    return _join_lines_with_newline(normalized_lines, trailing_newline), {
        "normalized_tabs": tab_normalized,
        "normalized_line_endings": had_newline_normalization,
    }


def _is_python_path(path: str) -> bool:
    return str(path or "").lower().endswith(".py")


def _is_indentation_error(error_payload: Dict[str, Any]) -> bool:
    error_type = str(error_payload.get("error_type") or "")
    message = str(error_payload.get("message") or "").lower()
    return (
        error_type in {"IndentationError", "TabError"}
        or "expected an indented block" in message
        or "unexpected indent" in message
        or "unindent does not match" in message
    )


def _repair_single_indentation_issue(
    content: str,
    error_payload: Dict[str, Any],
) -> tuple[str, Optional[Dict[str, Any]]]:
    lines, trailing_newline = _split_lines_keep_newline(content)
    if not lines:
        return content, None

    line_no_raw = error_payload.get("line")
    if not isinstance(line_no_raw, int) or line_no_raw <= 0:
        return content, None

    line_idx = min(max(line_no_raw - 1, 0), len(lines) - 1)
    message = str(error_payload.get("message") or "").lower()

    target_idx = line_idx
    if "expected an indented block" in message and not lines[target_idx].strip():
        for idx in range(line_idx + 1, len(lines)):
            if lines[idx].strip():
                target_idx = idx
                break

    original_line = lines[target_idx]
    stripped_target = original_line.lstrip(" ")
    if not stripped_target:
        return content, None

    previous_idx = None
    for idx in range(target_idx - 1, -1, -1):
        if lines[idx].strip():
            previous_idx = idx
            break

    previous_indent = (
        _leading_spaces(lines[previous_idx]) if previous_idx is not None else 0
    )
    previous_line = lines[previous_idx] if previous_idx is not None else ""
    current_indent = _leading_spaces(original_line)
    desired_indent = current_indent
    strategy = "none"

    if "expected an indented block" in message:
        desired_indent = previous_indent + CODE_GUARD_TAB_WIDTH
        strategy = "indent_after_block"
    elif "unexpected indent" in message:
        desired_indent = previous_indent
        strategy = "remove_unexpected_indent"
    elif (
        "unindent does not match" in message
        or error_payload.get("error_type") == "TabError"
    ):
        known_levels = sorted(
            {_leading_spaces(line) for line in lines[:target_idx] if line.strip()},
            reverse=True,
        )
        desired_indent = 0
        for indent_level in known_levels:
            if indent_level < current_indent:
                desired_indent = indent_level
                break
        if desired_indent == current_indent and current_indent >= CODE_GUARD_TAB_WIDTH:
            desired_indent = current_indent - CODE_GUARD_TAB_WIDTH
        strategy = "align_with_known_indent"
    elif previous_line.rstrip().endswith(":"):
        desired_indent = previous_indent + CODE_GUARD_TAB_WIDTH
        strategy = "indent_after_colon"
    else:
        return content, None

    if desired_indent < 0:
        desired_indent = 0

    updated_line = f"{' ' * desired_indent}{stripped_target}"
    if updated_line == original_line:
        return content, None

    lines[target_idx] = updated_line
    return _join_lines_with_newline(lines, trailing_newline), {
        "strategy": strategy,
        "line": target_idx + 1,
        "before_indent": current_indent,
        "after_indent": desired_indent,
    }


def _attempt_python_indentation_repairs(
    content: str,
    filename: str,
) -> tuple[str, bool, Optional[Dict[str, Any]], List[Dict[str, Any]]]:
    current = content
    applied_fixes: List[Dict[str, Any]] = []

    for _ in range(CODE_GUARD_MAX_REPAIR_PASSES):
        syntax_ok, syntax_error = _validate_python_syntax(current, filename)
        if syntax_ok:
            return current, True, None, applied_fixes
        if not syntax_error or not _is_indentation_error(syntax_error):
            return current, False, syntax_error, applied_fixes

        repaired, fix_info = _repair_single_indentation_issue(current, syntax_error)
        if not fix_info or repaired == current:
            return current, False, syntax_error, applied_fixes
        applied_fixes.append(fix_info)
        current = repaired

    syntax_ok, syntax_error = _validate_python_syntax(current, filename)
    return current, syntax_ok, syntax_error, applied_fixes


def _format_syntax_error_for_user(path: str, syntax_error: Dict[str, Any]) -> str:
    error_type = syntax_error.get("error_type") or "SyntaxError"
    message = syntax_error.get("message") or "syntax validation failed"
    line = syntax_error.get("line")
    if line:
        return f"{error_type} in {path} at line {line}: {message}"
    return f"{error_type} in {path}: {message}"


def _public_validation_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    safe_payload = dict(payload)
    safe_payload.pop("processed_content", None)
    return safe_payload


def _add_line_numbers(content: str, *, start_line: int = 1) -> str:
    """Add line-number prefix like Claude Code (cat -n style).

    Format: ``   123→<content>`` where the arrow separates the line number
    from the exact file content.  The model copies everything after the arrow
    (including leading whitespace) into old_text.
    """
    if not content:
        return ""
    lines = content.split("\n")
    width = max(4, len(str(len(lines))))
    result = []
    for i, line in enumerate(lines, start=start_line):
        prefix = f"{i:>{width}}→"
        result.append(f"{prefix}{line}")
    return "\n".join(result)


def _coerce_positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


_FIX_SCRIPT_PATTERNS = [
    r"^fix.*\.(?:py|sed|sh|bash|txt)$",
    r"^_fix.*\.(?:py|sed|sh|bash|txt)$",
    r"^debug.*\.(?:py|sed|sh|bash|txt)$",
    r"^_debug.*\.(?:py|sed|sh|bash|txt)$",
    r"^repair.*\.(?:py|sed|sh|bash|txt)$",
    r"^_repair.*\.(?:py|sed|sh|bash|txt)$",
    r"^helper.*\.(?:py|sed|sh|bash|txt)$",
    r"^_helper.*\.(?:py|sed|sh|bash|txt)$",
    r"^update.*\.(?:py|sed|sh|bash|txt)$",
    r"^_update.*\.(?:py|sed|sh|bash|txt)$",
    r"^rename.*\.(?:py|sed|sh|bash|txt)$",
    r"^_rename.*\.(?:py|sed|sh|bash|txt)$",
    r"^modify.*\.(?:py|sed|sh|bash|txt)$",
    r"^_modify.*\.(?:py|sed|sh|bash|txt)$",
    r"^rewrite.*\.(?:py|sed|sh|bash|txt)$",
    r"^_rewrite.*\.(?:py|sed|sh|bash|txt)$",
    r"^patch.*\.(?:py|sed|sh|bash|txt)$",
    r"^_patch.*\.(?:py|sed|sh|bash|txt)$",
    r"^generate.*\.(?:py|sed|sh|bash|txt)$",
    r"^_generate.*\.(?:py|sed|sh|bash|txt)$",
    r"^generator.*\.(?:py|sed|sh|bash|txt)$",
    r"^_generator.*\.(?:py|sed|sh|bash|txt)$",
    r"^check.*\.(?:py|sed|sh|bash|txt)$",
    r"^test_(?:line|mid|raw|concat).*\.(?:py|sed|sh|bash|txt)$",
]


_CJK_CHAR = re.compile(r"[一-鿿㐀-䶿豈-﫿]")

TOKENIZER_SENSITIVE_CODE_ADVICE = (
    "When writing code, do not write expressions with repeated adjacent letters "
    "or digits; for example, do not try to write summary, summary_table, "
    "summary_data, 0455, bullets, or similar names in scripts. Some tokenizers "
    "may alter these expressions in tool arguments; once that happens, they "
    "cannot be reliably repaired by retyping. Replace them with simple clear "
    "names such as tbl, q5_tbl, q3_tbl, rows, cells. For DOCX generation, use "
    "the minimal DOCX template only: Document(), add_heading(), add_paragraph(), "
    'add_run(), doc.save(). Do not use Word style names such as "List Bullet"; '
    "write plain paragraphs like '- text' instead."
)

MINIMAL_DOCX_TEMPLATE = """from docx import Document

doc = Document()
doc.add_heading("Marking Report", level=1)

rows = [
    ("Q5", "Score: ", "Feedback: "),
    ("Q3", "Score: ", "Feedback: "),
]

for q, score, fb in rows:
    doc.add_heading(q, level=2)
    doc.add_paragraph(score)
    doc.add_paragraph(fb)

doc.save("/workspace/econ_marking_report.docx")
"""


def _python_syntax_failure_recovery_advice(path: str, content: str) -> str:
    line_count = len(str(content or "").splitlines())
    basename = posixpath.basename(str(path or "script.py"))
    if line_count > SHORT_PYTHON_COMMAND_MAX_LINES:
        return (
            f"{basename} is a long Python script ({line_count} lines). Do not keep "
            "repairing a long one-off script after a syntax failure. Prefer a "
            "shorter ≤100 line python3 heredoc for bounded data processing, or "
            "split the task into smaller verified scripts. If the requested "
            "deliverable can be written directly, write the deliverable instead "
            "of building another extractor script."
        )
    return (
        "If this is one-off data processing rather than reusable source code, "
        "you may switch to one short ≤100 line python3 heredoc command instead "
        "of repeated write_file/edit_file repair attempts."
    )


def _check_cjk_font_issues(content: str, file_path: str) -> "str | None":
    """Return a warning if *content* has CJK characters without proper font config.

    Only runs on .py files.  Checks two anti-patterns:
    1. CJK text present but no ``Noto Sans CJK SC`` / ``Noto Serif CJK SC`` configured.
    2. CJK characters inside ``MathTex`` / ``Tex`` (pdflatex cannot render them).
    """
    if not str(file_path or "").endswith(".py"):
        return None
    if not isinstance(content, str):
        return None
    cjk_chars = _CJK_CHAR.findall(content)
    if not cjk_chars:
        return None
    has_noto = "Noto Sans CJK SC" in content or "Noto Serif CJK SC" in content
    in_mathtex = any(
        f"MathTex({{{c}}}" in content or f"Tex({{{c}}}" in content
        for c in cjk_chars[:5]
    )
    issues = []
    if not has_noto:
        issues.append(
            "CJK text detected but no CJK font configured. "
            "Use Text('中文', font='Noto Sans CJK SC') for all Chinese text."
        )
    if in_mathtex:
        issues.append(
            "Chinese characters inside MathTex/Tex detected — pdflatex "
            "cannot render CJK. Move them to Text() with "
            "font='Noto Sans CJK SC'."
        )
    return " | ".join(issues) if issues else None


def _detect_fix_script_anti_pattern(path: str, content: str) -> str | None:
    """Detect when an agent writes a helper script whose purpose is to fix another file.

    Returns a warning message string if the anti-pattern is detected, or None.
    """
    filename = path.rsplit("/", 1)[-1]
    if not any(re.match(pat, filename) for pat in _FIX_SCRIPT_PATTERNS):
        return None
    extension = posixpath.splitext(filename)[1].lower()
    # Check if content reads/modifies another file (suggesting it's a fixer, not standalone)
    workspace_file_ref = r"['\"][^'\"]*(?:/workspace/|workspace/)[^'\"]+['\"]"
    workspace_file_matches = [
        match.strip("'\"") for match in re.findall(workspace_file_ref, content)
    ]
    target_hint = next(
        (candidate for candidate in workspace_file_matches if candidate != path),
        "the target file",
    )
    has_file_io = bool(
        re.search(r"\bopen\s*\(\s*" + workspace_file_ref, content)
        or re.search(r"\bopen\s*\([^)]*,\s*['\"][^'\"]*[wa+]", content)
        or re.search(
            r"\bPath\s*\(\s*"
            + workspace_file_ref
            + r"\s*\)\.(?:read_text|rename|replace|write_text|write_bytes|unlink)",
            content,
        )
        or re.search(r"\.(?:read_text|write_text|write_bytes)\s*\(", content)
        or re.search(r"\bos\.rename\s*\(", content)
        or re.search(r"\bos\.replace\s*\(", content)
        or re.search(r"\bshutil\.(?:move|copy|copyfile)\s*\(", content)
        or re.search(r"\breplace\b.*" + workspace_file_ref, content)
        or re.search(r"\bsubprocess\b", content)
        or extension in {".sed", ".sh", ".bash"}
    )
    if not has_file_io:
        return None
    return (
        f"⚠️  {filename} looks like a helper script to fix another file. "
        "Do not rewrite the target file and do not call write_file for the "
        "target file just to repair a small issue. Instead: call read_file on "
        f"{target_hint} to copy the exact failing lines, then call edit_file "
        "with a surgical old_text/new_text replacement. This is faster, safer, "
        "and avoids introducing new failure points. Do not write fix_quotes.py, "
        "fix_*.py, check_*.py, fix*.sed, or temporary repair/diagnostic scripts "
        "as a fallback. If shell "
        "quoting is hard, return to read_file plus edit_file; if edit_file already "
        "failed, use one correct one-shot sed -i where old_text and new_text differ. "
        "Remember: 修 A 用 edit_file 修 A，不写 B 脚本修 A。"
    )


_REWRITE_GUARD_EXTENSIONS = {
    ".py",
    ".md",
    ".html",
    ".js",
    ".ts",
    ".tsx",
    ".css",
    ".json",
}
_REWRITE_SIMILARITY_MIN_BYTES = 800
_REWRITE_SIMILARITY_THRESHOLD = 0.92
_INTERMEDIATE_HELPER_NAME_MARKERS = (
    "extract",
    "extracted",
    "dump",
    "parse",
    "inspect",
    "scratch",
    "tmp",
    "temp",
    "debug",
    "question",
    "questions",
)
_REPORT_DELIVERABLE_EXTENSIONS = {".md", ".html"}
_REPORT_DELIVERABLE_MARKERS = (
    "report",
    "analysis",
    "wrong",
    "mcq",
    "错题",
    "报告",
    "分析",
)
SHORT_PYTHON_COMMAND_MAX_LINES = max(
    1,
    int(os.getenv("AGENTSCOPE_EXECUTE_COMMAND_SHORT_PYTHON_MAX_LINES", "100")),
)
DOCUMENT_INSPECTION_REDIRECT_AFTER = max(
    1,
    int(os.getenv("AGENTSCOPE_DOCUMENT_INSPECTION_REDIRECT_AFTER", "4")),
)
_BLOCKED_INLINE_PYTHON_WRITE_EXTENSIONS = {
    ".py",
    ".js",
    ".ts",
    ".tsx",
    ".jsx",
    ".sh",
    ".bash",
    ".sed",
}
_PDF_EXTRACTION_PACKAGES = (
    "pdfplumber",
    "pypdf",
    "pypdf2",
    "pymupdf",
    "fitz",
)


def _looks_like_intermediate_helper_path(path: str) -> bool:
    suffix = posixpath.splitext(str(path).lower())[1]
    if suffix not in {".py", ".sh", ".bash", ".sed", ".txt", ".json", ".jsonl"}:
        return False
    basename = posixpath.basename(str(path).lower())
    return any(marker in basename for marker in _INTERMEDIATE_HELPER_NAME_MARKERS)


def _extract_intermediate_helper_execution_path(command: str) -> str | None:
    cmd = str(command or "")
    match = re.search(
        r"(?:^|[;&|]\s*)(?:python3?|bash|sh)\s+"
        r"(?P<path>(?:/workspace/)?[\w./-]+\.(?:py|sh|bash|sed))\b",
        cmd,
    )
    if not match:
        return None
    raw_path = str(match.group("path") or "")
    if not raw_path:
        return None
    path = (
        raw_path
        if raw_path.startswith("/workspace/")
        else f"/workspace/{raw_path.lstrip('./')}"
    )
    return path if _looks_like_intermediate_helper_path(path) else None


def _looks_like_generated_report_deliverable(
    path: str,
    content: str,
    existing_content: str | None = None,
) -> bool:
    """Return True for user-facing report artifacts that may be safely rewritten.

    The rewrite guard is intentionally strict for source/code files because agents
    often try to bypass edit_file by writing a renamed copy. Generated report
    artifacts are different: in long-running document/report tasks the agent may
    first create a partial Markdown/HTML file, then correctly replace it with the
    complete deliverable via write_file. Direct write_file report writes are also
    not counted as edits in the evaluator, so blocking them creates avoidable
    repair loops.
    """
    suffix = posixpath.splitext(str(path or "").lower())[1]
    if suffix not in _REPORT_DELIVERABLE_EXTENSIONS:
        return False

    joined = "\n".join(
        part.lower()
        for part in (str(path or ""), str(content or ""), str(existing_content or ""))
        if part
    )
    if not any(marker in joined for marker in _REPORT_DELIVERABLE_MARKERS):
        return False

    text = str(content or "").strip()
    if suffix == ".html":
        return "<html" in text.lower() and "<body" in text.lower()

    # Markdown reports usually have headings/tables/lists and enough substance to
    # be a deliverable rather than a tiny scratch note.
    return len(text) >= 800 and (
        text.startswith("#")
        or "\n## " in text
        or "|---" in text
        or "建议" in text
        or "recommend" in text.lower()
    )


def _intermediate_helper_redirect_message(path: str) -> str:
    return (
        f"{path} looks like an intermediate extraction/helper file, not the user's "
        "final deliverable. Do not delete, rewrite, or repair intermediate helpers "
        "when you already have enough evidence for a report. Use the extracted facts "
        "already obtained, write the requested deliverable directly, verify that the "
        "requested output files exist, and stop. If exactly one fact is missing, run "
        "one targeted read/search command for that fact only."
    )


def _python_exploratory_syntax_redirect_hint(command: str, stderr: str) -> str | None:
    if "SyntaxError" not in str(stderr or ""):
        return None
    cmd = str(command or "")
    if not re.search(r"\bpython(?:3)?\b", cmd):
        return None
    if "<<" not in cmd and "-c" not in cmd:
        return None
    exploratory_markers = (
        "pdf",
        "docx",
        "pptx",
        "xlsx",
        "ocr",
        "document",
        "extract",
        "grep",
        "text",
        "read()",
        "pdftotext",
        "pdfplumber",
        "pypdf",
        "pymupdf",
        "tesseract",
        "workspace",
        "report",
    )
    if not any(marker in cmd.lower() for marker in exploratory_markers):
        return None
    return (
        "This Python failure appears to be exploratory document extraction. Do not "
        "spend more turns repairing inline extraction code unless the script itself "
        "is the requested deliverable. Use simpler targeted shell commands or the "
        "evidence already extracted, then write the requested deliverable and verify "
        "the output files."
    )


def _document_dependency_install_block_payload(command: str) -> dict[str, str] | None:
    cmd = str(command or "")
    lowered = cmd.lower()
    if not re.search(r"(?:^|[;&|]\s*)(?:python3?\s+-m\s+)?pip\s+install\b", lowered):
        return None
    if not any(pkg in lowered for pkg in _PDF_EXTRACTION_PACKAGES):
        return None
    return {
        "error_code": "DOCUMENT_EXTRACTION_DEPENDENCY_INSTALL_REDIRECT",
        "error": (
            "Do not install PDF parsing libraries during a document/report task just "
            "because an optional import is missing. Use available system tools first "
            "(for example `pdftotext`, `grep`, `sed -n`, and targeted `read_file` "
            "line ranges), or proceed from evidence already extracted. Installing "
            "packages is slow, can hang in sandboxed E2E runs, and is unnecessary "
            "unless the user explicitly asked to modify the environment."
        ),
        "command": cmd,
    }


def _looks_like_document_inspection_command(command: str) -> bool:
    cmd = str(command or "").strip()
    lowered = cmd.lower()
    if not cmd:
        return False
    if re.search(r">\s*/workspace/[^\s'\";|&<>]*\.(?:md|html)\b", lowered):
        return False
    if re.search(r"(?:^|[;&|]\s*)python3?\b", lowered) and any(
        marker in lowered
        for marker in (
            ".pdf",
            ".docx",
            ".pptx",
            ".xlsx",
            "document",
            "ocr",
            "pypdf",
            "pdfplumber",
            "pymupdf",
            "tesseract",
            "pdftotext",
            "extract_text",
            "textract",
            "unstructured",
        )
    ):
        return True
    if (
        re.search(
            r"(?:^|[;&|]+\s*)(?:grep|egrep|fgrep|sed\s+-n|head|tail|cat|awk|nl\s+-ba|pdfinfo|pdftotext)\b",
            lowered,
        )
        is None
    ):
        return False
    if re.search(r"(?:^|[^2])>\s*/workspace/[^\s'\";|&<>]*\.(?:md|html)\b", lowered):
        return False
    return any(
        marker in lowered
        for marker in (
            "/workspace/extracted",
            "/workspace/extraction",
            "/workspace/document_text",
            "/workspace/pdf_text",
            "/workspace/ocr",
            "/workspace/raw",
            "/workspace/dump",
            ".pdf",
            ".docx",
            ".pptx",
            ".xlsx",
            "pdftotext",
        )
    ) or bool(
        re.search(
            r"(?:/workspace|/tmp)/[^\s'\";|&<>]+\.(?:txt|text)\b",
            lowered,
        )
        or bool(
            re.search(r"(?:^|[;&|]+\s*)cd\s+/tmp\b", lowered)
            and re.search(r"\b[^\s'\";|&<>]+\.(?:txt|text)\b", lowered)
        )
    )


def _looks_like_document_inspection_path(path: str) -> bool:
    lowered = str(path or "").strip().lower()
    if not lowered:
        return False
    if lowered.endswith((".md", ".html")):
        return False
    return any(
        marker in lowered
        for marker in (
            "/workspace/pdf_text/",
            "/workspace/extracted",
            "/workspace/extraction",
            "/workspace/document_text/",
            "/workspace/ocr/",
            "/workspace/raw/",
            "/workspace/dump/",
            ".pdf",
            ".docx",
            ".pptx",
            ".xlsx",
        )
    )


def _document_inspection_loop_redirect_message(count: int) -> str:
    return (
        "You have already run many document inspection/extraction commands "
        f"({count}). This is now an extraction loop, not progress on the user's "
        "deliverable. Stop repairing parsers, regexes, OCR/extraction commands, or "
        "helper extractors. Use the evidence already gathered plus the user's provided "
        "source data to write the requested deliverable(s) directly, verify the outputs "
        "exist, then stop. "
        "If one detail is missing, include a cautious note rather than continuing "
        "bulk extraction."
    )


def _pdf_dependency_recovery_hint(command: str, stderr: str) -> str | None:
    text = f"{command}\n{stderr}"
    lowered = text.lower()
    if "modulenotfounderror" not in lowered:
        return None
    if not any(pkg in lowered for pkg in _PDF_EXTRACTION_PACKAGES):
        return None
    if not any(
        marker in lowered for marker in ("pdf", "document", "workspace", "extract")
    ):
        return None
    return (
        "PDF extraction dependency fallback: do not install missing PDF packages. "
        "Use `pdftotext /workspace/file.pdf -` or previously extracted text, then "
        "write the requested deliverables. If only a few facts are missing, run one "
        "targeted grep/sed/read_file command instead of changing the environment."
    )


def _tokenizer_loss_recovery_hint(command: str, stderr: str) -> str | None:
    text = f"{command}\n{stderr}"
    lowered = text.lower()
    if not (
        re.search(r"did you mean:\s*['\"][^'\"]+['\"]", text, re.IGNORECASE)
        or "\\new" in text
        or "ew Chart" in text
        or "old_text and new_text are identical" in text
    ):
        return None
    if not any(
        marker in lowered
        for marker in (
            "new",
            "chart",
            "nameerror",
            "identifier",
            "old_text",
            "new_text",
        )
    ):
        return None
    return (
        "Tokenizer-sensitive expression fallback: if 3 failed attempts still cannot "
        "produce the exact token/identifier/literal, rename or rephrase the expression "
        "instead of retrying. Examples: use createChart(...) or window.Chart(...) "
        "instead of repeatedly generating `new Chart(...)`; use match_count instead "
        "of a fragile repeated identifier; split numeric/text literals such as "
        '`"0" + "455"`; build brace-heavy snippets with chr(123)/chr(125) or a '
        "small template. If the static report layer is already valid and the user did "
        "not explicitly require dynamic/interactive behavior, verify deliverables and stop."
    )


def _repair_html_script_token_loss(content: str) -> tuple[str, list[str]]:
    text = str(content or "")
    if "<script" not in text.lower():
        return text, []
    repairs: list[str] = []

    def _repair_script(match: re.Match[str]) -> str:
        nonlocal repairs
        open_tag, body, close_tag = match.group(1), match.group(2), match.group(3)
        repaired_body, count = re.subn(
            r"(?<![\w$])ew\s+Chart\s*\(",
            "new Chart(",
            body,
        )
        if count:
            repairs.append("ew Chart( -> new Chart(")
        return f"{open_tag}{repaired_body}{close_tag}"

    repaired = re.sub(
        r"(<script\b(?![^>]*\bsrc=)[^>]*>)(.*?)(</script>)",
        _repair_script,
        text,
        flags=re.IGNORECASE | re.DOTALL,
    )
    return repaired, repairs


def _html_report_noop_guidance(path: str, original_content: str) -> str:
    suffix = posixpath.splitext(str(path).lower())[1]
    if suffix != ".html":
        return ""
    lowered = str(original_content or "").lower()
    if "<html" not in lowered or "<body" not in lowered:
        return ""
    report_markers = ("报告", "report", "analysis", "分析")
    if not any(
        marker in str(path).lower() or marker in lowered for marker in report_markers
    ):
        return ""
    return (
        "\n\nHTML report guidance: this appears to be a generated HTML report with a "
        "static report layer. Treat charts, filters, and scripts as the "
        "dynamic/interactive layer. If the user explicitly asked for dynamic behavior, "
        "make one targeted non-no-op edit using a different expression/name if needed. "
        "If the user only asked for an HTML report and the static report layer is "
        "readable, verify the files and stop instead of repeatedly repairing scripts."
    )


def _rewrite_block_message(*, attempted_path: str, target_path: str) -> str:
    if _looks_like_intermediate_helper_path(target_path):
        return _intermediate_helper_redirect_message(target_path)
    return (
        f"Do not rewrite workspace files with write_file. {target_path} already "
        "contains the content you need to change, or this write is a renamed "
        "rewrite of that file. Call read_file on "
        f"{target_path} to copy the exact failing lines, then call edit_file "
        "with a surgical old_text/new_text replacement. Do not call write_file "
        f"for {attempted_path} or another renamed copy. 修 A 用 edit_file 修 A。"
    )


def _looks_like_same_file_rewrite(existing_content: str, new_content: str) -> bool:
    existing = str(existing_content or "")
    new = str(new_content or "")
    if min(len(existing), len(new)) < _REWRITE_SIMILARITY_MIN_BYTES:
        return False
    if existing == new:
        return True
    from difflib import SequenceMatcher

    ratio = SequenceMatcher(None, existing, new).ratio()
    return ratio >= _REWRITE_SIMILARITY_THRESHOLD


def _workspace_paths_in_text(text: str) -> list[str]:
    matches = re.findall(r"['\"](/workspace/[^'\"\s;|&<>]+)['\"]", text)
    matches.extend(re.findall(r"(?<![\w/])(/workspace/[^\s;|&<>]+)", text))
    cleaned: list[str] = []
    for match in matches:
        candidate = str(match).strip().rstrip(".,)\"'")
        if candidate and candidate not in cleaned:
            cleaned.append(candidate)
    return cleaned


def _python_inline_line_count(command: str) -> int:
    cmd = str(command or "")
    if not cmd:
        return 0
    if re.search(r"\bpython3?\s+(?:-\s*)?<<", cmd):
        lines = cmd.splitlines()
        return max(0, len(lines) - 2)
    match = re.search(r"\bpython3?\s+-c\s+(['\"])(?P<code>.*)\1", cmd, re.DOTALL)
    if not match:
        return cmd.count("\n") + 1
    return str(match.group("code") or "").count("\n") + 1


def _blocked_inline_python_write_targets(command: str) -> list[str]:
    blocked: list[str] = []
    for path in _workspace_paths_in_text(command):
        basename = posixpath.basename(path)
        extension = posixpath.splitext(basename)[1].lower()
        if extension in _BLOCKED_INLINE_PYTHON_WRITE_EXTENSIONS or any(
            re.match(pattern, basename) for pattern in _FIX_SCRIPT_PATTERNS
        ):
            blocked.append(path)
    return blocked


def _short_python_command_is_allowed(command: str) -> bool:
    cmd = str(command or "")
    if not (
        re.search(r"\bpython3?\s+(?:-\s*)?<<", cmd)
        or re.search(r"\bpython3?\s+-c\s+", cmd)
    ):
        return False
    if _python_inline_line_count(cmd) > SHORT_PYTHON_COMMAND_MAX_LINES:
        return False
    return not _blocked_inline_python_write_targets(cmd)


def _long_script_command_block_payload(command: str) -> dict[str, str] | None:
    cmd = str(command or "")
    writes_workspace_path = r"(?:/workspace/|workspace/)[^\s'\";|&<>]+\.(?:py|js|ts|tsx|jsx|sh|bash|sed|txt|md)"
    creates_script_with_heredoc = bool(
        re.search(
            r"\b(?:cat|tee)\b[^\n]*(?:>\s*)?" + writes_workspace_path + r"[^\n]*<<",
            cmd,
        )
    )
    runs_inline_python_heredoc = bool(re.search(r"\bpython3?\s+(?:-\s*)?<<", cmd))
    inline_python = bool(re.search(r"\bpython3?\s+-c\s+", cmd))
    blocked_python_targets = _blocked_inline_python_write_targets(cmd)
    inline_workspace_write = inline_python and bool(blocked_python_targets)
    short_python_allowed = _short_python_command_is_allowed(cmd)
    multi_line_inline_code = (
        inline_python and cmd.count("\n") >= 2 and not short_python_allowed
    )

    if not (
        creates_script_with_heredoc
        or (runs_inline_python_heredoc and not short_python_allowed)
        or inline_workspace_write
        or multi_line_inline_code
    ):
        return None

    target_path = next(
        iter(blocked_python_targets),
        next(iter(_workspace_paths_in_text(cmd)), "/workspace/script.py"),
    )
    error = (
        "Do not use execute_command to write or repair long scripts, unsafe heredocs, "
        "or python -c source-code workspace writes. Short Python data-processing "
        f"commands up to {SHORT_PYTHON_COMMAND_MAX_LINES} lines are allowed when "
        'they do not create or repair source/helper scripts. Use the minimal DOCX template. Do not use Word style names such as "List Bullet". Create scripts with write_file. Repair '
        "existing scripts with edit_file, or with one carefully scoped non-no-op "
        "sed -i after inspecting the exact line. Execute scripts only with "
        'execute_command("python /workspace/script.py"). This avoids model '
        "tool-argument corruption such as Python string modes 'r'/'w' becoming "
        "unexpected tool kwargs."
    )
    return {
        "error": error,
        "target_path": target_path,
        "reason": "long_script_command",
    }


def _helper_script_command_block_payload(command: str) -> dict[str, str] | None:
    cmd = str(command or "")
    helper_patterns = [
        pattern
        for pattern in _FIX_SCRIPT_PATTERNS
        if not pattern.startswith(r"^generate")
        and not pattern.startswith(r"^_generate")
    ]
    helper_paths = [
        path
        for path in _workspace_paths_in_text(cmd)
        if any(
            re.match(pattern, posixpath.basename(path)) for pattern in helper_patterns
        )
    ]

    creates_helper = bool(
        re.search(r"(?:cat|tee)\s+[^\n;|&]*(?:>|/workspace/)", cmd) and helper_paths
    )
    runs_helper = bool(
        re.search(r"\b(?:python3?|bash|sh)\s+[^\n;|&]*(/workspace/[^\s;|&]+)", cmd)
        and helper_paths
    )
    if not (creates_helper or runs_helper):
        return None

    workspace_targets = [
        path for path in _workspace_paths_in_text(cmd) if path not in helper_paths
    ]
    target_path = next(iter(workspace_targets), "the target file")
    helper_path = next(iter(helper_paths), "a helper script")
    error = (
        f"Do not create, upload, or run helper scripts such as {helper_path}. "
        "Do not modify existing workspace files from execute_command. "
        f"Call read_file on {target_path} to copy the exact failing lines, "
        "then call edit_file with a surgical old_text/new_text replacement. "
        "修 A 用 edit_file 修 A，不写 B 脚本修 A。"
    )
    return {
        "error": error,
        "helper_path": helper_path,
        "target_path": target_path,
        "reason": (
            "helper_script_command" if helper_paths else "inline_workspace_rewrite"
        ),
    }


def _workspace_delete_rewrite_block_payload(command: str) -> dict[str, str] | None:
    cmd = str(command or "")
    match = re.search(
        r"(?:^|[;&|]\s*)rm\s+(?:-[A-Za-zfivRr]+\s+)*"
        r"(?P<path>['\"]?/workspace/[^'\"\s;|&<>]+['\"]?)",
        cmd,
    )
    if not match:
        return None

    target_path = str(match.group("path") or "").strip("'\"")
    if _looks_like_intermediate_helper_path(target_path):
        return {
            "error": _intermediate_helper_redirect_message(target_path),
            "target_path": target_path,
            "reason": "intermediate_helper_delete_redirect",
        }
    error = (
        f"Do not delete {target_path} to bypass rewrite protection. "
        f"If {target_path} already exists and needs a fix, call read_file on "
        f"{target_path}, then call edit_file with exact old_text/new_text. "
        "Only use a one-shot sed -i fallback after edit_file fails; never rm "
        "then write_file the same artifact. 修 A 用 edit_file 修 A。"
    )
    return {
        "error": error,
        "target_path": target_path,
        "reason": "workspace_delete_rewrite_bypass",
    }


def _noop_in_place_edit_block_payload(command: str) -> dict[str, str] | None:
    cmd = str(command or "")
    if not re.search(r"\bsed\s+-i\b", cmd):
        return None

    def _extract_sed_substitution(segment: str) -> tuple[str, str] | None:
        segment = segment.strip()
        if not segment.startswith("s"):
            return None
        delim = segment[1:2]
        if not delim or delim.isalnum() or delim.isspace():
            return None
        remainder = segment[2:]
        first = remainder.find(delim)
        if first < 0:
            return None
        second = remainder.find(delim, first + 1)
        if second < 0:
            return None
        old_text = remainder[:first]
        new_text = remainder[first + 1 : second]
        return old_text, new_text

    shell_vars = {
        name: value
        for name, _quote, value in re.findall(
            r"(?:^|[;&|]\s*)([A-Za-z_][A-Za-z0-9_]*)=(['\"])(.*?)\2",
            cmd,
        )
    }

    matches: list[tuple[str, str | None]] = []
    for match in re.finditer(
        r"sed\s+-i(?:\s+['\"][^'\"]*['\"])?\s+(?P<quote>['\"])(?P<expr>[^'\"]+)(?P=quote)(?P<rest>[^;&|]*)",
        cmd,
    ):
        expr = match.group("expr")
        if expr.startswith("$"):
            expr = shell_vars.get(expr[1:], expr)
        rest = str(match.group("rest") or "")
        target_match = re.search(
            r"(?P<path>(?:/workspace/)?[\w./-]+\.(?:py|sed|sh|bash|txt|md|json|yaml|yml|html|css|js|ts|tsx))",
            rest,
        )
        target_path = target_match.group("path") if target_match else None
        matches.append((expr, target_path))

    if not matches:
        return None

    cd_workspace = bool(re.search(r"(?:^|[;&|]\s*)cd\s+/workspace(?:\s|[;&|]|$)", cmd))

    for expr, raw_target_path in matches:
        parsed = _extract_sed_substitution(expr)
        if parsed is None:
            continue
        old_text, new_text = parsed
        if old_text != new_text:
            continue
        if raw_target_path:
            target_path = (
                raw_target_path
                if raw_target_path.startswith("/workspace/")
                else (
                    f"/workspace/{raw_target_path.lstrip('./')}"
                    if cd_workspace
                    else raw_target_path
                )
            )
        else:
            target_path = next(iter(_workspace_paths_in_text(cmd)), "/workspace target")
        error = (
            f"Blocked no-op sed -i on {target_path}: old_text and new_text are identical "
            f"({old_text!r}). This does not fix the file and can cause retry loops. "
            "Use read_file to inspect the exact text, then edit_file with a real "
            "old_text/new_text difference. If edit_file already failed, use one correct "
            "sed -i fallback where the replacement actually differs, then immediately "
            "rerun the failing command. Do not write fix_quotes.py, fix_*.py, "
            "check_*.py, fix*.sed, or any temporary repair/diagnostic script. "
            f"{TOKENIZER_SENSITIVE_CODE_ADVICE}"
        )
        return {
            "error": error,
            "target_path": target_path,
            "reason": "noop_in_place_edit",
        }

    return None


class SandboxCodeTool(SandboxToolsBase):
    """PPIO code sandbox utility functions (commands, files, port exposure)."""

    def __init__(
        self,
        project_id: str,
        thread_manager: Optional["ADKThreadManager"] = None,
        *,
        shadow_clone_run_id: Optional[str] = None,
        strict_sandbox: bool = False,
    ):
        super().__init__(
            project_id=project_id,
            thread_manager=thread_manager,
            sandbox_type="code",
            shadow_clone_run_id=shadow_clone_run_id,
            strict_sandbox=strict_sandbox,
        )
        self._path_failure_counts: Dict[str, int] = {}
        self._python_syntax_failure_counts: Dict[str, int] = {}
        self._large_document_read_counts: Dict[str, int] = {}
        self._intermediate_helper_write_count = 0
        self._document_inspection_command_count = 0
        self._intermediate_helper_execution_counts: Dict[str, int] = {}

    def _normalize_path(self, path: str) -> str:
        """Normalize a path to the /workspace root and prevent traversal."""
        raw_path = str(path or ".")
        if raw_path == self.workspace_path or raw_path.startswith(
            self.workspace_path + "/"
        ):
            normalized = posixpath.normpath(raw_path)
            if normalized != self.workspace_path and not normalized.startswith(
                self.workspace_path + "/",
            ):
                raise ValueError("Path must stay within /workspace")
            return normalized
        cleaned = self.clean_path(path or ".")
        normalized = posixpath.normpath(posixpath.join(self.workspace_path, cleaned))
        if normalized != self.workspace_path and not normalized.startswith(
            self.workspace_path + "/",
        ):
            raise ValueError("Path must stay within /workspace")
        return normalized

    def _record_path_failure(self, *, tool_name: str, path: str) -> int:
        del tool_name
        key = f"path_not_found:{path}"
        next_count = self._path_failure_counts.get(key, 0) + 1
        self._path_failure_counts[key] = next_count
        return next_count

    def _clear_path_failure(self, *, tool_name: str, path: str) -> None:
        del tool_name
        self._path_failure_counts.pop(f"path_not_found:{path}", None)

    def _path_failure_count(self, path: str) -> int:
        return self._path_failure_counts.get(f"path_not_found:{path}", 0)

    def _record_python_syntax_failure(self, path: str) -> int:
        key = f"python_syntax:{path}"
        next_count = self._python_syntax_failure_counts.get(key, 0) + 1
        self._python_syntax_failure_counts[key] = next_count
        return next_count

    def _clear_python_syntax_failure(self, path: str) -> None:
        self._python_syntax_failure_counts.pop(f"python_syntax:{path}", None)

    @staticmethod
    def _levenshtein_distance(left: str, right: str, max_distance: int = 2) -> int:
        if left == right:
            return 0
        if abs(len(left) - len(right)) > max_distance:
            return max_distance + 1
        previous = list(range(len(right) + 1))
        for i, left_char in enumerate(left, start=1):
            current = [i]
            row_min = current[0]
            for j, right_char in enumerate(right, start=1):
                insert_cost = current[j - 1] + 1
                delete_cost = previous[j] + 1
                replace_cost = previous[j - 1] + (0 if left_char == right_char else 1)
                value = min(insert_cost, delete_cost, replace_cost)
                current.append(value)
                row_min = min(row_min, value)
            if row_min > max_distance:
                return max_distance + 1
            previous = current
        return previous[-1]

    async def _find_unique_path_auto_correction(
        self,
        attempted_path: str,
        *,
        max_distance: int = 2,
    ) -> dict[str, Any] | None:
        directory = posixpath.dirname(attempted_path) or self.workspace_path
        target_name = posixpath.basename(attempted_path)
        try:
            entries = await self._run_blocking_sandbox_call(
                "files.list",
                lambda: self.sandbox.files.list(directory),
            )
        except Exception:
            return None

        candidates: list[dict[str, Any]] = []
        for entry in entries or []:
            if isinstance(entry, dict):
                raw_name = entry.get("name") or entry.get("path") or ""
                raw_path = entry.get("path") or raw_name
            else:
                raw_name = getattr(entry, "name", "") or str(entry)
                raw_path = getattr(entry, "path", "") or raw_name
            candidate_name = posixpath.basename(str(raw_name or raw_path or ""))
            if not candidate_name:
                continue
            distance = self._levenshtein_distance(
                target_name,
                candidate_name,
                max_distance=max_distance,
            )
            if distance > max_distance:
                continue
            candidate_path = str(raw_path or candidate_name)
            if candidate_path.startswith(self.workspace_path + "/"):
                pass
            elif candidate_path.startswith("/"):
                candidate_path = posixpath.join(
                    self.workspace_path,
                    candidate_path.lstrip("/"),
                )
            else:
                candidate_path = posixpath.join(directory, candidate_path)
            candidates.append(
                {
                    "path": self._normalize_path(candidate_path),
                    "name": candidate_name,
                    "distance": distance,
                },
            )

        if not candidates:
            return None
        candidates.sort(key=lambda item: (item["distance"], item["name"]))
        best_distance = candidates[0]["distance"]
        best_candidates = [
            candidate
            for candidate in candidates
            if candidate["distance"] == best_distance
        ]
        if len(best_candidates) != 1:
            return {"ambiguous": True, "candidates": candidates[:5]}
        return best_candidates[0]

    def _annotate_auto_corrected_output(
        self,
        result: ToolResult,
        *,
        original_path: str,
        corrected_path: str,
        distance: int,
    ) -> ToolResult:
        if not isinstance(result.output, dict):
            return result
        annotation = (
            f"[Auto-corrected] Path '{original_path}' not found \u2192 corrected to "
            f"'{corrected_path}' ({distance} character difference). "
            "Original tool result follows:\n\n"
        )
        output = dict(result.output)
        output["auto_corrected_path"] = True
        output["original_path"] = original_path
        output["corrected_path"] = corrected_path
        output["auto_correct_distance"] = distance
        if isinstance(output.get("output"), str):
            output["output"] = annotation + output["output"]
        else:
            output["output"] = annotation + str(output.get("content", ""))
        return ToolResult(success=result.success, output=output)

    async def _read_existing_workspace_text(self, path: str) -> str | None:
        try:
            raw = await self._run_blocking_sandbox_call(
                "files.read",
                lambda: self.sandbox.files.read(path),
            )
        except Exception:
            read_artifact = getattr(self, "_read_workspace_artifact_bytes", None)
            if not callable(read_artifact):
                return None
            artifact_bytes = await read_artifact(path)
            if artifact_bytes is None:
                return None
            return artifact_bytes.decode("utf-8", errors="ignore")
        if isinstance(raw, (bytes, bytearray)):
            return bytes(raw).decode("utf-8", errors="ignore")
        return str(raw)

    async def _read_live_sandbox_text(self, path: str) -> str | None:
        try:
            raw = await self._run_blocking_sandbox_call(
                "files.read",
                lambda: self.sandbox.files.read(path),
            )
        except Exception:
            return None
        if isinstance(raw, (bytes, bytearray)):
            return bytes(raw).decode("utf-8", errors="ignore")
        return str(raw)

    async def _workspace_rewrite_block_payload(
        self,
        *,
        attempted_path: str,
        content: str,
    ) -> dict[str, Any] | None:
        suffix = posixpath.splitext(attempted_path)[1].lower()
        if suffix not in _REWRITE_GUARD_EXTENSIONS:
            return None

        existing_content = await self._read_existing_workspace_text(attempted_path)
        if existing_content is not None:
            if _looks_like_generated_report_deliverable(
                attempted_path,
                content,
                existing_content,
            ):
                return None
            return {
                "target_path": attempted_path,
                "reason": "existing_path",
                "error": _rewrite_block_message(
                    attempted_path=attempted_path,
                    target_path=attempted_path,
                ),
            }

        directory = posixpath.dirname(attempted_path) or self.workspace_path
        try:
            entries = await self._run_blocking_sandbox_call(
                "files.list",
                lambda: self.sandbox.files.list(directory),
            )
        except Exception:
            entries = []

        for entry in entries or []:
            if isinstance(entry, dict):
                raw_name = entry.get("name") or entry.get("path") or ""
                raw_path = entry.get("path") or raw_name
            else:
                raw_name = getattr(entry, "name", "") or str(entry)
                raw_path = getattr(entry, "path", "") or raw_name
            candidate_path = str(raw_path or raw_name or "")
            if not candidate_path:
                continue
            if not candidate_path.startswith("/"):
                candidate_path = posixpath.join(directory, candidate_path)
            candidate_path = self._normalize_path(candidate_path)
            if candidate_path == attempted_path:
                continue
            if posixpath.splitext(candidate_path)[1].lower() != suffix:
                continue
            candidate_content = await self._read_existing_workspace_text(candidate_path)
            if candidate_content is None:
                continue
            if _looks_like_generated_report_deliverable(
                attempted_path,
                content,
                candidate_content,
            ):
                continue
            if _looks_like_same_file_rewrite(candidate_content, content):
                return {
                    "target_path": candidate_path,
                    "reason": "renamed_same_content",
                    "error": _rewrite_block_message(
                        attempted_path=attempted_path,
                        target_path=candidate_path,
                    ),
                }
        return None

    def _build_read_file_result_from_bytes(
        self,
        *,
        path: str,
        data: bytes,
        max_bytes: int,
        offset: int,
        line_start: int | None = None,
        line_end: int | None = None,
    ) -> ToolResult:
        total_bytes = len(data)
        sliced = data[offset : offset + max_bytes]
        decoded_text = sliced.decode("utf-8", errors="replace")
        truncated = (offset + len(sliced)) < total_bytes

        if _looks_like_binary_content(sliced, decoded_text):
            message = (
                "Binary file detected. Text preview is omitted to avoid garbled output."
            )
            return ToolResult(
                success=True,
                output={
                    "path": path,
                    "offset": offset,
                    "bytes_read": len(sliced),
                    "max_bytes": max_bytes,
                    "total_bytes": total_bytes,
                    "truncated": truncated,
                    "is_binary": True,
                    "message": message,
                    "suggested_action": (
                        "Use download_file for binary assets, or rely on native multimodal vision "
                        "for uploaded image understanding."
                    ),
                    "content": "",
                    "output": message,
                    **(
                        {
                            "line_start": line_start,
                            "line_end": line_end,
                            "offset_mode": "line_range",
                        }
                        if line_start is not None
                        else {}
                    ),
                },
            )

        content = sliced.decode("utf-8", errors="ignore")
        workflow_hint = self._read_file_document_workflow_hint(
            path=path,
            total_bytes=total_bytes,
            line_start=line_start,
        )
        displayed_content = content + workflow_hint
        line_number_start = line_start or 1
        return ToolResult(
            success=True,
            output={
                "path": path,
                "offset": offset,
                "bytes_read": len(content.encode("utf-8", errors="ignore")),
                "max_bytes": max_bytes,
                "total_bytes": total_bytes,
                "truncated": truncated,
                "is_binary": False,
                "content": content,
                "output": displayed_content,
                **(
                    {
                        "line_start": line_start,
                        "line_end": line_end,
                        "offset_mode": "line_range",
                    }
                    if line_start is not None
                    else {}
                ),
                **(
                    {
                        "line_numbered_content": _add_line_numbers(
                            content,
                            start_line=line_number_start,
                        ),
                    }
                    if READ_FILE_LINE_NUMBERS
                    else {}
                ),
            },
        )

    def _read_file_document_workflow_hint(
        self,
        *,
        path: str,
        total_bytes: int | None,
        line_start: int | None,
    ) -> str:
        if not total_bytes or total_bytes < 20_000:
            return ""
        suffix = posixpath.splitext(str(path).lower())[1]
        if suffix not in {".txt", ".md", ".csv", ".json", ".jsonl"}:
            return ""
        basename = posixpath.basename(str(path).lower())
        intermediate_markers = (
            "extract",
            "extracted",
            "question",
            "questions",
            "raw",
            "full",
            "text",
        )
        if not any(marker in basename for marker in intermediate_markers):
            return ""
        key = f"large_document_read:{self._normalize_path(path)}"
        read_count = self._large_document_read_counts.get(key, 0) + 1
        self._large_document_read_counts[key] = read_count
        if read_count >= 3:
            return (
                "\n\n[Document workflow reminder: this looks like a large intermediate "
                "extraction file and you have already previewed it multiple times. "
                "STOP reading this file chunk by chunk. Use a targeted search/extraction "
                "for one missing fact if necessary; otherwise write/verify the requested "
                "deliverable now and stop.]\n"
            )
        return (
            "\n\n[Document workflow reminder: this looks like a large intermediate "
            "extraction file. For report/analysis tasks, do not read every chunk. "
            "After sampling targeted sections and identifying the main patterns, "
            "write the requested deliverable directly and verify it.]\n"
        )

    async def execute_command(
        self,
        command: str,
        workdir: str = "",
        timeout: int = 1800,
        envs: Optional[Dict[str, str]] = None,
        session_name: Optional[str] = None,
        blocking: bool = True,
    ) -> ToolResult:
        """Run a shell command inside the code sandbox."""
        cmd_str = str(command).strip()
        normalized_workdir = self.workspace_path
        status_arguments: Dict[str, Any] = {
            "command": cmd_str,
            "workdir": normalized_workdir,
            "timeout": timeout,
            "session_name": session_name,
            "blocking": blocking,
        }
        try:
            await self._ensure_sandbox()
            dependency_install_block = _document_dependency_install_block_payload(
                cmd_str,
            )
            if dependency_install_block is not None:
                error_message = dependency_install_block["error"]
                await self._emit_tool_status(
                    "tool_rejected_dependency_install",
                    "execute_command",
                    arguments=status_arguments,
                    message=error_message,
                    extra={
                        "error_code": dependency_install_block["error_code"],
                        "reason": "document_dependency_install_redirect",
                    },
                )
                logger.warning(
                    "execute_command rejected before sandbox call",
                    trace_source_stage="sandbox_code_tool",
                    trace_exec_gate_reason="document_dependency_install_redirect",
                    command=cmd_str,
                )
                return ToolResult(
                    success=False,
                    output={
                        **dependency_install_block,
                        "output": error_message,
                    },
                )
            if _looks_like_document_inspection_command(cmd_str):
                self._document_inspection_command_count += 1
                if (
                    self._document_inspection_command_count
                    > DOCUMENT_INSPECTION_REDIRECT_AFTER
                ):
                    error_message = _document_inspection_loop_redirect_message(
                        self._document_inspection_command_count,
                    )
                    await self._emit_tool_status(
                        "tool_rejected_document_loop",
                        "execute_command",
                        arguments=status_arguments,
                        message=error_message,
                        extra={
                            "error_code": "DOCUMENT_INSPECTION_LOOP_REDIRECT",
                            "reason": "document_inspection_loop_redirect",
                        },
                    )
                    logger.warning(
                        "execute_command rejected before sandbox call",
                        trace_source_stage="sandbox_code_tool",
                        trace_exec_gate_reason="document_inspection_loop_redirect",
                        command_count=self._document_inspection_command_count,
                    )
                    return ToolResult(
                        success=False,
                        output={
                            "error_code": "DOCUMENT_INSPECTION_LOOP_REDIRECT",
                            "error": error_message,
                            "output": error_message,
                            "command": cmd_str,
                            "inspection_command_count": (
                                self._document_inspection_command_count
                            ),
                        },
                    )
            helper_execution_path = _extract_intermediate_helper_execution_path(
                cmd_str,
            )
            if helper_execution_path:
                execution_count = (
                    self._intermediate_helper_execution_counts.get(
                        helper_execution_path,
                        0,
                    )
                    + 1
                )
                self._intermediate_helper_execution_counts[helper_execution_path] = (
                    execution_count
                )
                if execution_count > 2:
                    error_message = (
                        "You have already executed the intermediate helper "
                        f"{helper_execution_path} {execution_count} times. "
                        f"{_intermediate_helper_redirect_message(helper_execution_path)}"
                    )
                    await self._emit_tool_status(
                        "tool_rejected_intermediate_helper_loop",
                        "execute_command",
                        arguments=status_arguments,
                        message=error_message,
                        extra={
                            "error_code": "INTERMEDIATE_HELPER_EXECUTION_REDIRECT",
                            "reason": "intermediate_helper_execution_redirect",
                            "helper_path": helper_execution_path,
                        },
                    )
                    logger.warning(
                        "execute_command rejected before sandbox call",
                        trace_source_stage="sandbox_code_tool",
                        trace_exec_gate_reason=(
                            "intermediate_helper_execution_redirect"
                        ),
                        helper_path=helper_execution_path,
                        execution_count=execution_count,
                    )
                    return ToolResult(
                        success=False,
                        output={
                            "error_code": "INTERMEDIATE_HELPER_EXECUTION_REDIRECT",
                            "error": error_message,
                            "output": error_message,
                            "command": cmd_str,
                            "helper_path": helper_execution_path,
                            "execution_count": execution_count,
                        },
                    )
            command_block_payload = _helper_script_command_block_payload(cmd_str)
            if command_block_payload is not None:
                error_message = command_block_payload["error"]
                await self._emit_tool_status(
                    "tool_rejected_helper_script",
                    "execute_command",
                    arguments=status_arguments,
                    message=error_message,
                    extra={
                        "error_code": "HELPER_SCRIPT_COMMAND_BLOCKED",
                        "reason": command_block_payload["reason"],
                        "helper_path": command_block_payload["helper_path"],
                        "target_path": command_block_payload["target_path"],
                    },
                )
                logger.warning(
                    "execute_command rejected before sandbox call",
                    trace_source_stage="sandbox_code_tool",
                    trace_exec_gate_reason="helper_script_command",
                    helper_path=command_block_payload["helper_path"],
                    target_path=command_block_payload["target_path"],
                )
                return ToolResult(
                    success=False,
                    output={
                        "error_code": "HELPER_SCRIPT_COMMAND_BLOCKED",
                        "error": error_message,
                        "command": cmd_str,
                        "helper_path": command_block_payload["helper_path"],
                        "target_path": command_block_payload["target_path"],
                    },
                )
            long_script_block_payload = _long_script_command_block_payload(cmd_str)
            if long_script_block_payload is not None:
                error_message = long_script_block_payload["error"]
                await self._emit_tool_status(
                    "tool_rejected_long_script",
                    "execute_command",
                    arguments=status_arguments,
                    message=error_message,
                    extra={
                        "error_code": "LONG_SCRIPT_COMMAND_BLOCKED",
                        "reason": long_script_block_payload["reason"],
                        "target_path": long_script_block_payload["target_path"],
                    },
                )
                logger.warning(
                    "execute_command rejected before sandbox call",
                    trace_source_stage="sandbox_code_tool",
                    trace_exec_gate_reason="long_script_command",
                    target_path=long_script_block_payload["target_path"],
                )
                return ToolResult(
                    success=False,
                    output={
                        "error_code": "LONG_SCRIPT_COMMAND_BLOCKED",
                        "error": error_message,
                        "command": cmd_str,
                        "target_path": long_script_block_payload["target_path"],
                    },
                )
            delete_block_payload = _workspace_delete_rewrite_block_payload(cmd_str)
            if delete_block_payload is not None:
                error_message = delete_block_payload["error"]
                await self._emit_tool_status(
                    "tool_rejected_rewrite",
                    "execute_command",
                    arguments=status_arguments,
                    message=error_message,
                    extra={
                        "error_code": "WORKSPACE_DELETE_REWRITE_BLOCKED",
                        "reason": delete_block_payload["reason"],
                        "target_path": delete_block_payload["target_path"],
                    },
                )
                logger.warning(
                    "execute_command rejected before sandbox call",
                    trace_source_stage="sandbox_code_tool",
                    trace_exec_gate_reason="workspace_delete_rewrite_bypass",
                    target_path=delete_block_payload["target_path"],
                )
                return ToolResult(
                    success=False,
                    output={
                        "error_code": "WORKSPACE_DELETE_REWRITE_BLOCKED",
                        "error": error_message,
                        "command": cmd_str,
                        "target_path": delete_block_payload["target_path"],
                    },
                )

            noop_block_payload = _noop_in_place_edit_block_payload(cmd_str)
            if noop_block_payload is not None:
                error_message = noop_block_payload["error"]
                await self._emit_tool_status(
                    "tool_rejected_rewrite",
                    "execute_command",
                    arguments=status_arguments,
                    message=error_message,
                    extra={
                        "error_code": "NOOP_IN_PLACE_EDIT_BLOCKED",
                        "reason": noop_block_payload["reason"],
                        "target_path": noop_block_payload["target_path"],
                    },
                )
                logger.warning(
                    "execute_command rejected before sandbox call",
                    trace_source_stage="sandbox_code_tool",
                    trace_exec_gate_reason="noop_in_place_edit",
                    target_path=noop_block_payload["target_path"],
                )
                return ToolResult(
                    success=False,
                    output={
                        "error_code": "NOOP_IN_PLACE_EDIT_BLOCKED",
                        "error": error_message,
                        "command": cmd_str,
                        "target_path": noop_block_payload["target_path"],
                    },
                )

            command_envs = _build_command_envs(envs)

            # Always operate inside the workspace for consistency with system prompt
            normalized_workdir = (
                self._normalize_path(workdir) if workdir else self.workspace_path
            )
            status_arguments["workdir"] = normalized_workdir
            stripped_cmd = cmd_str.lstrip()
            if re.match(
                r"^cd\s+(?:/workspace|['\"]/workspace['\"])(?:\s|;|&&|$)", stripped_cmd
            ):
                full_command = cmd_str
            else:
                full_command = f"cd {shlex.quote(normalized_workdir)} && {cmd_str}"
            run_kwargs: Dict[str, Any] = {"timeout": timeout}
            if command_envs is not None:
                run_kwargs["envs"] = command_envs

            await self._emit_tool_status(
                "tool_started",
                "execute_command",
                arguments=status_arguments,
            )
            result = await self._run_blocking_sandbox_call(
                "commands.run",
                lambda: self.sandbox.commands.run(full_command, **run_kwargs),
                timeout_seconds=max(float(timeout) + 5.0, 10.0),
            )

            exit_code = getattr(result, "exit_code", 0)
            stdout = _truncate_head_tail(getattr(result, "stdout", ""))
            stderr = _truncate_head_tail(getattr(result, "stderr", ""))
            combined_output = stdout
            if stderr:
                combined_output = (
                    f"{stdout}\nSTDERR:\n{stderr}" if stdout else f"STDERR:\n{stderr}"
                )

            payload = {
                "command": cmd_str,
                "workdir": normalized_workdir,
                "session_name": session_name,
                "blocking": blocking,
                "stdout": stdout,
                "stderr": stderr,
                "exit_code": exit_code,
                "output": combined_output,
                "completed": True,
            }
            syntax_redirect_hint = _python_exploratory_syntax_redirect_hint(
                cmd_str,
                stderr,
            )
            if exit_code != 0 and syntax_redirect_hint:
                payload["error_code"] = "PYTHON_EXPLORATORY_SYNTAX_REDIRECT"
                payload["recovery_hint"] = syntax_redirect_hint
                payload["output"] = (
                    f"{combined_output}\n\n[Recovery hint: {syntax_redirect_hint}]"
                    if combined_output
                    else f"[Recovery hint: {syntax_redirect_hint}]"
                )
            dependency_hint = _pdf_dependency_recovery_hint(cmd_str, stderr)
            if exit_code != 0 and dependency_hint:
                payload["error_code"] = "PDF_EXTRACTION_DEPENDENCY_REDIRECT"
                payload["recovery_hint"] = dependency_hint
                payload["output"] = (
                    f"{payload['output']}\n\n[Recovery hint: {dependency_hint}]"
                    if payload.get("output")
                    else f"[Recovery hint: {dependency_hint}]"
                )
            tokenizer_loss_hint = _tokenizer_loss_recovery_hint(cmd_str, stderr)
            if exit_code != 0 and tokenizer_loss_hint:
                payload["error_code"] = "TOKENIZER_LOSS_SUSPECTED"
                payload["recovery_hint"] = tokenizer_loss_hint
                payload["output"] = (
                    f"{payload['output']}\n\n[Recovery hint: {tokenizer_loss_hint}]"
                    if payload.get("output")
                    else f"[Recovery hint: {tokenizer_loss_hint}]"
                )
            await self._sync_workspace_artifacts_from_sandbox(
                source="sandbox_code_tool.execute_command",
            )

            await self._emit_tool_status(
                "tool_completed" if exit_code == 0 else "tool_failed",
                "execute_command",
                arguments=status_arguments,
                extra={"exit_code": exit_code},
            )
            return ToolResult(success=exit_code == 0, output=payload)
        except Exception as e:
            logger.warning(f"execute_command failed: {e}")
            await self._emit_tool_status(
                "tool_failed",
                "execute_command",
                arguments=status_arguments,
                message=str(e),
            )
            return ToolResult(
                success=False, output={"error": str(e), "command": command}
            )

    async def run_code(
        self,
        code: str,
        language: str = "python",
        envs: Optional[Dict[str, str]] = None,
    ) -> ToolResult:
        """Execute code via sandbox.run_code (python/ts/js/r/java/bash)."""
        try:
            await self._ensure_sandbox()
            run_envs = _build_command_envs(envs)
            run_kwargs: Dict[str, Any] = {"language": language}
            if run_envs is not None:
                run_kwargs["envs"] = run_envs
            result = await self._run_blocking_sandbox_call(
                "run_code",
                lambda: self.sandbox.run_code(code, **run_kwargs),
            )
            payload = {
                "language": language,
                "logs": _truncate(getattr(result, "logs", "")),
                "result": _truncate(str(getattr(result, "result", ""))),
                "error": getattr(result, "error", "") or None,
            }
            payload["output"] = payload["logs"] or payload["result"] or ""
            success = payload["error"] in (None, "", 0)
            return ToolResult(success=success, output=payload)
        except Exception as e:
            logger.warning(f"run_code failed: {e}")
            return ToolResult(success=False, output={"error": str(e)})

    async def read_file(
        self,
        path: str,
        max_bytes: Optional[int] = None,
        offset: int = 0,
        line_start: Optional[int] = None,
        line_end: Optional[int] = None,
    ) -> ToolResult:
        """Read a file from /workspace with optional size limits."""
        clean = path if isinstance(path, str) else str(path or "")
        try:
            await self._ensure_sandbox()
            clean = self._normalize_path(path)
            effective_max = READ_FILE_MAX_BYTES
            if _looks_like_document_inspection_path(clean):
                self._document_inspection_command_count += 1
                if (
                    self._document_inspection_command_count
                    > DOCUMENT_INSPECTION_REDIRECT_AFTER
                ):
                    error_message = _document_inspection_loop_redirect_message(
                        self._document_inspection_command_count
                    )
                    return ToolResult(
                        success=False,
                        output={
                            "error_code": "DOCUMENT_INSPECTION_LOOP_REDIRECT",
                            "error": error_message,
                            "output": error_message,
                            "path": clean,
                            "inspection_command_count": (
                                self._document_inspection_command_count
                            ),
                        },
                    )
            if max_bytes is not None:
                try:
                    requested = int(max_bytes)
                    if requested > 0:
                        effective_max = min(requested, READ_FILE_MAX_BYTES)
                except (TypeError, ValueError):
                    pass

            if offset < 0:
                offset = 0
            start_line = _coerce_positive_int(line_start)
            end_line = _coerce_positive_int(line_end)
            if start_line is not None and end_line is None:
                end_line = start_line
            if (
                start_line is not None
                and end_line is not None
                and end_line < start_line
            ):
                end_line = start_line

            file_size = None
            try:
                stat_cmd = f"stat -c %s {shlex.quote(clean)}"
                stat_result = await self._run_blocking_sandbox_call(
                    "commands.run",
                    lambda: self.sandbox.commands.run(stat_cmd),
                )
                if getattr(stat_result, "exit_code", 0) == 0:
                    stdout = getattr(stat_result, "stdout", "").strip()
                    if stdout.isdigit():
                        file_size = int(stdout)
            except Exception:
                file_size = None

            if start_line is not None:
                read_cmd = (
                    f"sed -n '{start_line},{end_line}p' {shlex.quote(clean)} "
                    f"| head -c {effective_max}"
                )
            elif offset == 0:
                read_cmd = f"head -c {effective_max} {shlex.quote(clean)}"
            else:
                read_cmd = (
                    f"dd if={shlex.quote(clean)} bs=1 skip={offset} "
                    f"count={effective_max} 2>/dev/null"
                )

            read_result = await self._run_blocking_sandbox_call(
                "commands.run",
                lambda: self.sandbox.commands.run(read_cmd),
            )
            if getattr(read_result, "exit_code", 0) != 0:
                stderr = _truncate(getattr(read_result, "stderr", ""))
                try:
                    raw_file = await self._run_blocking_sandbox_call(
                        "files.read",
                        lambda: self.sandbox.files.read(clean),
                    )
                    raw_bytes = (
                        bytes(raw_file)
                        if isinstance(raw_file, (bytes, bytearray))
                        else str(raw_file).encode("utf-8", errors="replace")
                    )
                    self._clear_path_failure(tool_name="read_file", path=clean)
                    return self._build_read_file_result_from_bytes(
                        path=clean,
                        data=raw_bytes,
                        max_bytes=effective_max,
                        offset=offset,
                        line_start=start_line,
                        line_end=end_line,
                    )
                except Exception:
                    pass
                artifact_bytes = await self._read_workspace_artifact_bytes(clean)
                if artifact_bytes is not None:
                    self._clear_path_failure(tool_name="read_file", path=clean)
                    return self._build_read_file_result_from_bytes(
                        path=clean,
                        data=artifact_bytes,
                        max_bytes=effective_max,
                        offset=offset,
                        line_start=start_line,
                        line_end=end_line,
                    )
                failure_count = self._record_path_failure(
                    tool_name="read_file",
                    path=clean,
                )
                if failure_count >= 3:
                    correction = await self._find_unique_path_auto_correction(clean)
                    if correction and not correction.get("ambiguous"):
                        corrected_path = str(correction["path"])
                        corrected = await self.read_file(
                            path=corrected_path,
                            max_bytes=effective_max,
                            offset=offset,
                            line_start=start_line,
                            line_end=end_line,
                        )
                        if corrected.success:
                            self._clear_path_failure(tool_name="read_file", path=clean)
                            return self._annotate_auto_corrected_output(
                                corrected,
                                original_path=clean,
                                corrected_path=corrected_path,
                                distance=int(correction["distance"]),
                            )
                    if correction and correction.get("ambiguous"):
                        return ToolResult(
                            success=False,
                            output={
                                "error": stderr or "Failed to read file",
                                "path": clean,
                                "auto_correct_candidates": correction.get(
                                    "candidates", []
                                ),
                                "auto_corrected_path": False,
                            },
                        )
                return ToolResult(
                    success=False,
                    output={"error": stderr or "Failed to read file", "path": clean},
                )

            raw = getattr(read_result, "stdout", "")
            if isinstance(raw, (bytes, bytearray)):
                raw_bytes = bytes(raw)
            else:
                raw_text = str(raw)
                raw_bytes = raw_text.encode("utf-8", errors="replace")

            decoded_text = raw_bytes.decode("utf-8", errors="replace")
            if _looks_like_binary_content(raw_bytes, decoded_text):
                bytes_read = len(raw_bytes)
                truncated = False
                if file_size is not None:
                    truncated = (offset + bytes_read) < file_size

                message = "Binary file detected. Text preview is omitted to avoid garbled output."
                return ToolResult(
                    success=True,
                    output={
                        "path": clean,
                        "offset": offset,
                        "bytes_read": bytes_read,
                        "max_bytes": effective_max,
                        "total_bytes": file_size,
                        "truncated": truncated,
                        "is_binary": True,
                        "message": message,
                        "suggested_action": (
                            "Use download_file for binary assets, or rely on native multimodal vision "
                            "for uploaded image understanding."
                        ),
                        "content": "",
                        "output": message,
                        **(
                            {
                                "line_start": start_line,
                                "line_end": end_line,
                                "offset_mode": "line_range",
                            }
                            if start_line is not None
                            else {}
                        ),
                    },
                )

            if isinstance(raw, (bytes, bytearray)):
                content = raw_bytes.decode("utf-8", errors="ignore")
            else:
                content = raw_text
                raw_bytes = content.encode("utf-8", errors="ignore")
            self._clear_path_failure(tool_name="read_file", path=clean)

            bytes_read = len(raw_bytes)
            truncated = False
            if file_size is not None:
                truncated = (offset + bytes_read) < file_size
            line_number_start = start_line or 1
            workflow_hint = self._read_file_document_workflow_hint(
                path=clean,
                total_bytes=file_size,
                line_start=start_line,
            )
            displayed_content = content + workflow_hint

            return ToolResult(
                success=True,
                output={
                    "path": clean,
                    "offset": offset,
                    "bytes_read": bytes_read,
                    "max_bytes": effective_max,
                    "total_bytes": file_size,
                    "truncated": truncated,
                    "is_binary": False,
                    "content": content,
                    "output": displayed_content,
                    **(
                        {
                            "line_start": start_line,
                            "line_end": end_line,
                            "offset_mode": "line_range",
                        }
                        if start_line is not None
                        else {}
                    ),
                    **(
                        {
                            "line_numbered_content": _add_line_numbers(
                                content,
                                start_line=line_number_start,
                            ),
                        }
                        if READ_FILE_LINE_NUMBERS
                        else {}
                    ),
                },
            )
        except Exception as e:
            artifact_bytes = await self._read_workspace_artifact_bytes(clean)
            if artifact_bytes is not None:
                self._clear_path_failure(tool_name="read_file", path=clean)
                return self._build_read_file_result_from_bytes(
                    path=clean,
                    data=artifact_bytes,
                    max_bytes=(
                        effective_max
                        if "effective_max" in locals()
                        else READ_FILE_MAX_BYTES
                    ),
                    offset=offset if "offset" in locals() else 0,
                    line_start=start_line if "start_line" in locals() else None,
                    line_end=end_line if "end_line" in locals() else None,
                )
            if "clean" in locals():
                failure_count = self._record_path_failure(
                    tool_name="read_file",
                    path=clean,
                )
                if failure_count >= 3:
                    correction = await self._find_unique_path_auto_correction(clean)
                    if correction and not correction.get("ambiguous"):
                        corrected_path = str(correction["path"])
                        corrected = await self.read_file(
                            path=corrected_path,
                            max_bytes=(
                                effective_max
                                if "effective_max" in locals()
                                else READ_FILE_MAX_BYTES
                            ),
                            offset=offset if "offset" in locals() else 0,
                            line_start=start_line if "start_line" in locals() else None,
                            line_end=end_line if "end_line" in locals() else None,
                        )
                        if corrected.success:
                            self._clear_path_failure(tool_name="read_file", path=clean)
                            return self._annotate_auto_corrected_output(
                                corrected,
                                original_path=clean,
                                corrected_path=corrected_path,
                                distance=int(correction["distance"]),
                            )
                    if correction and correction.get("ambiguous"):
                        return ToolResult(
                            success=False,
                            output={
                                "error": str(e),
                                "path": clean,
                                "auto_correct_candidates": correction.get(
                                    "candidates", []
                                ),
                                "auto_corrected_path": False,
                            },
                        )
            logger.warning(f"read_file failed: {e}")
            return ToolResult(success=False, output={"error": str(e), "path": path})

    async def write_file(self, path: str, content: str) -> ToolResult:
        """Write text content to a file under /workspace."""
        clean = path if isinstance(path, str) else str(path or "")
        try:
            raw_path = path.strip() if isinstance(path, str) else ""
            if not raw_path:
                gate_message = "write_file requires a non-empty path."
                await self._emit_write_file_status(
                    "tool_failed",
                    "<missing_path>",
                    message=gate_message,
                    extra={
                        "exec_gate": "rejected",
                        "exec_gate_reason": "missing_path",
                        "content_bytes": 0,
                        "error_code": "WRITE_FILE_INCOMPLETE_PAYLOAD",
                    },
                )
                logger.warning(
                    "write_file execution rejected before sandbox call",
                    trace_source_stage="sandbox_code_tool",
                    trace_exec_gate_reason="missing_path",
                )
                return ToolResult(
                    success=False,
                    output={
                        "error_code": "WRITE_FILE_INCOMPLETE_PAYLOAD",
                        "error": gate_message,
                        "path": raw_path,
                        "content_bytes": 0,
                    },
                )

            if not isinstance(content, str):
                gate_message = (
                    f"write_file requires string content, got {type(content).__name__}."
                )
                await self._emit_write_file_status(
                    "tool_failed",
                    raw_path,
                    message=gate_message,
                    extra={
                        "exec_gate": "rejected",
                        "exec_gate_reason": "content_not_string",
                        "content_bytes": 0,
                        "error_code": "WRITE_FILE_INCOMPLETE_PAYLOAD",
                    },
                )
                logger.warning(
                    "write_file execution rejected before sandbox call",
                    trace_source_stage="sandbox_code_tool",
                    trace_exec_gate_reason="content_not_string",
                    path=raw_path,
                )
                return ToolResult(
                    success=False,
                    output={
                        "error_code": "WRITE_FILE_INCOMPLETE_PAYLOAD",
                        "error": gate_message,
                        "path": raw_path,
                        "content_bytes": 0,
                    },
                )

            content_text = content
            content_bytes = len(content_text.encode("utf-8"))
            if content_bytes <= 0:
                gate_message = "write_file requires non-empty content."
                await self._emit_write_file_status(
                    "tool_failed",
                    raw_path,
                    message=gate_message,
                    extra={
                        "exec_gate": "rejected",
                        "exec_gate_reason": "empty_content",
                        "content_bytes": 0,
                        "error_code": "WRITE_FILE_INCOMPLETE_PAYLOAD",
                    },
                )
                logger.warning(
                    "write_file execution rejected before sandbox call",
                    trace_source_stage="sandbox_code_tool",
                    trace_exec_gate_reason="empty_content",
                    path=raw_path,
                )
                return ToolResult(
                    success=False,
                    output={
                        "error_code": "WRITE_FILE_INCOMPLETE_PAYLOAD",
                        "error": gate_message,
                        "path": raw_path,
                        "content_bytes": 0,
                    },
                )

            preflight_clean = self._normalize_path(raw_path)
            original_path_exists = (
                await self._read_existing_workspace_text(preflight_clean)
            ) is not None
            correction = (
                await self._find_unique_path_auto_correction(preflight_clean)
                if not original_path_exists
                and self._path_failure_count(preflight_clean) >= 2
                else None
            )
            if correction and not correction.get("ambiguous"):
                corrected_path = str(correction["path"])
                preflight_clean = corrected_path
            fix_script_hint = _detect_fix_script_anti_pattern(
                preflight_clean,
                content_text,
            )
            if fix_script_hint:
                await self._emit_write_file_status(
                    "tool_rejected_fix_script",
                    preflight_clean,
                    extra={
                        "reason": "FIX_SCRIPT_ANTI_PATTERN",
                        "message": fix_script_hint,
                    },
                )
                logger.warning(
                    "write_file execution rejected before sandbox call",
                    trace_source_stage="sandbox_code_tool",
                    trace_exec_gate_reason="fix_script_anti_pattern",
                    path=preflight_clean,
                )
                return ToolResult(
                    success=False,
                    output={
                        "path": preflight_clean,
                        "error": fix_script_hint,
                        "error_code": "FIX_SCRIPT_BLOCKED",
                    },
                )

            if (
                _looks_like_intermediate_helper_path(preflight_clean)
                and posixpath.splitext(preflight_clean)[1].lower()
                in {".py", ".sh", ".bash", ".sed"}
                and not original_path_exists
                and self._intermediate_helper_write_count >= 1
            ):
                error_message = (
                    "You already created an intermediate extraction/helper file in this run. "
                    f"{_intermediate_helper_redirect_message(preflight_clean)}"
                )
                await self._emit_write_file_status(
                    "tool_rejected_intermediate_helper",
                    preflight_clean,
                    message=error_message,
                    extra={
                        "error_code": "INTERMEDIATE_HELPER_CREATION_REDIRECT",
                        "exec_gate_reason": "intermediate_helper_creation_redirect",
                    },
                )
                return ToolResult(
                    success=False,
                    output={
                        "path": preflight_clean,
                        "error_code": "INTERMEDIATE_HELPER_CREATION_REDIRECT",
                        "error": error_message,
                        "output": error_message,
                    },
                )

            await self._ensure_sandbox()
            clean = preflight_clean
            existing_before_write = await self._read_existing_workspace_text(clean)
            rewrite_block_payload = await self._workspace_rewrite_block_payload(
                attempted_path=clean,
                content=content_text,
            )
            if rewrite_block_payload is not None:
                error_message = str(rewrite_block_payload["error"])
                await self._emit_write_file_status(
                    "tool_rejected_rewrite",
                    clean,
                    message=error_message,
                    extra={
                        "reason": rewrite_block_payload["reason"],
                        "target_path": rewrite_block_payload["target_path"],
                        "error_code": "WORKSPACE_REWRITE_BLOCKED",
                    },
                )
                logger.warning(
                    "write_file execution rejected before sandbox call",
                    trace_source_stage="sandbox_code_tool",
                    trace_exec_gate_reason="workspace_rewrite_blocked",
                    path=clean,
                    target_path=rewrite_block_payload["target_path"],
                    reason=rewrite_block_payload["reason"],
                )
                return ToolResult(
                    success=False,
                    output={
                        "path": clean,
                        "rewrite_target_path": rewrite_block_payload["target_path"],
                        "rewrite_block_reason": rewrite_block_payload["reason"],
                        "error": error_message,
                        "error_code": "WORKSPACE_REWRITE_BLOCKED",
                    },
                )
            logger.info(
                "write_file execution entered",
                trace_source_stage="sandbox_code_tool",
                trace_exec_gate_reason="tool_invoked",
                path=clean,
                content_bytes=content_bytes,
                trace_args_hash=hashlib.sha1(
                    f"{clean}\n{content_text}".encode("utf-8"),
                ).hexdigest()[:12],
            )
            await self._emit_write_file_status("tool_started", clean)
            await self._stream_write_progress(clean, content)
            processed_content = content
            tokenizer_loss_repairs: list[str] = []
            if posixpath.splitext(clean)[1].lower() == ".html":
                processed_content, tokenizer_loss_repairs = (
                    _repair_html_script_token_loss(
                        processed_content,
                    )
                )
            validation_payload: Optional[Dict[str, Any]] = None

            if self._should_apply_code_guard(clean, processed_content):
                validation_payload = await self._prepare_python_write_validation(
                    clean,
                    processed_content,
                )
                processed_content = validation_payload.get(
                    "processed_content",
                    processed_content,
                )
                if not validation_payload.get("syntax_ok", False):
                    failure_count = self._record_python_syntax_failure(clean)
                    safe_validation_payload = _public_validation_payload(
                        validation_payload,
                    )
                    syntax_error = validation_payload.get("error") or {}
                    error_message = _format_syntax_error_for_user(
                        clean,
                        syntax_error,
                    )
                    error_message = (
                        f"{error_message}. File was not written. Fix the script "
                        "content and call write_file again for this new file, or "
                        "use edit_file only after a valid file exists."
                    )
                    forced_template = failure_count >= 3 and (
                        "from docx" in content_text
                        or "import docx" in content_text
                        or "Document(" in content_text
                    )
                    if forced_template:
                        error_message = (
                            f"{error_message} Use this minimal DOCX template instead. "
                            f"{TOKENIZER_SENSITIVE_CODE_ADVICE}"
                        )
                    else:
                        error_message = (
                            f"{error_message} {TOKENIZER_SENSITIVE_CODE_ADVICE}"
                        )
                    error_message = (
                        f"{error_message} "
                        f"{_python_syntax_failure_recovery_advice(clean, content_text)}"
                    )
                    error_code = "PYTHON_SYNTAX_VALIDATION_FAILED"
                    if _looks_like_intermediate_helper_path(clean):
                        error_code = "INTERMEDIATE_HELPER_SYNTAX_REDIRECT"
                        error_message = (
                            f"{error_message} This file name looks like an "
                            "intermediate extraction/helper artifact. Do not spend "
                            "more turns repairing this helper unless the helper "
                            "itself is the requested deliverable. Use one targeted "
                            "read/search command for any missing fact, or write the "
                            "requested Markdown/HTML deliverable directly."
                        )
                        self._intermediate_helper_write_count += 1
                    await self._emit_guard_stage(
                        clean,
                        "failed",
                        extra={"pre_write_validation": safe_validation_payload},
                    )
                    await self._emit_write_file_status(
                        "tool_failed",
                        clean,
                        message=error_message,
                        extra={"pre_write_validation": safe_validation_payload},
                    )
                    return ToolResult(
                        success=False,
                        output={
                            "path": clean,
                            "error_code": error_code,
                            "error": error_message,
                            "post_write_validation": safe_validation_payload,
                            "output": error_message,
                            "syntax_failure_count": failure_count,
                            "forced_minimal_docx_template": forced_template,
                            **(
                                {"minimal_docx_template": MINIMAL_DOCX_TEMPLATE}
                                if forced_template
                                else {}
                            ),
                        },
                    )

            if validation_payload:
                await self._emit_guard_stage(clean, "write")
            await self._run_blocking_sandbox_call(
                "files.write",
                lambda: self.sandbox.files.write(clean, processed_content),
            )

            if await self._read_live_sandbox_text(clean) is None:
                error_message = (
                    f"write_file wrote {clean}, but the file is not visible in the "
                    "live sandbox filesystem used by execute_command. Treat this "
                    "write as failed; do not run grep, sed, cat, or python on this "
                    f"path yet. {TOKENIZER_SENSITIVE_CODE_ADVICE}"
                )
                await self._emit_write_file_status(
                    "tool_failed",
                    clean,
                    message=error_message,
                    extra={
                        "error_code": "SANDBOX_FILE_NOT_VISIBLE",
                        "exec_gate_reason": "sandbox_file_not_visible",
                    },
                )
                return ToolResult(
                    success=False,
                    output={
                        "path": clean,
                        "error_code": "SANDBOX_FILE_NOT_VISIBLE",
                        "error": error_message,
                        "output": error_message,
                    },
                )

            if validation_payload:
                validation_payload = await self._finalize_python_write_validation(
                    clean,
                    validation_payload,
                )
                safe_validation_payload = _public_validation_payload(validation_payload)
                if not validation_payload.get("syntax_ok", False):
                    failure_count = self._record_python_syntax_failure(clean)
                    syntax_error = validation_payload.get("error") or {}
                    error_message = _format_syntax_error_for_user(clean, syntax_error)
                    error_message = (
                        f"{error_message}. {TOKENIZER_SENSITIVE_CODE_ADVICE}"
                    )
                    error_message = (
                        f"{error_message} "
                        f"{_python_syntax_failure_recovery_advice(clean, updated_content)}"
                    )
                    await self._emit_guard_stage(
                        clean,
                        "failed",
                        extra={"post_write_validation": safe_validation_payload},
                    )
                    await self._emit_write_file_status(
                        "tool_failed",
                        clean,
                        message=error_message,
                        extra={"post_write_validation": safe_validation_payload},
                    )
                    return ToolResult(
                        success=False,
                        output={
                            "path": clean,
                            "error_code": "PYTHON_SYNTAX_VALIDATION_FAILED",
                            "error": error_message,
                            "post_write_validation": safe_validation_payload,
                            "output": error_message,
                            "syntax_failure_count": failure_count,
                        },
                    )
            else:
                safe_validation_payload = None

            content_bytes = len(processed_content.encode())
            if validation_payload and isinstance(
                validation_payload.get("final_bytes"), int
            ):
                content_bytes = int(validation_payload["final_bytes"])
                await self._emit_guard_stage(
                    clean,
                    "done",
                    extra={"post_write_validation": safe_validation_payload},
                )

            completion_extra: Dict[str, Any] = {"bytes": content_bytes}
            if safe_validation_payload:
                completion_extra["post_write_validation"] = safe_validation_payload
            await self._emit_write_file_status(
                "tool_completed",
                clean,
                extra=completion_extra,
            )
            logger.info(
                "write_file execution completed",
                trace_source_stage="sandbox_code_tool",
                path=clean,
                bytes=content_bytes,
            )
            output_payload: Dict[str, Any] = {
                "path": clean,
                "bytes": content_bytes,
                "output": f"Written {content_bytes} bytes",
            }
            if correction and not correction.get("ambiguous"):
                output_payload["auto_corrected_path"] = True
                output_payload["original_path"] = self._normalize_path(raw_path)
                output_payload["corrected_path"] = clean
                output_payload["auto_correct_distance"] = int(correction["distance"])
                output_payload["output"] = (
                    f"[Auto-corrected] Path '{output_payload['original_path']}' not found "
                    f"\u2192 corrected to '{clean}' ({int(correction['distance'])} "
                    "character difference). Original tool result follows:\n\n"
                    + output_payload["output"]
                )
            if safe_validation_payload:
                output_payload["line_count"] = safe_validation_payload.get("line_count")
                output_payload["post_write_validation"] = safe_validation_payload
            if tokenizer_loss_repairs:
                output_payload["tokenizer_loss_repairs"] = tokenizer_loss_repairs
            await self._persist_workspace_artifact(
                clean,
                processed_content,
                source="sandbox_code_tool.write_file",
                content_type="text/plain; charset=utf-8",
            )
            if _looks_like_intermediate_helper_path(clean) and posixpath.splitext(
                clean
            )[1].lower() in {".py", ".sh", ".bash", ".sed"}:
                self._intermediate_helper_write_count += 1
            self._clear_path_failure(tool_name="write_file", path=clean)
            self._clear_path_failure(
                tool_name="write_file", path=self._normalize_path(raw_path)
            )
            self._clear_python_syntax_failure(clean)
            # Append CJK font hint when relevant
            cjk_hint = _check_cjk_font_issues(content, clean)
            if cjk_hint:
                output_payload["cjk_warning"] = cjk_hint
            return ToolResult(
                success=True,
                output=output_payload,
            )
        except Exception as e:
            await self._emit_write_file_status(
                "tool_failed",
                clean,
                message=str(e),
            )
            logger.warning(
                "write_file execution failed",
                trace_source_stage="sandbox_code_tool",
                path=clean,
                error=str(e),
            )
            logger.warning(f"write_file failed: {e}")
            return ToolResult(success=False, output={"error": str(e), "path": path})

    async def edit_file(
        self,
        path: str,
        start_line: Optional[int] = None,
        end_line: Optional[int] = None,
        new_content: Optional[str] = None,
        old_text: Optional[str] = None,
        new_text: Optional[str] = None,
        expected_occurrences: int = 1,
        instructions: str = "",
    ) -> ToolResult:
        """Edit an existing file by line range or anchored text replacement."""
        original_instructions = instructions
        del instructions  # instructions are accepted for compatibility/logging

        clean = path if isinstance(path, str) else str(path or "")
        try:
            await self._ensure_sandbox()
            clean = self._normalize_path(path)

            line_mode_has_any = any(
                value is not None for value in (start_line, end_line, new_content)
            )
            anchor_mode_has_any = old_text is not None or new_text is not None
            if line_mode_has_any and anchor_mode_has_any:
                return ToolResult(
                    success=False,
                    output={
                        "error": "edit_file accepts either line-range params or anchor params, not both",
                        "path": clean,
                    },
                )
            if not line_mode_has_any and not anchor_mode_has_any:
                return ToolResult(
                    success=False,
                    output={
                        "error": "Missing edit parameters. Provide line-range or anchor replacement args.",
                        "path": clean,
                    },
                )

            try:
                raw_content = await self._run_blocking_sandbox_call(
                    "files.read",
                    lambda: self.sandbox.files.read(clean),
                )
                self._clear_path_failure(tool_name="edit_file", path=clean)
                if isinstance(raw_content, (bytes, bytearray)):
                    original_content = bytes(raw_content).decode(
                        "utf-8", errors="ignore"
                    )
                else:
                    original_content = str(raw_content)
            except Exception:
                artifact_bytes = await self._read_workspace_artifact_bytes(clean)
                if artifact_bytes is None:
                    failure_count = self._record_path_failure(
                        tool_name="edit_file",
                        path=clean,
                    )
                    if failure_count >= 3:
                        correction = await self._find_unique_path_auto_correction(clean)
                        if correction and not correction.get("ambiguous"):
                            corrected_path = str(correction["path"])
                            corrected = await self.edit_file(
                                path=corrected_path,
                                start_line=start_line,
                                end_line=end_line,
                                new_content=new_content,
                                old_text=old_text,
                                new_text=new_text,
                                expected_occurrences=expected_occurrences,
                                instructions=original_instructions,
                            )
                            if corrected.success:
                                self._clear_path_failure(
                                    tool_name="edit_file", path=clean
                                )
                                return self._annotate_auto_corrected_output(
                                    corrected,
                                    original_path=clean,
                                    corrected_path=corrected_path,
                                    distance=int(correction["distance"]),
                                )
                        if correction and correction.get("ambiguous"):
                            return ToolResult(
                                success=False,
                                output={
                                    "error": f"Failed to read file for editing: {clean}",
                                    "path": clean,
                                    "auto_correct_candidates": correction.get(
                                        "candidates", []
                                    ),
                                    "auto_corrected_path": False,
                                },
                            )
                    raise
                self._clear_path_failure(tool_name="edit_file", path=clean)
                original_content = artifact_bytes.decode("utf-8", errors="ignore")

            updated_content = original_content
            mode = ""
            applied_edits: Dict[str, Any] = {}

            if line_mode_has_any:
                mode = "line_range"
                if start_line is None or end_line is None or new_content is None:
                    return ToolResult(
                        success=False,
                        output={
                            "error": "Line-range mode requires start_line, end_line, and new_content",
                            "path": clean,
                        },
                    )

                if not isinstance(new_content, str):
                    return ToolResult(
                        success=False,
                        output={
                            "error": f"new_content must be a string, got {type(new_content).__name__}",
                            "path": clean,
                        },
                    )

                try:
                    parsed_start_line = _coerce_int(start_line, "start_line")
                    parsed_end_line = _coerce_int(end_line, "end_line")
                except ValueError as parse_error:
                    return ToolResult(
                        success=False,
                        output={
                            "error": str(parse_error),
                            "path": clean,
                        },
                    )

                if parsed_start_line <= 0 or parsed_end_line <= 0:
                    return ToolResult(
                        success=False,
                        output={
                            "error": "start_line and end_line must be >= 1",
                            "path": clean,
                        },
                    )
                if parsed_start_line > parsed_end_line:
                    return ToolResult(
                        success=False,
                        output={
                            "error": "start_line cannot be greater than end_line",
                            "path": clean,
                        },
                    )

                lines = original_content.splitlines(keepends=True)
                total_lines = len(lines)
                if parsed_start_line > total_lines or parsed_end_line > total_lines:
                    return ToolResult(
                        success=False,
                        output={
                            "error": "Line range is out of bounds",
                            "path": clean,
                            "total_lines": total_lines,
                            "requested_range": [parsed_start_line, parsed_end_line],
                        },
                    )

                prefix = "".join(lines[: parsed_start_line - 1])
                suffix = "".join(lines[parsed_end_line:])
                updated_content = prefix + new_content + suffix
                applied_edits = {
                    "line_range": [parsed_start_line, parsed_end_line],
                    "replaced_lines": parsed_end_line - parsed_start_line + 1,
                }
            else:
                mode = "anchor_replace"
                if old_text is None or new_text is None:
                    return ToolResult(
                        success=False,
                        output={
                            "error": "Anchor mode requires old_text and new_text",
                            "path": clean,
                        },
                    )
                if not isinstance(old_text, str):
                    return ToolResult(
                        success=False,
                        output={
                            "error": f"old_text must be a string, got {type(old_text).__name__}",
                            "path": clean,
                        },
                    )
                if not isinstance(new_text, str):
                    return ToolResult(
                        success=False,
                        output={
                            "error": f"new_text must be a string, got {type(new_text).__name__}",
                            "path": clean,
                        },
                    )
                if not old_text:
                    return ToolResult(
                        success=False,
                        output={
                            "error": "old_text cannot be empty in anchor mode",
                            "path": clean,
                        },
                    )

                if old_text == new_text:
                    html_report_guidance = _html_report_noop_guidance(
                        clean,
                        original_content,
                    )
                    return ToolResult(
                        success=False,
                        output={
                            "path": clean,
                            "error_code": "EDIT_FILE_NOOP",
                            "mode": "anchor_replace",
                            "changed": False,
                            "error": (
                                f"Rejected no-op edit: old_text and new_text are "
                                f"exactly the same ({len(old_text)} chars each). "
                                f"The file was not modified.\n\n"
                                f"⚠️  CHARACTER LOSS WARNING: If you intended "
                                f"old_text and new_text to be different but they "
                                f"ended up identical, this is a known issue where "
                                f"single characters (like 'i', '{{', '}}') get "
                                f"dropped during tool call generation. "
                                f"Workaround: use sed -i for this specific fix, "
                                f"or use a different variable name that doesn't "
                                f"contain the dropped character."
                                f"{html_report_guidance}"
                            ),
                        },
                    )

                # Reject oversized old_text
                old_text_bytes = len(old_text.encode("utf-8"))
                if (
                    EDIT_FILE_MAX_OLD_TEXT_BYTES > 0
                    and old_text_bytes > EDIT_FILE_MAX_OLD_TEXT_BYTES
                ):
                    return ToolResult(
                        success=False,
                        output={
                            "error": (
                                f"old_text too large ({old_text_bytes} bytes, "
                                f"limit {EDIT_FILE_MAX_OLD_TEXT_BYTES}). Use line-range "
                                f"mode or write_file instead."
                            ),
                            "error_code": "OLD_TEXT_TOO_LARGE",
                            "path": clean,
                        },
                    )

                # Reject oversized files — suggest sed instead
                content_size_kb = len(original_content.encode("utf-8")) / 1024.0
                if (
                    CODE_GUARD_MAX_FILE_KB > 0
                    and content_size_kb > CODE_GUARD_MAX_FILE_KB
                ):
                    return ToolResult(
                        success=False,
                        output={
                            "error": (
                                f"File too large for edit_file ({content_size_kb:.0f} KB, limit "
                                f"{CODE_GUARD_MAX_FILE_KB} KB). Use execute_command with sed instead."
                            ),
                            "error_code": "EDIT_FILE_TOO_LARGE",
                            "path": clean,
                        },
                    )

                try:
                    parsed_expected_occurrences = _coerce_int(
                        expected_occurrences,
                        "expected_occurrences",
                    )
                except ValueError as parse_error:
                    return ToolResult(
                        success=False,
                        output={
                            "error": str(parse_error),
                            "path": clean,
                        },
                    )

                if parsed_expected_occurrences <= 0:
                    return ToolResult(
                        success=False,
                        output={
                            "error": "expected_occurrences must be >= 1",
                            "path": clean,
                        },
                    )

                actual_old, actual_count, match_diagnostics = _find_actual_old_text(
                    original_content,
                    old_text,
                    parsed_expected_occurrences,
                    file_path=clean,
                )

                if actual_old is None:
                    anchor_diag = _build_anchor_diagnostics(
                        original_content,
                        old_text,
                    )
                    error_code, error_message = _classify_anchor_failure(
                        actual_count,
                        parsed_expected_occurrences,
                        match_diagnostics,
                        original_content=original_content,
                        anchor_diag=anchor_diag,
                        old_text=old_text,
                    )
                    return ToolResult(
                        success=False,
                        output={
                            "error_code": error_code,
                            "error": error_message,
                            "path": clean,
                            "expected_occurrences": parsed_expected_occurrences,
                            "actual_occurrences": actual_count,
                            "diagnostics": {
                                **anchor_diag,
                                "match_layers": match_diagnostics,
                            },
                        },
                    )

                updated_content = original_content.replace(
                    actual_old,
                    new_text,
                    parsed_expected_occurrences,
                )
                applied_edits = {
                    "expected_occurrences": parsed_expected_occurrences,
                    "actual_occurrences": actual_count,
                    "match_layer": match_diagnostics[-1]["layer"],
                }

            changed = updated_content != original_content
            if changed:
                await self._run_blocking_sandbox_call(
                    "files.write",
                    lambda: self.sandbox.files.write(clean, updated_content),
                )
                await self._persist_workspace_artifact(
                    clean,
                    updated_content,
                    source="sandbox_code_tool.edit_file",
                    content_type="text/plain; charset=utf-8",
                )

            # Post-edit syntax check for Python files (reuse code guard)
            syntax_warning = None
            if changed and self._should_apply_code_guard(clean, updated_content):
                syntax_ok, syntax_error = _validate_python_syntax(
                    updated_content, clean
                )
                if not syntax_ok:
                    syntax_warning = {
                        "ok": False,
                        "error": syntax_error,
                    }

            diff_preview = "\n".join(
                difflib.unified_diff(
                    original_content.splitlines(),
                    updated_content.splitlines(),
                    fromfile=f"{clean}:before",
                    tofile=f"{clean}:after",
                    lineterm="",
                ),
            )
            diff_preview = _truncate(diff_preview, limit=MAX_OUTPUT_CHARS)

            output_payload: Dict[str, Any] = {
                "path": clean,
                "mode": mode,
                "changed": changed,
                "applied_edits": applied_edits,
                "original_content": _truncate(original_content, limit=MAX_OUTPUT_CHARS),
                "updated_content": _truncate(updated_content, limit=MAX_OUTPUT_CHARS),
                "diff_preview": diff_preview,
                "output": (
                    f"Updated {clean} via {mode}"
                    if changed
                    else f"No content change for {clean} via {mode}"
                ),
            }
            if syntax_warning is not None:
                output_payload["syntax_warning"] = syntax_warning
            self._clear_path_failure(tool_name="edit_file", path=clean)
            return ToolResult(success=True, output=output_payload)
        except Exception as e:
            logger.warning(f"edit_file failed: {e}")
            return ToolResult(success=False, output={"error": str(e), "path": path})

    def _should_apply_code_guard(self, path: str, content: str) -> bool:
        if not CODE_GUARD_ENABLED:
            return False
        if not _is_python_path(path):
            return False
        if CODE_GUARD_LANGS and not ({"py", "python"} & CODE_GUARD_LANGS):
            return False

        if CODE_GUARD_MAX_FILE_KB > 0:
            content_size_kb = len(content.encode("utf-8")) / 1024.0
            if content_size_kb > CODE_GUARD_MAX_FILE_KB:
                return False
        return True

    async def _emit_guard_stage(
        self,
        path: str,
        stage: str,
        *,
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        payload_extra: Dict[str, Any] = {"post_write_stage": stage}
        if extra:
            payload_extra.update(extra)
        await self._emit_write_file_status(
            "tool_progress",
            path,
            extra=payload_extra,
        )

    async def _prepare_python_write_validation(
        self,
        path: str,
        content: str,
    ) -> Dict[str, Any]:
        validation_payload: Dict[str, Any] = {
            "language": "python",
            "guard_enabled": True,
            "formatter_applied": False,
            "formatter_name": None,
        }

        await self._emit_guard_stage(path, "normalize")
        normalized_content, normalize_flags = _normalize_python_content(content)
        validation_payload.update(normalize_flags)

        await self._emit_guard_stage(path, "validate")
        repaired_content, syntax_ok, syntax_error, repairs = (
            _attempt_python_indentation_repairs(
                normalized_content,
                path,
            )
        )
        validation_payload["indentation_repairs"] = repairs
        validation_payload["repair_passes"] = len(repairs)
        validation_payload["syntax_ok"] = syntax_ok
        if syntax_error:
            validation_payload["error"] = syntax_error
        validation_payload["processed_content"] = repaired_content
        return validation_payload

    async def _read_text_file_from_sandbox(self, path: str) -> str:
        raw_content = await self._run_blocking_sandbox_call(
            "files.read",
            lambda: self.sandbox.files.read(path),
        )
        if isinstance(raw_content, (bytes, bytearray)):
            return bytes(raw_content).decode("utf-8", errors="ignore")
        return str(raw_content)

    async def _try_autoformat_python_file(self, path: str) -> Dict[str, Any]:
        quoted_path = shlex.quote(path)
        formatter_commands = [
            ("black", f"python3 -m black --quiet {quoted_path}"),
            ("ruff", f"ruff format {quoted_path}"),
            ("ruff_module", f"python3 -m ruff format {quoted_path}"),
        ]
        formatter_errors: List[Dict[str, Any]] = []

        for formatter_name, command in formatter_commands:
            try:
                result = await self._run_blocking_sandbox_call(
                    "commands.run",
                    lambda: self.sandbox.commands.run(command),
                )
                exit_code = int(getattr(result, "exit_code", 1) or 0)
                if exit_code == 0:
                    return {
                        "formatter_applied": True,
                        "formatter_name": formatter_name,
                    }
                formatter_errors.append(
                    {
                        "formatter": formatter_name,
                        "exit_code": exit_code,
                        "stderr": _truncate(getattr(result, "stderr", "")),
                        "stdout": _truncate(getattr(result, "stdout", "")),
                    },
                )
            except Exception as formatter_error:
                formatter_errors.append(
                    {
                        "formatter": formatter_name,
                        "error": str(formatter_error),
                    },
                )

        return {
            "formatter_applied": False,
            "formatter_name": None,
            "formatter_errors": formatter_errors,
        }

    async def _finalize_python_write_validation(
        self,
        path: str,
        validation_payload: Dict[str, Any],
    ) -> Dict[str, Any]:
        final_content = str(validation_payload.get("processed_content", ""))
        syntax_ok = bool(validation_payload.get("syntax_ok", False))

        if syntax_ok and CODE_GUARD_AUTOFORMAT:
            await self._emit_guard_stage(path, "format")
            formatter_result = await self._try_autoformat_python_file(path)
            validation_payload["formatter_applied"] = formatter_result.get(
                "formatter_applied", False
            )
            validation_payload["formatter_name"] = formatter_result.get(
                "formatter_name"
            )
            if formatter_result.get("formatter_errors"):
                validation_payload["formatter_errors"] = formatter_result[
                    "formatter_errors"
                ]

            if validation_payload["formatter_applied"]:
                try:
                    final_content = await self._read_text_file_from_sandbox(path)
                except Exception as read_error:
                    validation_payload["formatter_read_error"] = str(read_error)

        await self._emit_guard_stage(path, "validate")
        final_syntax_ok, final_syntax_error = _validate_python_syntax(
            final_content, path
        )
        validation_payload["syntax_ok"] = final_syntax_ok
        if final_syntax_error:
            validation_payload["error"] = final_syntax_error
        else:
            validation_payload.pop("error", None)

        validation_payload["processed_content"] = final_content
        validation_payload["final_bytes"] = len(final_content.encode("utf-8"))
        validation_payload["line_count"] = len(final_content.splitlines())
        return validation_payload

    def _resolve_agent_run_context(
        self,
    ) -> tuple[Optional[str], Optional[str], Optional[int]]:
        try:
            return get_agent_run_execution_context()
        except Exception:
            try:
                agent_run_id, thread_id = get_agent_run_context()
            except Exception:
                return None, None, None
            return agent_run_id, thread_id, None

    def _resolve_response_stream_keys(
        self,
        *,
        agent_run_id: str,
        execution_epoch: Optional[int],
    ) -> tuple[str, Optional[str]]:
        primary_response_list_key = build_response_list_key(
            agent_run_id,
            execution_epoch=execution_epoch,
        )
        compatibility_response_list_key: Optional[str] = None
        if execution_epoch is not None:
            compatibility_response_list_key = build_response_list_key(agent_run_id)
            if compatibility_response_list_key == primary_response_list_key:
                compatibility_response_list_key = None
        return primary_response_list_key, compatibility_response_list_key

    async def _push_agent_run_response_payload(
        self,
        *,
        primary_response_list_key: str,
        response_channel: str,
        payload_json: str,
        compatibility_response_list_key: Optional[str] = None,
        expire_primary: bool,
        expire_compatibility: bool,
    ) -> int:
        list_length = await asyncio.wait_for(
            redis.rpush(primary_response_list_key, payload_json),
            timeout=REDIS_PUSH_TIMEOUT_SECONDS,
        )
        if expire_primary:
            await asyncio.wait_for(
                redis.expire(primary_response_list_key, 3600 * 24),
                timeout=REDIS_PUSH_TIMEOUT_SECONDS,
            )

        if compatibility_response_list_key:
            try:
                await asyncio.wait_for(
                    redis.rpush(compatibility_response_list_key, payload_json),
                    timeout=REDIS_PUSH_TIMEOUT_SECONDS,
                )
                if expire_compatibility:
                    await asyncio.wait_for(
                        redis.expire(compatibility_response_list_key, 3600 * 24),
                        timeout=REDIS_PUSH_TIMEOUT_SECONDS,
                    )
            except Exception as mirror_error:
                logger.debug(
                    "tool response compatibility mirror failed",
                    primary_response_list_key=primary_response_list_key,
                    compatibility_response_list_key=compatibility_response_list_key,
                    error=str(mirror_error),
                )

        await asyncio.wait_for(
            redis.publish(response_channel, "new"),
            timeout=REDIS_PUSH_TIMEOUT_SECONDS,
        )
        return int(list_length or 0)

    async def _is_run_write_allowed(
        self,
        agent_run_id: str,
        *,
        use_cache: bool = True,
    ) -> bool:
        if not agent_run_id:
            return False

        now = time.monotonic()
        if use_cache and RUN_WRITE_LIVENESS_CACHE_SECONDS > 0:
            cached = _run_write_liveness_cache.get(agent_run_id)
            if cached and (now - cached[0]) <= RUN_WRITE_LIVENESS_CACHE_SECONDS:
                return bool(cached[1])

        allowed = True
        try:
            active_keys = await redis.keys(f"active_run:*:{agent_run_id}")
            allowed = bool(active_keys)
        except Exception as check_error:
            # Keep write pipeline fail-open if liveness check is temporarily unavailable.
            logger.debug(
                "write_file liveness check fallback-open for run=%s: %s",
                agent_run_id,
                check_error,
            )
            allowed = True

        _run_write_liveness_cache[agent_run_id] = (now, allowed)
        return allowed

    async def _emit_tool_status(
        self,
        status_type: str,
        function_name: str,
        *,
        arguments: Optional[Any] = None,
        message: Optional[str] = None,
        extra: Optional[Dict[str, Any]] = None,
        xml_tag_name: Optional[str] = None,
        tool_index: int = 0,
    ) -> None:
        if not WRITE_FILE_STATUS_ENABLED:
            return

        resolved_context = self._resolve_agent_run_context()
        agent_run_id = resolved_context[0] if len(resolved_context) > 0 else None
        thread_id = resolved_context[1] if len(resolved_context) > 1 else None
        execution_epoch = resolved_context[2] if len(resolved_context) > 2 else None
        if not agent_run_id or not thread_id:
            return
        if not await self._is_run_write_allowed(agent_run_id, use_cache=False):
            logger.debug(
                "Dropping tool status after terminal run fence",
                status_type=status_type,
                function_name=function_name,
                agent_run_id=agent_run_id,
            )
            return

        if isinstance(arguments, str):
            serialized_arguments = arguments
        else:
            serialized_arguments = json.dumps(arguments or {}, ensure_ascii=False)

        content_payload: Dict[str, Any] = {
            "role": "assistant",
            "status_type": status_type,
            "function_name": function_name,
            "arguments": serialized_arguments,
            "xml_tag_name": xml_tag_name or function_name,
            "tool_index": tool_index,
        }
        if message:
            content_payload["message"] = message
        if extra:
            content_payload.update(extra)

        logger.info(
            "tool status emitted",
            trace_source_stage="sandbox_code_tool",
            status_type=status_type,
            function_name=function_name,
            agent_run_id=agent_run_id,
        )

        now = datetime.now(timezone.utc).isoformat()
        payload = {
            "sequence": int(time.time() * 1000),
            "message_id": None,
            "thread_id": thread_id,
            "type": "status",
            "is_llm_message": False,
            "content": json.dumps(content_payload, ensure_ascii=False),
            "metadata": json.dumps({"thread_run_id": agent_run_id}, ensure_ascii=False),
            "created_at": now,
            "updated_at": now,
        }
        response_list_key, compatibility_response_list_key = (
            self._resolve_response_stream_keys(
                agent_run_id=agent_run_id,
                execution_epoch=execution_epoch,
            )
        )
        response_channel = f"agent_run:{agent_run_id}:new_response"

        try:
            await self._push_agent_run_response_payload(
                primary_response_list_key=response_list_key,
                response_channel=response_channel,
                payload_json=json.dumps(payload, ensure_ascii=False),
                compatibility_response_list_key=compatibility_response_list_key,
                expire_primary=True,
                expire_compatibility=True,
            )
        except Exception as status_error:
            logger.debug(
                "tool status emit failed",
                status_type=status_type,
                function_name=function_name,
                error=str(status_error),
            )

    async def _emit_write_file_status(
        self,
        status_type: str,
        path: str,
        *,
        message: Optional[str] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
        await self._emit_tool_status(
            status_type,
            "write_file",
            arguments={"path": path},
            message=message,
            extra=extra,
            xml_tag_name="write_file",
            tool_index=0,
        )

    async def _resolve_write_stream_backlog_profile(
        self,
        *,
        response_list_key: str,
        agent_run_id: str,
        path: str,
    ) -> Tuple[int, Optional[int]]:
        base_max_updates = max(0, WRITE_STREAM_MAX_UPDATES)
        if (
            base_max_updates <= 0
            or WRITE_STREAM_HIGH_BACKLOG_THRESHOLD <= 0
            or WRITE_STREAM_HIGH_BACKLOG_MAX_UPDATES >= base_max_updates
        ):
            return base_max_updates, None

        try:
            backlog = await asyncio.wait_for(
                redis.llen(response_list_key),
                timeout=REDIS_PUSH_TIMEOUT_SECONDS,
            )
        except Exception as backlog_error:
            logger.debug(
                "write_file backlog inspection failed",
                path=path,
                agent_run_id=agent_run_id,
                error=str(backlog_error),
            )
            return base_max_updates, None

        try:
            backlog_size = int(backlog)
        except (TypeError, ValueError):
            return base_max_updates, None

        if backlog_size < WRITE_STREAM_HIGH_BACKLOG_THRESHOLD:
            return base_max_updates, backlog_size

        throttled_updates = min(base_max_updates, WRITE_STREAM_HIGH_BACKLOG_MAX_UPDATES)
        logger.info(
            "write_file backlog throttle active",
            path=path,
            agent_run_id=agent_run_id,
            list_length=backlog_size,
            base_max_updates=base_max_updates,
            throttled_max_updates=throttled_updates,
        )
        return throttled_updates, backlog_size

    async def _stream_write_progress(self, path: str, content: str) -> None:
        """Stream incremental write progress to the agent SSE channel."""
        if not content:
            return

        resolved_context = self._resolve_agent_run_context()
        agent_run_id = resolved_context[0] if len(resolved_context) > 0 else None
        thread_id = resolved_context[1] if len(resolved_context) > 1 else None
        execution_epoch = resolved_context[2] if len(resolved_context) > 2 else None
        if not agent_run_id or not thread_id:
            logger.info(
                "write_file streaming skipped (missing context)",
                path=path,
            )
            return
        if not await self._is_run_write_allowed(agent_run_id, use_cache=False):
            logger.debug(
                "write_file streaming skipped by terminal run fence",
                path=path,
                agent_run_id=agent_run_id,
            )
            return

        response_list_key, compatibility_response_list_key = (
            self._resolve_response_stream_keys(
                agent_run_id=agent_run_id,
                execution_epoch=execution_epoch,
            )
        )
        response_channel = f"agent_run:{agent_run_id}:new_response"
        max_updates, backlog_size = await self._resolve_write_stream_backlog_profile(
            response_list_key=response_list_key,
            agent_run_id=agent_run_id,
            path=path,
        )
        total_chars = len(content)
        if max_updates <= 0:
            logger.info(
                "write_file streaming skipped (updates disabled)",
                path=path,
            )
            return
        if WRITE_STREAM_MAX_CHARS > 0 and total_chars > WRITE_STREAM_MAX_CHARS:
            logger.info(
                "write_file streaming skipped (content too large)",
                path=path,
                total_chars=total_chars,
                limit=WRITE_STREAM_MAX_CHARS,
            )
            return
        if (
            max_updates == WRITE_STREAM_MAX_UPDATES
            and WRITE_STREAM_TOKEN_MODE_MAX_CHARS > 0
            and total_chars <= WRITE_STREAM_TOKEN_MODE_MAX_CHARS
        ):
            # For short writes, stream character-by-character for ChatGPT-like UX.
            chunk_size = 1
        else:
            chunk_size = max(
                WRITE_STREAM_MIN_CHUNK_CHARS,
                math.ceil(total_chars / max_updates),
            )
        total_chunks = math.ceil(total_chars / chunk_size) if total_chars else 0
        logger.info(
            "write_file streaming start",
            path=path,
            total_chars=total_chars,
            chunk_size=chunk_size,
            total_chunks=total_chunks,
            max_updates=max_updates,
            backlog_size=backlog_size,
            agent_run_id=agent_run_id,
            thread_id=thread_id,
        )

        chunk_index = 0
        start_index = 0
        for end in range(chunk_size, total_chars + chunk_size, chunk_size):
            if not await self._is_run_write_allowed(agent_run_id):
                logger.debug(
                    "write_file streaming stopped by terminal run fence",
                    path=path,
                    chunk_index=chunk_index,
                    agent_run_id=agent_run_id,
                )
                return
            end_index = min(end, total_chars)
            if end_index <= start_index:
                break
            chunk_text = content[start_index:end_index]
            tool_call_data_chunk = {
                "id": f"stream_write_{agent_run_id}",
                "index": 0,
                "type": "function",
                "function": {
                    "name": "write_file",
                    "arguments": json.dumps(
                        {
                            "file_path": path,
                            "file_contents_delta": chunk_text,
                            "delta_index": chunk_index,
                        }
                    ),
                },
            }
            now = datetime.now(timezone.utc).isoformat()
            payload = {
                "sequence": chunk_index,
                "message_id": None,
                "thread_id": thread_id,
                "type": "assistant",
                "is_llm_message": True,
                "content": json.dumps({"role": "assistant", "content": ""}),
                "metadata": json.dumps(
                    {
                        "thread_run_id": agent_run_id,
                        "stream_status": "tool_call_chunk",
                        "tool_calls": [tool_call_data_chunk],
                    }
                ),
                "created_at": now,
                "updated_at": now,
            }
            try:
                list_length = await self._push_agent_run_response_payload(
                    primary_response_list_key=response_list_key,
                    response_channel=response_channel,
                    payload_json=json.dumps(payload, ensure_ascii=False),
                    compatibility_response_list_key=compatibility_response_list_key,
                    expire_primary=chunk_index == 0,
                    expire_compatibility=chunk_index == 0,
                )
            except Exception as stream_error:
                logger.debug(
                    "write_file streaming halted due to redis error",
                    path=path,
                    chunk_index=chunk_index,
                    error=str(stream_error),
                )
                return
            if chunk_index == 0 or chunk_index == total_chunks - 1:
                logger.info(
                    "write_file streaming chunk pushed",
                    path=path,
                    chunk_index=chunk_index,
                    total_chunks=total_chunks,
                    list_length=list_length,
                    agent_run_id=agent_run_id,
                )
            await asyncio.sleep(WRITE_STREAM_CHUNK_DELAY_SEC)
            start_index = end_index
            chunk_index += 1

        logger.info(
            "write_file streaming complete",
            path=path,
            total_chars=total_chars,
            total_chunks=total_chunks,
            agent_run_id=agent_run_id,
        )

    async def list_dir(self, path: str = ".") -> ToolResult:
        """List files in a directory."""
        clean = path if isinstance(path, str) else str(path or "")
        try:
            await self._ensure_sandbox()
            clean = self._normalize_path(path)
            entries = await self._run_blocking_sandbox_call(
                "files.list",
                lambda: self.sandbox.files.list(clean),
            )

            # entries may be pathlib or dataclass objects; coerce to dicts when possible
            def _as_dict(item: Any) -> Any:
                if isinstance(item, dict):
                    return item
                if hasattr(item, "__dict__"):
                    return {k: v for k, v in item.__dict__.items()}
                return str(item)

            listing = [_as_dict(e) for e in entries] if entries is not None else []
            return ToolResult(
                success=True,
                output={"path": clean, "entries": listing, "output": listing},
            )
        except Exception as e:
            artifact_entries = await self._list_workspace_artifact_entries(clean)
            if artifact_entries:
                return ToolResult(
                    success=True,
                    output={
                        "path": clean,
                        "entries": artifact_entries,
                        "output": artifact_entries,
                    },
                )
            logger.warning(f"list_dir failed: {e}")
            return ToolResult(success=False, output={"error": str(e), "path": path})

    async def make_dir(self, path: str) -> ToolResult:
        """Create a directory (equivalent to mkdir -p)."""
        try:
            await self._ensure_sandbox()
            clean = self._normalize_path(path)
            await self._run_blocking_sandbox_call(
                "files.make_dir",
                lambda: self.sandbox.files.make_dir(clean),
            )
            return ToolResult(
                success=True,
                output={"path": clean, "output": f"Created directory {clean}"},
            )
        except Exception as e:
            logger.warning(f"make_dir failed: {e}")
            return ToolResult(success=False, output={"error": str(e), "path": path})

    async def upload_file(self, path: str, content_base64: str) -> ToolResult:
        """Upload a base64-encoded file to /workspace."""
        try:
            await self._ensure_sandbox()
            clean = self._normalize_path(path)
            data = base64.b64decode(content_base64)
            content_text = data.decode("utf-8", errors="ignore")
            fix_script_hint = _detect_fix_script_anti_pattern(clean, content_text)
            if fix_script_hint:
                logger.warning(
                    "upload_file rejected before sandbox call",
                    trace_source_stage="sandbox_code_tool",
                    trace_exec_gate_reason="fix_script_anti_pattern",
                    path=clean,
                )
                return ToolResult(
                    success=False,
                    output={
                        "path": clean,
                        "error": fix_script_hint,
                        "error_code": "FIX_SCRIPT_BLOCKED",
                    },
                )
            rewrite_block_payload = await self._workspace_rewrite_block_payload(
                attempted_path=clean,
                content=content_text,
            )
            if rewrite_block_payload is not None:
                error_message = str(rewrite_block_payload["error"])
                logger.warning(
                    "upload_file rejected before sandbox call",
                    trace_source_stage="sandbox_code_tool",
                    trace_exec_gate_reason="workspace_rewrite_blocked",
                    path=clean,
                    target_path=rewrite_block_payload["target_path"],
                    reason=rewrite_block_payload["reason"],
                )
                return ToolResult(
                    success=False,
                    output={
                        "path": clean,
                        "rewrite_target_path": rewrite_block_payload["target_path"],
                        "rewrite_block_reason": rewrite_block_payload["reason"],
                        "error": error_message,
                        "error_code": "WORKSPACE_REWRITE_BLOCKED",
                    },
                )
            await self._run_blocking_sandbox_call(
                "files.write",
                lambda: self.sandbox.files.write(clean, data),
            )
            await self._persist_workspace_artifact(
                clean,
                data,
                source="sandbox_code_tool.upload_file",
            )
            return ToolResult(
                success=True,
                output={
                    "path": clean,
                    "bytes": len(data),
                    "output": f"Uploaded {len(data)} bytes to {clean}",
                },
            )
        except Exception as e:
            logger.warning(f"upload_file failed: {e}")
            return ToolResult(success=False, output={"error": str(e), "path": path})

    async def download_file(self, path: str, as_base64: bool = False) -> ToolResult:
        """Download a file from the sandbox to local ./downloads or return base64."""
        clean = path if isinstance(path, str) else str(path or "")
        try:
            await self._ensure_sandbox()
            clean = self._normalize_path(path)
            # Read raw bytes to avoid corrupting binary files (e.g., zip archives)
            content = await self._run_blocking_sandbox_call(
                "files.read",
                lambda: self.sandbox.files.read(clean, format="bytes"),
            )

            # Normalize to bytes for binary-safe writes
            if isinstance(content, (bytes, bytearray)):
                data: bytes = bytes(content)
            elif isinstance(content, str):
                data = content.encode()
            else:
                data = str(content).encode()

            if as_base64:
                encoded = base64.b64encode(data).decode()
                return ToolResult(
                    success=True,
                    output={"path": clean, "base64": encoded, "bytes": len(data)},
                )

            downloads_dir = os.path.abspath("downloads")
            os.makedirs(downloads_dir, exist_ok=True)
            filename = os.path.basename(clean) or "downloaded_file"
            local_path = os.path.join(downloads_dir, filename)
            with open(local_path, "wb") as f:
                f.write(data)

            return ToolResult(
                success=True,
                output={
                    "path": clean,
                    "local_path": local_path,
                    "bytes": os.path.getsize(local_path),
                    "output": f"Downloaded {os.path.getsize(local_path)} bytes to {local_path}",
                },
            )
        except Exception as e:
            artifact_bytes = await self._read_workspace_artifact_bytes(clean)
            if artifact_bytes is not None:
                data = artifact_bytes
                if as_base64:
                    encoded = base64.b64encode(data).decode()
                    return ToolResult(
                        success=True,
                        output={"path": clean, "base64": encoded, "bytes": len(data)},
                    )

                downloads_dir = os.path.abspath("downloads")
                os.makedirs(downloads_dir, exist_ok=True)
                filename = os.path.basename(clean) or "downloaded_file"
                local_path = os.path.join(downloads_dir, filename)
                with open(local_path, "wb") as f:
                    f.write(data)

                return ToolResult(
                    success=True,
                    output={
                        "path": clean,
                        "local_path": local_path,
                        "bytes": os.path.getsize(local_path),
                        "output": f"Downloaded {os.path.getsize(local_path)} bytes to {local_path}",
                    },
                )
            logger.warning(f"download_file failed: {e}")
            return ToolResult(success=False, output={"error": str(e), "path": path})

    async def expose_port(self, port: int) -> ToolResult:
        """Get a public URL for a sandbox service on the given port."""
        try:
            await self._ensure_sandbox()
            host = await self._run_blocking_sandbox_call(
                "get_host",
                lambda: self.sandbox.get_host(port),
            )
            url = f"https://{host}"
            return ToolResult(
                success=True,
                output={
                    "port": port,
                    "url": url,
                    "output": f"Port {port} exposed at {url}",
                },
            )
        except Exception as e:
            logger.warning(f"expose_port failed: {e}")
            return ToolResult(success=False, output={"error": str(e), "port": port})
