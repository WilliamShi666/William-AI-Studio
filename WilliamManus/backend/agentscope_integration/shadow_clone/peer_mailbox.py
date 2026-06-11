"""Redis-backed peer note mailbox for Shadow Clone subagents."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, Optional, Sequence, Tuple
from uuid import uuid4

from services import redis as redis_service

from .constants import (
    SHADOW_CLONE_PEER_NOTE_MAX_APPENDIX_CHARS,
    SHADOW_CLONE_PEER_NOTE_MAX_DETAILS_CHARS,
    SHADOW_CLONE_PEER_NOTE_MAX_INJECTED_NOTES_PER_DRAIN,
    SHADOW_CLONE_PEER_NOTE_MAX_INJECTION_CHARS,
    SHADOW_CLONE_PEER_NOTE_MAX_SUMMARY_CHARS,
    SHADOW_CLONE_PEER_NOTE_TTL,
)


PEER_NOTE_MEMORY_MARK = "peer_note"
PEER_NOTE_MAILBOX_KEY = "shadow_clone:{run_id}:peer_notes:{recipient_subtask_id}"
SUPERVISOR_SENDER_SUBTASK_ID = "shadow_clone_coordinator"
SUPERVISOR_SENDER_ROLE = "Coordinator"

_APPEND_PEER_NOTE_LUA = """
local mailbox_key = KEYS[1]
local note_json = ARGV[1]
local ttl = tonumber(ARGV[2])

redis.call('RPUSH', mailbox_key, note_json)
redis.call('EXPIRE', mailbox_key, ttl)
return 1
"""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _mailbox_key(run_id: str, recipient_subtask_id: str) -> str:
    return PEER_NOTE_MAILBOX_KEY.format(
        run_id=run_id,
        recipient_subtask_id=recipient_subtask_id,
    )


def _truncate_text(text: str, max_chars: int) -> str:
    value = str(text or "").strip()
    if len(value) <= max_chars:
        return value
    return value[:max_chars].rstrip() + "..."


def _coerce_note(raw_note: Any) -> Optional[Dict[str, str]]:
    if isinstance(raw_note, str):
        try:
            raw_note = json.loads(raw_note)
        except Exception:
            return None
    if not isinstance(raw_note, dict):
        return None

    note_id = str(raw_note.get("note_id") or "").strip()
    sender_subtask_id = str(raw_note.get("sender_subtask_id") or "").strip()
    recipient_subtask_id = str(raw_note.get("recipient_subtask_id") or "").strip()
    summary = _truncate_text(
        str(raw_note.get("summary") or ""),
        SHADOW_CLONE_PEER_NOTE_MAX_SUMMARY_CHARS,
    )
    if not note_id or not sender_subtask_id or not recipient_subtask_id or not summary:
        return None

    return {
        "note_id": note_id,
        "run_id": str(raw_note.get("run_id") or "").strip(),
        "sender_subtask_id": sender_subtask_id,
        "sender_role": str(raw_note.get("sender_role") or "").strip(),
        "sender_type": str(raw_note.get("sender_type") or "peer").strip() or "peer",
        "recipient_subtask_id": recipient_subtask_id,
        "recipient_role": str(raw_note.get("recipient_role") or "").strip(),
        "summary": summary,
        "details": _truncate_text(
            str(raw_note.get("details") or ""),
            SHADOW_CLONE_PEER_NOTE_MAX_DETAILS_CHARS,
        ),
        "kind": str(raw_note.get("kind") or "cross_task_hint").strip() or "cross_task_hint",
        "command_type": str(raw_note.get("command_type") or "").strip(),
        "attempt_index": str(raw_note.get("attempt_index") or "").strip(),
        "execution_epoch": str(raw_note.get("execution_epoch") or "").strip(),
        "replacement_for": str(raw_note.get("replacement_for") or "").strip(),
        "reason": _truncate_text(
            str(raw_note.get("reason") or ""),
            SHADOW_CLONE_PEER_NOTE_MAX_DETAILS_CHARS,
        ),
        "created_at": str(raw_note.get("created_at") or "").strip(),
    }


def _estimate_note_chars(note: Dict[str, str]) -> int:
    return (
        len(note.get("sender_subtask_id") or "")
        + len(note.get("sender_role") or "")
        + len(note.get("sender_type") or "")
        + len(note.get("summary") or "")
        + len(note.get("details") or "")
        + len(note.get("command_type") or "")
        + len(note.get("reason") or "")
        + 96
    )


def _format_supervisor_note_line(note: Dict[str, str]) -> str:
    command_type = note.get("command_type") or "instruction"
    summary = note.get("summary") or ""
    reason = note.get("reason") or ""
    replacement_for = note.get("replacement_for") or ""
    header = f"- Supervisor ({command_type}): {summary}"
    parts = [header]
    if replacement_for:
        parts.append(f"  Replacement for: {replacement_for}")
    if note.get("attempt_index"):
        parts.append(f"  Attempt: {note['attempt_index']}")
    if note.get("execution_epoch"):
        parts.append(f"  Epoch: {note['execution_epoch']}")
    if reason:
        parts.append(f"  Reason: {reason}")
    if note.get("details"):
        parts.append(f"  Details: {note['details']}")
    return "\n".join(parts)


def _format_note_line(note: Dict[str, str]) -> str:
    if str(note.get("sender_type") or "").strip().lower() == "supervisor":
        return _format_supervisor_note_line(note)
    sender = note.get("sender_subtask_id") or "unknown"
    sender_role = note.get("sender_role") or ""
    role_suffix = f" ({sender_role})" if sender_role else ""
    parts = [f"- From {sender}{role_suffix}: {note.get('summary') or ''}"]
    if note.get("details"):
        parts.append(f"  Details: {note['details']}")
    return "\n".join(parts)


async def append_peer_note(
    *,
    run_id: str,
    sender_subtask_id: str,
    sender_role: str,
    recipient_subtask_id: str,
    recipient_role: str,
    summary: str,
    details: str = "",
    kind: str = "cross_task_hint",
    timeout: float | None = None,
) -> Dict[str, str]:
    """Append one note into the recipient mailbox."""
    normalized_summary = _truncate_text(summary, SHADOW_CLONE_PEER_NOTE_MAX_SUMMARY_CHARS)
    if not normalized_summary:
        raise ValueError("Peer note summary must be non-empty.")

    note = {
        "note_id": uuid4().hex,
        "run_id": str(run_id),
        "sender_subtask_id": str(sender_subtask_id),
        "sender_role": str(sender_role or ""),
        "recipient_subtask_id": str(recipient_subtask_id),
        "recipient_role": str(recipient_role or ""),
        "summary": normalized_summary,
        "details": _truncate_text(details, SHADOW_CLONE_PEER_NOTE_MAX_DETAILS_CHARS),
        "kind": str(kind or "cross_task_hint"),
        "created_at": _now_iso(),
    }

    mailbox_key = _mailbox_key(run_id, recipient_subtask_id)
    await redis_service.eval_script(
        _APPEND_PEER_NOTE_LUA,
        keys=[mailbox_key],
        args=[json.dumps(note, ensure_ascii=False), str(SHADOW_CLONE_PEER_NOTE_TTL)],
        timeout=timeout,
    )
    return note


async def append_supervisor_command(
    *,
    run_id: str,
    recipient_subtask_id: str,
    recipient_role: str,
    summary: str,
    details: str = "",
    command_type: str = "wake",
    attempt_index: int | None = None,
    execution_epoch: int | None = None,
    replacement_for: str = "",
    reason: str = "",
    timeout: float | None = None,
) -> Dict[str, str]:
    normalized_summary = _truncate_text(summary, SHADOW_CLONE_PEER_NOTE_MAX_SUMMARY_CHARS)
    if not normalized_summary:
        raise ValueError("Supervisor command summary must be non-empty.")

    note = {
        "note_id": uuid4().hex,
        "run_id": str(run_id),
        "sender_subtask_id": SUPERVISOR_SENDER_SUBTASK_ID,
        "sender_role": SUPERVISOR_SENDER_ROLE,
        "sender_type": "supervisor",
        "recipient_subtask_id": str(recipient_subtask_id),
        "recipient_role": str(recipient_role or ""),
        "summary": normalized_summary,
        "details": _truncate_text(details, SHADOW_CLONE_PEER_NOTE_MAX_DETAILS_CHARS),
        "kind": "supervisor_command",
        "command_type": str(command_type or "wake").strip() or "wake",
        "attempt_index": str(max(1, int(attempt_index))) if attempt_index else "",
        "execution_epoch": str(int(execution_epoch)) if execution_epoch is not None else "",
        "replacement_for": str(replacement_for or "").strip(),
        "reason": _truncate_text(reason, SHADOW_CLONE_PEER_NOTE_MAX_DETAILS_CHARS),
        "created_at": _now_iso(),
    }

    mailbox_key = _mailbox_key(run_id, recipient_subtask_id)
    await redis_service.eval_script(
        _APPEND_PEER_NOTE_LUA,
        keys=[mailbox_key],
        args=[json.dumps(note, ensure_ascii=False), str(SHADOW_CLONE_PEER_NOTE_TTL)],
        timeout=timeout,
    )
    return note


async def drain_new_notes(
    *,
    run_id: str,
    recipient_subtask_id: str,
    cursor: int = 0,
    seen_note_ids: Optional[Iterable[str]] = None,
    max_notes: int = SHADOW_CLONE_PEER_NOTE_MAX_INJECTED_NOTES_PER_DRAIN,
    max_chars: int = SHADOW_CLONE_PEER_NOTE_MAX_INJECTION_CHARS,
    timeout: float | None = None,
) -> Tuple[list[Dict[str, str]], int]:
    """Read newly arrived notes without advancing past undispatched entries."""
    mailbox_key = _mailbox_key(run_id, recipient_subtask_id)
    raw_items = await redis_service.lrange(
        mailbox_key,
        max(0, int(cursor or 0)),
        -1,
        timeout=timeout,
    )
    if not raw_items:
        return [], max(0, int(cursor or 0))

    seen_ids = {str(note_id) for note_id in (seen_note_ids or set()) if str(note_id).strip()}
    notes: list[Dict[str, str]] = []
    consumed_raw = 0
    estimated_chars = 0

    for raw_item in raw_items:
        note = _coerce_note(raw_item)
        if note is None:
            consumed_raw += 1
            continue
        if note["recipient_subtask_id"] != str(recipient_subtask_id):
            consumed_raw += 1
            continue
        if note["note_id"] in seen_ids:
            consumed_raw += 1
            continue

        note_chars = _estimate_note_chars(note)
        if notes and (
            len(notes) >= max(1, int(max_notes or 1))
            or estimated_chars + note_chars > max(1, int(max_chars or 1))
        ):
            break

        notes.append(note)
        seen_ids.add(note["note_id"])
        estimated_chars += note_chars
        consumed_raw += 1

    return notes, max(0, int(cursor or 0)) + consumed_raw


async def reconcile_late_notes(
    *,
    run_id: str,
    recipient_subtask_id: str,
    cursor: int = 0,
    seen_note_ids: Optional[Iterable[str]] = None,
    timeout: float | None = None,
) -> Tuple[list[Dict[str, str]], int]:
    """Drain remaining mailbox notes for final result reconciliation."""
    return await drain_new_notes(
        run_id=run_id,
        recipient_subtask_id=recipient_subtask_id,
        cursor=cursor,
        seen_note_ids=seen_note_ids,
        max_notes=max(1, SHADOW_CLONE_PEER_NOTE_MAX_INJECTED_NOTES_PER_DRAIN * 2),
        max_chars=SHADOW_CLONE_PEER_NOTE_MAX_APPENDIX_CHARS,
        timeout=timeout,
    )


def format_notes_for_injection(
    notes: Sequence[Dict[str, str]],
    *,
    recipient_subtask_id: str,
) -> str:
    """Render notes as a system-hint block for memory injection."""
    rendered_notes = "\n".join(_format_note_line(note) for note in notes)
    has_supervisor_note = any(
        str(note.get("sender_type") or "").strip().lower() == "supervisor"
        for note in notes
    )
    intro = (
        "New supervisor instructions and peer notes arrived for your subtask "
        if has_supervisor_note
        else "New peer notes arrived for your subtask "
    )
    guidance = (
        "Supervisor instructions should be followed before you continue. "
        "Peer notes remain high-signal hints, not verified facts."
        if has_supervisor_note
        else "Treat these as high-signal hints, not verified facts. Verify before relying on them."
    )
    return (
        "<system-hint>"
        f"{intro}'{recipient_subtask_id}'.\n\n"
        f"{rendered_notes}\n\n"
        f"{guidance}"
        "</system-hint>"
    )


def format_notes_for_result_appendix(notes: Sequence[Dict[str, str]]) -> str:
    """Render notes for late-note reconciliation in persisted results."""
    rendered_notes = "\n".join(_format_note_line(note) for note in notes)
    return "[Late Peer Notes]\n" + rendered_notes


def collect_seen_note_ids(
    existing_seen_ids: Optional[Iterable[str]],
    notes: Sequence[Dict[str, str]],
) -> set[str]:
    """Extend the local dedupe set with note ids from delivered notes."""
    seen_ids = {str(note_id) for note_id in (existing_seen_ids or set()) if str(note_id).strip()}
    for note in notes:
        note_id = str(note.get("note_id") or "").strip()
        if note_id:
            seen_ids.add(note_id)
    return seen_ids


__all__ = [
    "PEER_NOTE_MEMORY_MARK",
    "SUPERVISOR_SENDER_ROLE",
    "SUPERVISOR_SENDER_SUBTASK_ID",
    "append_supervisor_command",
    "append_peer_note",
    "collect_seen_note_ids",
    "drain_new_notes",
    "format_notes_for_injection",
    "format_notes_for_result_appendix",
    "reconcile_late_notes",
]
