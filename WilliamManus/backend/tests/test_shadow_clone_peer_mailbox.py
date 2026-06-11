"""Tests for Shadow Clone peer mailbox helpers."""

import json
from unittest.mock import AsyncMock, patch

import pytest

from agentscope_integration.shadow_clone.peer_mailbox import (
    append_supervisor_command,
    append_peer_note,
    collect_seen_note_ids,
    drain_new_notes,
    format_notes_for_injection,
    format_notes_for_result_appendix,
)


@pytest.mark.asyncio
async def test_append_peer_note_uses_atomic_lua_write():
    with patch("agentscope_integration.shadow_clone.peer_mailbox.redis_service") as mock_redis:
        mock_redis.eval_script = AsyncMock(return_value=1)

        note = await append_peer_note(
            run_id="run-123",
            sender_subtask_id="task-a",
            sender_role="Researcher A",
            recipient_subtask_id="task-b",
            recipient_role="Researcher B",
            summary="Important finding",
            details="Cross-check this signal.",
        )

        assert note["sender_subtask_id"] == "task-a"
        assert note["recipient_subtask_id"] == "task-b"
        assert note["summary"] == "Important finding"
        mock_redis.eval_script.assert_called_once()
        kwargs = mock_redis.eval_script.call_args.kwargs
        assert kwargs["keys"] == ["shadow_clone:run-123:peer_notes:task-b"]


@pytest.mark.asyncio
async def test_drain_new_notes_respects_cursor_and_seen_ids():
    raw_notes = [
        json.dumps(
            {
                "note_id": "note-1",
                "sender_subtask_id": "task-a",
                "recipient_subtask_id": "task-b",
                "summary": "already seen",
            },
        ),
        json.dumps(
            {
                "note_id": "note-2",
                "sender_subtask_id": "task-a",
                "sender_role": "Researcher A",
                "recipient_subtask_id": "task-b",
                "summary": "new note",
                "details": "details 2",
            },
        ),
        json.dumps(
            {
                "note_id": "note-3",
                "sender_subtask_id": "task-c",
                "recipient_subtask_id": "task-b",
                "summary": "queued for later",
            },
        ),
    ]

    with patch("agentscope_integration.shadow_clone.peer_mailbox.redis_service") as mock_redis:
        mock_redis.lrange = AsyncMock(return_value=raw_notes)

        notes, next_cursor = await drain_new_notes(
            run_id="run-123",
            recipient_subtask_id="task-b",
            cursor=0,
            seen_note_ids={"note-1"},
            max_notes=1,
            max_chars=1000,
        )

        assert [note["note_id"] for note in notes] == ["note-2"]
        assert next_cursor == 2


@pytest.mark.asyncio
async def test_append_supervisor_command_persists_supervisor_metadata():
    with patch("agentscope_integration.shadow_clone.peer_mailbox.redis_service") as mock_redis:
        mock_redis.eval_script = AsyncMock(return_value=1)

        note = await append_supervisor_command(
            run_id="run-123",
            recipient_subtask_id="task-b",
            recipient_role="Researcher B",
            summary="Resume the interrupted task.",
            details="Reuse the previous memory context before redoing work.",
            command_type="wake",
            attempt_index=2,
            execution_epoch=7,
            reason="provider response parse error",
        )

        assert note["sender_type"] == "supervisor"
        assert note["command_type"] == "wake"
        assert note["attempt_index"] == "2"
        assert note["execution_epoch"] == "7"
        assert note["reason"] == "provider response parse error"
        mock_redis.eval_script.assert_called_once()
        kwargs = mock_redis.eval_script.call_args.kwargs
        assert kwargs["keys"] == ["shadow_clone:run-123:peer_notes:task-b"]


def test_formatters_and_seen_id_collection():
    notes = [
        {
            "note_id": "note-1",
            "sender_subtask_id": "task-a",
            "sender_role": "Researcher A",
            "recipient_subtask_id": "task-b",
            "summary": "Important finding",
            "details": "Verify against the filing.",
        },
        {
            "note_id": "note-2",
            "sender_subtask_id": "shadow_clone_coordinator",
            "sender_role": "Coordinator",
            "sender_type": "supervisor",
            "recipient_subtask_id": "task-b",
            "summary": "Resume the interrupted subtask",
            "details": "Continue from the previous context window.",
            "command_type": "wake",
            "attempt_index": "2",
            "execution_epoch": "5",
            "reason": "timeout",
        },
    ]

    injection = format_notes_for_injection(notes, recipient_subtask_id="task-b")
    appendix = format_notes_for_result_appendix(notes)
    seen_ids = collect_seen_note_ids({"existing"}, notes)

    assert "<system-hint>" in injection
    assert "task-b" in injection
    assert "Researcher A" in injection
    assert "Supervisor instructions should be followed before you continue" in injection
    assert "Supervisor (wake)" in injection
    assert appendix.startswith("[Late Peer Notes]")
    assert "Important finding" in appendix
    assert "Resume the interrupted subtask" in appendix
    assert seen_ids == {"existing", "note-1", "note-2"}
