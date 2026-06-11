from __future__ import annotations

import json
import asyncio

import pytest

from agentscope_integration.shadow_clone_v2 import mailbox
from agentscope_integration.shadow_clone_v2.models import (
    MailboxMessage,
    MessageKind,
    MessageType,
)


class _FakeRedisClient:
    def __init__(self) -> None:
        self.xadd_calls = []
        self.stream_entries = []
        self.values = {}
        self.eval_barrier = None
        self.eval_lock = asyncio.Lock()

    async def xadd(self, key, fields, maxlen=None, approximate=True):
        self.xadd_calls.append(
            {
                "key": key,
                "fields": dict(fields),
                "maxlen": maxlen,
                "approximate": approximate,
            }
        )
        stream_id = f"{len(self.stream_entries) + 1}-0"
        self.stream_entries.append((key, stream_id, dict(fields)))
        return stream_id

    async def xrange(self, key, min="-", max="+", count=None):  # noqa: A002
        entries = [
            (stream_id, fields)
            for entry_key, stream_id, fields in self.stream_entries
            if entry_key == key
            and (min in ("-", stream_id) or stream_id >= min)
            and (max in ("+", stream_id) or stream_id <= max)
        ]
        return list(entries[: count or None])

    async def get(self, key):
        return self.values.get(key)

    async def set(self, key, value, ex=None, nx=False):
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    async def delete(self, key):
        existed = key in self.values
        self.values.pop(key, None)
        return int(existed)

    async def incr(self, key):
        value = int(self.values.get(key, "0")) + 1
        self.values[key] = str(value)
        return value

    async def eval(self, _script, numkeys, *keys_and_args):
        assert numkeys == 2
        idem_key, stream_key, *args = keys_and_args
        (
            maxlen,
            placeholder_json,
            _idem_ttl_seconds,
        ) = args
        if idem_key in self.values:
            return [0, self.values[idem_key]]
        if self.eval_barrier is not None:
            await self.eval_barrier.wait()
        async with self.eval_lock:
            if idem_key in self.values:
                return [0, self.values[idem_key]]
            stream_id = await self.xadd(
                stream_key,
                {"message": placeholder_json},
                maxlen=int(maxlen),
                approximate=True,
            )
            self.values[idem_key] = stream_id
            return [1, stream_id]


class _AsyncBarrier:
    def __init__(self, parties: int) -> None:
        self._parties = parties
        self._arrived = 0
        self._event = asyncio.Event()

    async def wait(self) -> None:
        self._arrived += 1
        if self._arrived >= self._parties:
            self._event.set()
        await self._event.wait()


def test_mailbox_message_uses_payload_default_factory() -> None:
    first = MailboxMessage(
        id="message-1",
        run_id="run-1",
        thread_id="thread-1",
        project_id="project-1",
        sender="facilitator",
        recipient="reviewer",
        kind=MessageKind.CONTROL,
        type=MessageType.WAKE,
        idempotency_key="wake-reviewer-1",
        created_at="2026-05-31T00:00:00Z",
    )
    second = MailboxMessage(
        id="message-2",
        run_id="run-1",
        thread_id="thread-1",
        project_id="project-1",
        sender="facilitator",
        recipient="researcher",
        kind=MessageKind.CONTROL,
        type=MessageType.WAKE,
        idempotency_key="wake-researcher-1",
        created_at="2026-05-31T00:00:00Z",
    )

    first.payload["task"] = "revise"

    assert second.payload == {}


@pytest.mark.asyncio
async def test_send_message_writes_to_control_mailbox_stream(monkeypatch) -> None:
    fake_client = _FakeRedisClient()

    async def _get_client():
        return fake_client

    monkeypatch.setattr(mailbox.redis_service, "get_client", _get_client)

    message = await mailbox.send_message(
        run_id="run-1",
        thread_id="thread-1",
        project_id="project-1",
        sender="facilitator",
        recipient="reviewer",
        kind=MessageKind.CONTROL,
        message_type=MessageType.WAKE,
        idempotency_key="wake-reviewer-1",
        payload={"reason": "follow_up"},
        created_at="2026-05-31T00:00:00Z",
    )

    assert message.id == "1-0"
    assert fake_client.xadd_calls[0]["key"] == (
        "sc_v2:project:project-1:thread:thread-1:mailbox:reviewer:control"
    )
    serialized = fake_client.xadd_calls[0]["fields"]["message"]
    assert json.loads(serialized)["payload"] == {"reason": "follow_up"}


@pytest.mark.asyncio
async def test_read_messages_replays_mailbox_messages(monkeypatch) -> None:
    fake_client = _FakeRedisClient()

    async def _get_client():
        return fake_client

    monkeypatch.setattr(mailbox.redis_service, "get_client", _get_client)

    await mailbox.send_message(
        run_id="run-1",
        thread_id="thread-1",
        project_id="project-1",
        sender="facilitator",
        recipient="reviewer",
        kind=MessageKind.CONTROL,
        message_type=MessageType.WAKE,
        idempotency_key="wake-reviewer-1",
        payload={"reason": "follow_up"},
        created_at="2026-05-31T00:00:00Z",
    )

    messages = await mailbox.read_messages(
        project_id="project-1",
        thread_id="thread-1",
        agent_name="reviewer",
        kind=MessageKind.CONTROL,
    )

    assert len(messages) == 1
    assert messages[0].type == MessageType.WAKE
    assert messages[0].payload == {"reason": "follow_up"}


@pytest.mark.asyncio
async def test_send_message_is_idempotent_by_recipient_and_idempotency_key(
    monkeypatch,
) -> None:
    fake_client = _FakeRedisClient()

    async def _get_client():
        return fake_client

    monkeypatch.setattr(mailbox.redis_service, "get_client", _get_client)

    first = await mailbox.send_message(
        run_id="run-1",
        thread_id="thread-1",
        project_id="project-1",
        sender="researcher",
        recipient="reviewer",
        kind=MessageKind.TEXT,
        message_type=MessageType.TEXT,
        idempotency_key="researcher-reviewer-1",
        text="Please review this.",
        created_at="2026-06-03T00:00:00Z",
    )
    second = await mailbox.send_message(
        run_id="run-1",
        thread_id="thread-1",
        project_id="project-1",
        sender="researcher",
        recipient="reviewer",
        kind=MessageKind.TEXT,
        message_type=MessageType.TEXT,
        idempotency_key="researcher-reviewer-1",
        text="Duplicate should not enqueue.",
        created_at="2026-06-03T00:00:01Z",
    )

    assert second == first
    assert len(fake_client.xadd_calls) == 1


@pytest.mark.asyncio
async def test_send_message_idempotency_is_atomic_under_concurrent_duplicates(
    monkeypatch,
) -> None:
    fake_client = _FakeRedisClient()
    fake_client.eval_barrier = _AsyncBarrier(2)

    async def _get_client():
        return fake_client

    monkeypatch.setattr(mailbox.redis_service, "get_client", _get_client)

    async def _send(text: str):
        return await mailbox.send_message(
            run_id="run-1",
            thread_id="thread-1",
            project_id="project-1",
            sender="researcher",
            recipient="reviewer",
            kind=MessageKind.TEXT,
            message_type=MessageType.TEXT,
            idempotency_key="same-concurrent-message",
            text=text,
            created_at="2026-06-03T00:00:00Z",
        )

    first, second = await asyncio.gather(_send("first"), _send("second"))

    assert first.id == second.id == "1-0"
    assert first.text == second.text
    assert first.text in {"first", "second"}
    assert len(fake_client.xadd_calls) == 1


@pytest.mark.asyncio
async def test_mark_sent_projected_sets_duplicate_projection_marker(
    monkeypatch,
) -> None:
    fake_client = _FakeRedisClient()

    async def _get_client():
        return fake_client

    monkeypatch.setattr(mailbox.redis_service, "get_client", _get_client)

    first = await mailbox.send_message(
        run_id="run-1",
        thread_id="thread-1",
        project_id="project-1",
        sender="researcher",
        recipient="reviewer",
        kind=MessageKind.TEXT,
        message_type=MessageType.TEXT,
        idempotency_key="projected-message",
        text="Project me once.",
        created_at="2026-06-03T00:00:00Z",
    )
    await mailbox.mark_sent_projected(first)
    duplicate = await mailbox.send_message(
        run_id="run-1",
        thread_id="thread-1",
        project_id="project-1",
        sender="researcher",
        recipient="reviewer",
        kind=MessageKind.TEXT,
        message_type=MessageType.TEXT,
        idempotency_key="projected-message",
        text="Do not project again.",
        created_at="2026-06-03T00:00:01Z",
    )

    assert duplicate.id == first.id
    assert getattr(duplicate, "_was_created") is False
    assert getattr(duplicate, "_sent_projected") is True
    assert len(fake_client.xadd_calls) == 1


@pytest.mark.asyncio
async def test_read_messages_prioritizes_control_lane_before_text(
    monkeypatch,
) -> None:
    fake_client = _FakeRedisClient()

    async def _get_client():
        return fake_client

    monkeypatch.setattr(mailbox.redis_service, "get_client", _get_client)

    await mailbox.send_message(
        run_id="run-1",
        thread_id="thread-1",
        project_id="project-1",
        sender="facilitator",
        recipient="reviewer",
        kind=MessageKind.TEXT,
        message_type=MessageType.TEXT,
        idempotency_key="text-1",
        text="Normal text.",
        created_at="2026-06-03T00:00:00Z",
    )
    await mailbox.send_message(
        run_id="run-1",
        thread_id="thread-1",
        project_id="project-1",
        sender="facilitator",
        recipient="reviewer",
        kind=MessageKind.CONTROL,
        message_type=MessageType.WAKE,
        idempotency_key="wake-1",
        payload={"reason": "task"},
        created_at="2026-06-03T00:00:01Z",
    )

    messages = await mailbox.read_next_messages(
        project_id="project-1",
        thread_id="thread-1",
        agent_name="reviewer",
        count=2,
    )

    assert [message.kind for message in messages] == [
        MessageKind.CONTROL,
        MessageKind.TEXT,
    ]
    assert [message.delivery_attempts for message in messages] == [1, 1]


@pytest.mark.asyncio
async def test_ack_message_records_durable_ack_state(monkeypatch) -> None:
    fake_client = _FakeRedisClient()

    async def _get_client():
        return fake_client

    monkeypatch.setattr(mailbox.redis_service, "get_client", _get_client)
    await mailbox.send_message(
        run_id="run-1",
        thread_id="thread-1",
        project_id="project-1",
        sender="facilitator",
        recipient="reviewer",
        kind=MessageKind.TEXT,
        message_type=MessageType.TEXT,
        idempotency_key="ack-state",
        text="Ack me.",
        created_at="2026-06-03T00:00:00Z",
    )

    acked = await mailbox.ack_message(
        project_id="project-1",
        thread_id="thread-1",
        agent_name="reviewer",
        message_id="1-0",
        acked_by="reviewer",
        acked_at="2026-06-03T00:01:00Z",
    )

    assert acked.already_acked is False
    stored = json.loads(
        fake_client.values[
            "sc_v2:project:project-1:thread:thread-1:mailbox:reviewer:ack:1-0"
        ]
    )
    assert stored["acked_by"] == "reviewer"
    assert stored["acked_at"] == "2026-06-03T00:01:00Z"


@pytest.mark.asyncio
async def test_read_next_messages_skips_acked_messages(monkeypatch) -> None:
    fake_client = _FakeRedisClient()

    async def _get_client():
        return fake_client

    monkeypatch.setattr(mailbox.redis_service, "get_client", _get_client)

    first = await mailbox.send_message(
        run_id="run-1",
        thread_id="thread-1",
        project_id="project-1",
        sender="facilitator",
        recipient="reviewer",
        kind=MessageKind.TEXT,
        message_type=MessageType.TEXT,
        idempotency_key="text-acked",
        text="Acked text.",
        created_at="2026-06-03T00:00:00Z",
    )
    second = await mailbox.send_message(
        run_id="run-1",
        thread_id="thread-1",
        project_id="project-1",
        sender="facilitator",
        recipient="reviewer",
        kind=MessageKind.TEXT,
        message_type=MessageType.TEXT,
        idempotency_key="text-unread",
        text="Unread text.",
        created_at="2026-06-03T00:00:01Z",
    )
    await mailbox.ack_message(
        project_id="project-1",
        thread_id="thread-1",
        agent_name="reviewer",
        message_id=first.id,
        acked_by="reviewer",
    )

    messages = await mailbox.read_next_messages(
        project_id="project-1",
        thread_id="thread-1",
        agent_name="reviewer",
    )

    assert [message.id for message in messages] == [second.id]


@pytest.mark.asyncio
async def test_ack_message_rejects_wrong_recipient(monkeypatch) -> None:
    fake_client = _FakeRedisClient()

    async def _get_client():
        return fake_client

    monkeypatch.setattr(mailbox.redis_service, "get_client", _get_client)
    sent = await mailbox.send_message(
        run_id="run-1",
        thread_id="thread-1",
        project_id="project-1",
        sender="facilitator",
        recipient="reviewer",
        kind=MessageKind.TEXT,
        message_type=MessageType.TEXT,
        idempotency_key="wrong-recipient",
        text="Private.",
        created_at="2026-06-03T00:00:00Z",
    )

    with pytest.raises(ValueError, match="acked_by must match agent_name"):
        await mailbox.ack_message(
            project_id="project-1",
            thread_id="thread-1",
            agent_name="reviewer",
            message_id=sent.id,
            acked_by="researcher",
        )

    with pytest.raises(ValueError, match="message does not exist"):
        await mailbox.ack_message(
            project_id="project-1",
            thread_id="thread-1",
            agent_name="researcher",
            message_id=sent.id,
            acked_by="researcher",
        )


@pytest.mark.asyncio
async def test_ack_message_is_idempotent(monkeypatch) -> None:
    fake_client = _FakeRedisClient()

    async def _get_client():
        return fake_client

    monkeypatch.setattr(mailbox.redis_service, "get_client", _get_client)
    sent = await mailbox.send_message(
        run_id="run-1",
        thread_id="thread-1",
        project_id="project-1",
        sender="facilitator",
        recipient="reviewer",
        kind=MessageKind.TEXT,
        message_type=MessageType.TEXT,
        idempotency_key="ack-twice",
        text="Ack once.",
        created_at="2026-06-03T00:00:00Z",
    )

    first = await mailbox.ack_message(
        project_id="project-1",
        thread_id="thread-1",
        agent_name="reviewer",
        message_id=sent.id,
        acked_by="reviewer",
        acked_at="2026-06-03T00:01:00Z",
    )
    second = await mailbox.ack_message(
        project_id="project-1",
        thread_id="thread-1",
        agent_name="reviewer",
        message_id=sent.id,
        acked_by="reviewer",
        acked_at="2026-06-03T00:02:00Z",
    )

    assert first.already_acked is False
    assert second.already_acked is True
    assert second.acked_at == "2026-06-03T00:01:00Z"


@pytest.mark.asyncio
async def test_ack_message_is_atomic_under_concurrent_duplicates(monkeypatch) -> None:
    fake_client = _FakeRedisClient()

    async def _get_client():
        return fake_client

    monkeypatch.setattr(mailbox.redis_service, "get_client", _get_client)
    sent = await mailbox.send_message(
        run_id="run-1",
        thread_id="thread-1",
        project_id="project-1",
        sender="facilitator",
        recipient="reviewer",
        kind=MessageKind.TEXT,
        message_type=MessageType.TEXT,
        idempotency_key="concurrent-ack",
        text="Ack concurrently.",
        created_at="2026-06-03T00:00:00Z",
    )

    first, second = await asyncio.gather(
        mailbox.ack_message(
            project_id="project-1",
            thread_id="thread-1",
            agent_name="reviewer",
            message_id=sent.id,
            acked_by="reviewer",
            acked_at="2026-06-03T00:01:00Z",
        ),
        mailbox.ack_message(
            project_id="project-1",
            thread_id="thread-1",
            agent_name="reviewer",
            message_id=sent.id,
            acked_by="reviewer",
            acked_at="2026-06-03T00:02:00Z",
        ),
    )

    assert sorted([first.already_acked, second.already_acked]) == [False, True]
    assert first.acked_at == second.acked_at == "2026-06-03T00:01:00Z"


@pytest.mark.asyncio
async def test_dead_letter_message_moves_to_deadletter_lane(monkeypatch) -> None:
    fake_client = _FakeRedisClient()

    async def _get_client():
        return fake_client

    monkeypatch.setattr(mailbox.redis_service, "get_client", _get_client)

    message = MailboxMessage(
        id="1-0",
        run_id="run-1",
        thread_id="thread-1",
        project_id="project-1",
        sender="researcher",
        recipient="reviewer",
        kind=MessageKind.TEXT,
        type=MessageType.TEXT,
        idempotency_key="message-1",
        text="Poison.",
        created_at="2026-06-03T00:00:00Z",
    )

    dead_lettered = await mailbox.dead_letter_message(
        message=message,
        reason="handler_error",
        dead_lettered_at="2026-06-03T00:02:00Z",
    )

    assert dead_lettered.id == "1-0"
    assert dead_lettered.dead_letter_reason == "handler_error"
    assert fake_client.xadd_calls[-1]["key"] == (
        "sc_v2:project:project-1:thread:thread-1:mailbox:reviewer:deadletter"
    )


@pytest.mark.asyncio
async def test_read_next_messages_dead_letters_poison_after_max_attempts(
    monkeypatch,
) -> None:
    fake_client = _FakeRedisClient()

    async def _get_client():
        return fake_client

    monkeypatch.setattr(mailbox.redis_service, "get_client", _get_client)
    sent = await mailbox.send_message(
        run_id="run-1",
        thread_id="thread-1",
        project_id="project-1",
        sender="facilitator",
        recipient="reviewer",
        kind=MessageKind.TEXT,
        message_type=MessageType.TEXT,
        idempotency_key="poison",
        text="Poison.",
        created_at="2026-06-03T00:00:00Z",
    )

    first = await mailbox.read_next_messages(
        project_id="project-1",
        thread_id="thread-1",
        agent_name="reviewer",
        max_delivery_attempts=1,
    )
    second = await mailbox.read_next_messages(
        project_id="project-1",
        thread_id="thread-1",
        agent_name="reviewer",
        max_delivery_attempts=1,
    )

    assert [message.id for message in first] == [sent.id]
    assert second == []
    assert fake_client.xadd_calls[-1]["key"] == (
        "sc_v2:project:project-1:thread:thread-1:mailbox:reviewer:deadletter"
    )


@pytest.mark.asyncio
async def test_dead_letter_message_is_idempotent(monkeypatch) -> None:
    fake_client = _FakeRedisClient()

    async def _get_client():
        return fake_client

    monkeypatch.setattr(mailbox.redis_service, "get_client", _get_client)

    message = MailboxMessage(
        id="1-0",
        run_id="run-1",
        thread_id="thread-1",
        project_id="project-1",
        sender="researcher",
        recipient="reviewer",
        kind=MessageKind.TEXT,
        type=MessageType.TEXT,
        idempotency_key="message-1",
        text="Poison.",
        created_at="2026-06-03T00:00:00Z",
    )

    first = await mailbox.dead_letter_message(
        message=message,
        reason="handler_error",
        dead_lettered_at="2026-06-03T00:02:00Z",
    )
    second = await mailbox.dead_letter_message(
        message=message,
        reason="different_reason",
        dead_lettered_at="2026-06-03T00:03:00Z",
    )

    assert first.dead_letter_reason == second.dead_letter_reason == "handler_error"
    assert getattr(first, "_was_dead_lettered") is True
    assert getattr(second, "_was_dead_lettered") is False
    deadletter_appends = [
        call for call in fake_client.xadd_calls if call["key"].endswith(":deadletter")
    ]
    assert len(deadletter_appends) == 1


@pytest.mark.asyncio
async def test_account_scoped_send_read_and_idempotency_are_isolated(
    monkeypatch,
) -> None:
    fake_client = _FakeRedisClient()

    async def _get_client():
        return fake_client

    monkeypatch.setattr(mailbox.redis_service, "get_client", _get_client)

    account_a = await mailbox.send_message(
        run_id="run-a",
        thread_id="thread-1",
        project_id="project-1",
        account_id="account-a",
        sender="researcher",
        recipient="reviewer",
        kind=MessageKind.TEXT,
        message_type=MessageType.TEXT,
        idempotency_key="same-key",
        text="Account A message.",
        created_at="2026-06-03T00:00:00Z",
    )
    account_b = await mailbox.send_message(
        run_id="run-b",
        thread_id="thread-1",
        project_id="project-1",
        account_id="account-b",
        sender="researcher",
        recipient="reviewer",
        kind=MessageKind.TEXT,
        message_type=MessageType.TEXT,
        idempotency_key="same-key",
        text="Account B message.",
        created_at="2026-06-03T00:00:01Z",
    )

    assert account_a.account_id == "account-a"
    assert account_b.account_id == "account-b"
    assert len(fake_client.xadd_calls) == 2
    assert fake_client.xadd_calls[0]["key"] == (
        "sc_v2:account:account-a:project:project-1:thread:thread-1:"
        "mailbox:reviewer:text"
    )
    assert fake_client.xadd_calls[1]["key"] == (
        "sc_v2:account:account-b:project:project-1:thread:thread-1:"
        "mailbox:reviewer:text"
    )

    account_a_messages = await mailbox.read_messages(
        project_id="project-1",
        thread_id="thread-1",
        account_id="account-a",
        agent_name="reviewer",
        kind=MessageKind.TEXT,
    )
    account_b_messages = await mailbox.read_messages(
        project_id="project-1",
        thread_id="thread-1",
        account_id="account-b",
        agent_name="reviewer",
        kind=MessageKind.TEXT,
    )
    legacy_messages = await mailbox.read_messages(
        project_id="project-1",
        thread_id="thread-1",
        agent_name="reviewer",
        kind=MessageKind.TEXT,
    )

    assert [message.id for message in account_a_messages] == [account_a.id]
    assert [message.id for message in account_b_messages] == [account_b.id]
    assert legacy_messages == []


@pytest.mark.asyncio
async def test_account_scoped_ack_state_rejects_cross_account_message(
    monkeypatch,
) -> None:
    fake_client = _FakeRedisClient()

    async def _get_client():
        return fake_client

    monkeypatch.setattr(mailbox.redis_service, "get_client", _get_client)
    sent = await mailbox.send_message(
        run_id="run-a",
        thread_id="thread-1",
        project_id="project-1",
        account_id="account-a",
        sender="facilitator",
        recipient="reviewer",
        kind=MessageKind.TEXT,
        message_type=MessageType.TEXT,
        idempotency_key="ack-account-scope",
        text="Private account A message.",
        created_at="2026-06-03T00:00:00Z",
    )

    with pytest.raises(ValueError, match="message does not exist"):
        await mailbox.ack_message(
            project_id="project-1",
            thread_id="thread-1",
            account_id="account-b",
            agent_name="reviewer",
            message_id=sent.id,
            acked_by="reviewer",
        )

    acked = await mailbox.ack_message(
        project_id="project-1",
        thread_id="thread-1",
        account_id="account-a",
        agent_name="reviewer",
        message_id=sent.id,
        acked_by="reviewer",
        acked_at="2026-06-03T00:01:00Z",
    )

    assert acked.already_acked is False
    assert (
        "sc_v2:account:account-a:project:project-1:thread:thread-1:"
        "mailbox:reviewer:ack:1-0"
    ) in fake_client.values
    assert (
        "sc_v2:account:account-b:project:project-1:thread:thread-1:"
        "mailbox:reviewer:ack:1-0"
    ) not in fake_client.values


@pytest.mark.asyncio
async def test_account_scoped_dead_letter_state_uses_message_account(
    monkeypatch,
) -> None:
    fake_client = _FakeRedisClient()

    async def _get_client():
        return fake_client

    monkeypatch.setattr(mailbox.redis_service, "get_client", _get_client)
    sent = await mailbox.send_message(
        run_id="run-a",
        thread_id="thread-1",
        project_id="project-1",
        account_id="account-a",
        sender="facilitator",
        recipient="reviewer",
        kind=MessageKind.TEXT,
        message_type=MessageType.TEXT,
        idempotency_key="deadletter-account-scope",
        text="Poison account A message.",
        created_at="2026-06-03T00:00:00Z",
    )

    assert (
        await mailbox.get_message(
            project_id="project-1",
            thread_id="thread-1",
            account_id="account-b",
            agent_name="reviewer",
            message_id=sent.id,
        )
        is None
    )
    message = await mailbox.get_message(
        project_id="project-1",
        thread_id="thread-1",
        account_id="account-a",
        agent_name="reviewer",
        message_id=sent.id,
    )
    assert message is not None

    dead_lettered = await mailbox.dead_letter_message(
        message=message,
        reason="handler_error",
        dead_lettered_at="2026-06-03T00:02:00Z",
    )

    assert dead_lettered.account_id == "account-a"
    assert (
        "sc_v2:account:account-a:project:project-1:thread:thread-1:"
        "mailbox:reviewer:deadletter:1-0"
    ) in fake_client.values
    assert fake_client.xadd_calls[-1]["key"] == (
        "sc_v2:account:account-a:project:project-1:thread:thread-1:"
        "mailbox:reviewer:deadletter"
    )
