"""Redis Stream mailbox helpers for Shadow Clone V2."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from typing import Any

from services import redis as redis_service

from .constants import (
    SHADOW_CLONE_V2_MAILBOX_STREAM_MAXLEN,
    SHADOW_CLONE_V2_STATE_TTL_SECONDS,
)
from .keys import agent_mailbox_key
from .models import MailboxMessage, MessageKind, MessageType
from .validation import require_key_part, require_non_empty_text

_ATOMIC_SEND_SCRIPT = """
local existing = redis.call('GET', KEYS[1])
if existing then
  return {0, existing}
end
local stream_id = redis.call(
  'XADD',
  KEYS[2],
  'MAXLEN',
  '~',
  ARGV[1],
  '*',
  'message',
  ARGV[2]
)
redis.call('SET', KEYS[1], stream_id, 'EX', ARGV[3])
return {1, stream_id}
"""


@dataclass(frozen=True)
class MailboxAckResult:
    """Durable acknowledgement result for one recipient/message pair."""

    project_id: str
    thread_id: str
    agent_name: str
    message_id: str
    acked_by: str
    acked_at: str
    read_at: str
    already_acked: bool


@dataclass(frozen=True)
class MailboxReadResult:
    """Unread mailbox delivery result plus state changes requiring projection."""

    messages: list[MailboxMessage]
    dead_lettered: list[MailboxMessage]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _lane_for_kind(kind: MessageKind) -> str:
    if kind == MessageKind.CONTROL:
        return "control"
    return "text"


def _decode_message_payload(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value or "")


def _decode_eval_flag(value: Any) -> int:
    if isinstance(value, bytes):
        return int(value.decode("utf-8", errors="replace"))
    return int(value)


def _split_idempotency_value(value: Any) -> tuple[str, str | None]:
    decoded = _decode_message_payload(value)
    stream_id, separator, marker = decoded.partition("|")
    return stream_id, marker if separator else None


def _thread_scope_prefix(
    *,
    project_id: str,
    thread_id: str,
    account_id: str | None = None,
) -> str:
    account = str(account_id or "").strip()
    project = require_key_part(project_id, "project_id")
    thread = require_key_part(thread_id, "thread_id")
    if account:
        return (
            "sc_v2:"
            f"account:{require_key_part(account, 'account_id')}:"
            f"project:{project}:thread:{thread}:"
        )
    return f"sc_v2:project:{project}:thread:{thread}:"


def _idempotency_key(
    *,
    project_id: str,
    thread_id: str,
    account_id: str | None = None,
    sender: str,
    recipient: str,
    idempotency_key: str,
) -> str:
    return _thread_scope_prefix(
        project_id=project_id,
        thread_id=thread_id,
        account_id=account_id,
    ) + (
        f"mailbox_idem:"
        f"{require_key_part(sender, 'sender')}:"
        f"{require_key_part(recipient, 'recipient')}:"
        f"{require_key_part(idempotency_key, 'idempotency_key')}"
    )


def _idempotency_key_for_message(message: MailboxMessage) -> str:
    return _idempotency_key(
        project_id=message.project_id,
        thread_id=message.thread_id,
        account_id=message.account_id,
        sender=message.sender,
        recipient=message.recipient,
        idempotency_key=message.idempotency_key,
    )


def _sent_projection_key(message: MailboxMessage) -> str:
    return f"{_idempotency_key_for_message(message)}:sent_projection"


def _ack_key(
    *,
    project_id: str,
    thread_id: str,
    account_id: str | None = None,
    agent_name: str,
    message_id: str,
) -> str:
    return _thread_scope_prefix(
        project_id=project_id,
        thread_id=thread_id,
        account_id=account_id,
    ) + (
        f"mailbox:{require_key_part(agent_name, 'agent_name')}:"
        f"ack:{require_key_part(message_id, 'message_id')}"
    )


def _delivery_key(
    *,
    project_id: str,
    thread_id: str,
    account_id: str | None = None,
    agent_name: str,
    message_id: str,
) -> str:
    return _thread_scope_prefix(
        project_id=project_id,
        thread_id=thread_id,
        account_id=account_id,
    ) + (
        f"mailbox:{require_key_part(agent_name, 'agent_name')}:"
        f"delivery:{require_key_part(message_id, 'message_id')}"
    )


def _dead_letter_key(
    *,
    project_id: str,
    thread_id: str,
    account_id: str | None = None,
    agent_name: str,
    message_id: str,
) -> str:
    return _thread_scope_prefix(
        project_id=project_id,
        thread_id=thread_id,
        account_id=account_id,
    ) + (
        f"mailbox:{require_key_part(agent_name, 'agent_name')}:"
        f"deadletter:{require_key_part(message_id, 'message_id')}"
    )


async def _read_message_from_lane(
    *,
    redis_client: Any,
    project_id: str,
    thread_id: str,
    account_id: str | None = None,
    agent_name: str,
    kind: MessageKind,
    message_id: str,
) -> MailboxMessage | None:
    entries = await redis_client.xrange(
        agent_mailbox_key(
            project_id=project_id,
            thread_id=thread_id,
            account_id=account_id,
            agent_name=agent_name,
            lane=_lane_for_kind(kind),
        ),
        min=message_id,
        max=message_id,
        count=1,
    )
    for stream_id, fields in entries or []:
        if str(stream_id) != str(message_id):
            continue
        raw_message = fields.get("message") if isinstance(fields, dict) else None
        if raw_message is None:
            continue
        message = MailboxMessage.model_validate_json(
            _decode_message_payload(raw_message)
        )
        return message.model_copy(update={"id": str(stream_id)})
    return None


async def _read_message_by_id(
    *,
    redis_client: Any,
    project_id: str,
    thread_id: str,
    account_id: str | None = None,
    agent_name: str,
    message_id: str,
) -> MailboxMessage | None:
    for kind in (MessageKind.CONTROL, MessageKind.TEXT):
        message = await _read_message_from_lane(
            redis_client=redis_client,
            project_id=project_id,
            thread_id=thread_id,
            account_id=account_id,
            agent_name=agent_name,
            kind=kind,
            message_id=message_id,
        )
        if message is not None:
            return message
    return None


async def _is_acked(redis_client: Any, *, message: MailboxMessage) -> bool:
    return bool(
        await redis_client.get(
            _ack_key(
                project_id=message.project_id,
                thread_id=message.thread_id,
                account_id=message.account_id,
                agent_name=message.recipient,
                message_id=message.id,
            )
        )
    )


async def _is_dead_lettered(redis_client: Any, *, message: MailboxMessage) -> bool:
    return bool(
        await redis_client.get(
            _dead_letter_key(
                project_id=message.project_id,
                thread_id=message.thread_id,
                account_id=message.account_id,
                agent_name=message.recipient,
                message_id=message.id,
            )
        )
    )


async def _increment_delivery_attempts(
    redis_client: Any,
    *,
    message: MailboxMessage,
) -> int:
    if hasattr(redis_client, "incr"):
        return int(
            await redis_client.incr(
                _delivery_key(
                    project_id=message.project_id,
                    thread_id=message.thread_id,
                    account_id=message.account_id,
                    agent_name=message.recipient,
                    message_id=message.id,
                )
            )
        )
    return message.delivery_attempts + 1


async def send_message(
    *,
    run_id: str,
    thread_id: str,
    project_id: str,
    account_id: str | None = None,
    sender: str,
    recipient: str,
    kind: MessageKind,
    message_type: MessageType,
    idempotency_key: str,
    summary: str | None = None,
    text: str | None = None,
    payload: dict[str, Any] | None = None,
    priority: int = 100,
    created_at: str | None = None,
    expires_at: str | None = None,
    maxlen: int = SHADOW_CLONE_V2_MAILBOX_STREAM_MAXLEN,
) -> MailboxMessage:
    """Send one durable mailbox message to a recipient stream atomically."""
    idem_key = _idempotency_key(
        project_id=project_id,
        thread_id=thread_id,
        account_id=account_id,
        sender=sender,
        recipient=recipient,
        idempotency_key=idempotency_key,
    )
    redis_client = await redis_service.get_client()
    placeholder = MailboxMessage(
        id="pending",
        run_id=run_id,
        thread_id=thread_id,
        project_id=project_id,
        account_id=account_id,
        sender=sender,
        recipient=recipient,
        kind=kind,
        type=message_type,
        idempotency_key=idempotency_key,
        summary=summary,
        text=text,
        payload=dict(payload or {}),
        priority=priority,
        created_at=created_at or _now_iso(),
        expires_at=expires_at,
    )
    mailbox_key = agent_mailbox_key(
        project_id=project_id,
        thread_id=thread_id,
        account_id=account_id,
        agent_name=recipient,
        lane=_lane_for_kind(kind),
    )

    if hasattr(redis_client, "eval"):
        created_flag, stream_id = await redis_client.eval(
            _ATOMIC_SEND_SCRIPT,
            2,
            idem_key,
            mailbox_key,
            int(maxlen),
            placeholder.model_dump_json(),
            SHADOW_CLONE_V2_STATE_TTL_SECONDS,
        )
        created = bool(_decode_eval_flag(created_flag))
        stream_id_text, projection_state = _split_idempotency_value(stream_id)
        message = await _read_message_from_lane(
            redis_client=redis_client,
            project_id=project_id,
            thread_id=thread_id,
            account_id=account_id,
            agent_name=recipient,
            kind=kind,
            message_id=stream_id_text,
        )
        if message is None:
            raise RuntimeError("mailbox idempotency key points to a missing message")
        object.__setattr__(message, "_was_created", created)
        object.__setattr__(message, "_sent_projected", projection_state == "sent_done")
        object.__setattr__(message, "_sent_projection_state", projection_state)
        return message

    reserved = await redis_client.set(
        idem_key,
        "pending",
        ex=SHADOW_CLONE_V2_STATE_TTL_SECONDS,
        nx=True,
    )
    if not reserved:
        existing_stream_id = await redis_client.get(idem_key)
        stream_id_text, projection_state = _split_idempotency_value(existing_stream_id)
        message = await _read_message_from_lane(
            redis_client=redis_client,
            project_id=project_id,
            thread_id=thread_id,
            account_id=account_id,
            agent_name=recipient,
            kind=kind,
            message_id=stream_id_text,
        )
        if message is None:
            raise RuntimeError("mailbox idempotency key points to a missing message")
        object.__setattr__(message, "_was_created", False)
        object.__setattr__(message, "_sent_projected", projection_state == "sent_done")
        object.__setattr__(message, "_sent_projection_state", projection_state)
        return message

    stream_id = await redis_client.xadd(
        mailbox_key,
        {"message": placeholder.model_dump_json()},
        maxlen=maxlen,
        approximate=True,
    )
    await redis_client.set(
        idem_key,
        str(stream_id),
        ex=SHADOW_CLONE_V2_STATE_TTL_SECONDS,
    )
    message = placeholder.model_copy(update={"id": str(stream_id)})
    object.__setattr__(message, "_was_created", True)
    object.__setattr__(message, "_sent_projected", False)
    return message


async def mark_sent_projected(message: MailboxMessage) -> None:
    """Mark a sent mailbox message as projected into the event log."""
    await mark_sent_projection_state(message, "sent_done")


async def mark_sent_projection_state(message: MailboxMessage, state: str) -> None:
    """Persist the current sent-message projection phase for retry recovery."""
    allowed_states = {"mailbox_sent", "wake_state", "sent_done"}
    if state not in allowed_states:
        raise ValueError(f"invalid sent projection state: {state}")
    redis_client = await redis_service.get_client()
    await redis_client.set(
        _idempotency_key_for_message(message),
        f"{message.id}|{state}",
        ex=SHADOW_CLONE_V2_STATE_TTL_SECONDS,
    )
    object.__setattr__(message, "_sent_projection_state", state)
    object.__setattr__(message, "_sent_projected", state == "sent_done")


async def claim_sent_projection(message: MailboxMessage) -> bool:
    """Atomically claim responsibility for MAILBOX_SENT/WAKE projection."""
    if bool(getattr(message, "_sent_projected", False)):
        return False
    redis_client = await redis_service.get_client()
    claimed = await redis_client.set(
        _sent_projection_key(message),
        message.id,
        ex=SHADOW_CLONE_V2_STATE_TTL_SECONDS,
        nx=True,
    )
    return bool(claimed)


async def release_sent_projection(message: MailboxMessage) -> None:
    """Release a sent-projection claim after projection failure."""
    redis_client = await redis_service.get_client()
    if hasattr(redis_client, "delete"):
        await redis_client.delete(_sent_projection_key(message))


async def read_messages(
    *,
    project_id: str,
    thread_id: str,
    account_id: str | None = None,
    agent_name: str,
    kind: MessageKind,
    start: str = "-",
    end: str = "+",
    count: int | None = None,
) -> list[MailboxMessage]:
    """Read messages from an agent mailbox lane."""
    redis_client = await redis_service.get_client()
    entries = await redis_client.xrange(
        agent_mailbox_key(
            project_id=project_id,
            thread_id=thread_id,
            account_id=account_id,
            agent_name=agent_name,
            lane=_lane_for_kind(kind),
        ),
        min=start,
        max=end,
        count=count,
    )
    messages: list[MailboxMessage] = []
    for stream_id, fields in entries or []:
        raw_message = fields.get("message") if isinstance(fields, dict) else None
        if raw_message is None:
            continue
        message = MailboxMessage.model_validate_json(
            _decode_message_payload(raw_message)
        )
        messages.append(message.model_copy(update={"id": str(stream_id)}))
    return messages


async def get_message(
    *,
    project_id: str,
    thread_id: str,
    account_id: str | None = None,
    agent_name: str,
    message_id: str,
) -> MailboxMessage | None:
    """Read one message by id from a recipient mailbox."""
    redis_client = await redis_service.get_client()
    return await _read_message_by_id(
        redis_client=redis_client,
        project_id=project_id,
        thread_id=thread_id,
        account_id=account_id,
        agent_name=agent_name,
        message_id=message_id,
    )


async def read_next_messages(
    *,
    project_id: str,
    thread_id: str,
    account_id: str | None = None,
    agent_name: str,
    count: int = 20,
    max_delivery_attempts: int = 3,
) -> list[MailboxMessage]:
    """Read next unread messages with backend-enforced control-lane priority."""
    result = await read_next_messages_with_dead_letters(
        project_id=project_id,
        thread_id=thread_id,
        account_id=account_id,
        agent_name=agent_name,
        count=count,
        max_delivery_attempts=max_delivery_attempts,
    )
    return result.messages


async def read_next_messages_with_dead_letters(
    *,
    project_id: str,
    thread_id: str,
    account_id: str | None = None,
    agent_name: str,
    count: int = 20,
    max_delivery_attempts: int = 3,
) -> MailboxReadResult:
    """Read next unread messages and return auto-dead-lettered transitions."""
    redis_client = await redis_service.get_client()
    control_messages = await read_messages(
        project_id=project_id,
        thread_id=thread_id,
        account_id=account_id,
        agent_name=agent_name,
        kind=MessageKind.CONTROL,
    )
    text_messages = await read_messages(
        project_id=project_id,
        thread_id=thread_id,
        account_id=account_id,
        agent_name=agent_name,
        kind=MessageKind.TEXT,
    )
    selected: list[MailboxMessage] = []
    dead_lettered_messages: list[MailboxMessage] = []
    for message in [*control_messages, *text_messages]:
        if len(selected) >= count:
            break
        if await _is_acked(redis_client, message=message):
            continue
        if await _is_dead_lettered(redis_client, message=message):
            continue
        attempts = await _increment_delivery_attempts(redis_client, message=message)
        attempted = message.model_copy(update={"delivery_attempts": attempts})
        if attempts > max_delivery_attempts:
            dead_lettered = await dead_letter_message(
                message=attempted,
                reason=f"max_delivery_attempts_exceeded:{max_delivery_attempts}",
            )
            if bool(getattr(dead_lettered, "_was_dead_lettered", True)):
                dead_lettered_messages.append(dead_lettered)
            continue
        selected.append(attempted)
    return MailboxReadResult(messages=selected, dead_lettered=dead_lettered_messages)


async def ack_message(
    *,
    project_id: str,
    thread_id: str,
    account_id: str | None = None,
    agent_name: str,
    message_id: str,
    acked_by: str,
    acked_at: str | None = None,
) -> MailboxAckResult:
    """Persist per-recipient ack/read state for one mailbox message."""
    if require_key_part(agent_name, "agent_name") != require_key_part(
        acked_by, "acked_by"
    ):
        raise ValueError("acked_by must match agent_name")

    redis_client = await redis_service.get_client()
    message = await _read_message_by_id(
        redis_client=redis_client,
        project_id=project_id,
        thread_id=thread_id,
        account_id=account_id,
        agent_name=agent_name,
        message_id=message_id,
    )
    if message is None:
        raise ValueError("message does not exist in recipient mailbox")
    if await _is_dead_lettered(redis_client, message=message):
        raise ValueError("dead-lettered message cannot be acked")

    ack_key = _ack_key(
        project_id=project_id,
        thread_id=thread_id,
        account_id=account_id,
        agent_name=agent_name,
        message_id=message_id,
    )
    now = acked_at or _now_iso()
    ack_payload = {
        "project_id": require_key_part(project_id, "project_id"),
        "thread_id": require_key_part(thread_id, "thread_id"),
        "agent_name": require_key_part(agent_name, "agent_name"),
        "message_id": require_key_part(message_id, "message_id"),
        "acked_by": require_key_part(acked_by, "acked_by"),
        "acked_at": now,
        "read_at": now,
    }
    ack_json = json.dumps(ack_payload, ensure_ascii=False, sort_keys=True)
    stored = await redis_client.set(
        ack_key,
        ack_json,
        ex=SHADOW_CLONE_V2_STATE_TTL_SECONDS,
        nx=True,
    )
    if not stored:
        existing_ack = await redis_client.get(ack_key)
        if not existing_ack:
            raise RuntimeError("ack was concurrently created but could not be read")
        payload = json.loads(_decode_message_payload(existing_ack))
        return MailboxAckResult(
            project_id=payload["project_id"],
            thread_id=payload["thread_id"],
            agent_name=payload["agent_name"],
            message_id=payload["message_id"],
            acked_by=payload["acked_by"],
            acked_at=payload["acked_at"],
            read_at=payload["read_at"],
            already_acked=True,
        )
    return MailboxAckResult(
        project_id=ack_payload["project_id"],
        thread_id=ack_payload["thread_id"],
        agent_name=ack_payload["agent_name"],
        message_id=ack_payload["message_id"],
        acked_by=ack_payload["acked_by"],
        acked_at=ack_payload["acked_at"],
        read_at=ack_payload["read_at"],
        already_acked=False,
    )


async def dead_letter_message(
    *,
    message: MailboxMessage,
    reason: str,
    dead_lettered_at: str | None = None,
    maxlen: int = SHADOW_CLONE_V2_MAILBOX_STREAM_MAXLEN,
) -> MailboxMessage:
    """Move an unhandled message to the recipient dead-letter lane."""
    safe_reason = require_non_empty_text(reason, "reason")
    dead_lettered = message.model_copy(
        update={
            "dead_letter_reason": safe_reason,
            "dead_lettered_at": dead_lettered_at or _now_iso(),
        }
    )
    redis_client = await redis_service.get_client()
    dead_letter_key = _dead_letter_key(
        project_id=message.project_id,
        thread_id=message.thread_id,
        account_id=message.account_id,
        agent_name=message.recipient,
        message_id=message.id,
    )
    stored = await redis_client.set(
        dead_letter_key,
        dead_lettered.model_dump_json(),
        ex=SHADOW_CLONE_V2_STATE_TTL_SECONDS,
        nx=True,
    )
    if not stored:
        existing = await redis_client.get(dead_letter_key)
        if not existing:
            raise RuntimeError(
                "dead-letter was concurrently created but could not be read"
            )
        existing_message = MailboxMessage.model_validate_json(
            _decode_message_payload(existing)
        )
        object.__setattr__(existing_message, "_was_dead_lettered", False)
        return existing_message
    await redis_client.xadd(
        agent_mailbox_key(
            project_id=message.project_id,
            thread_id=message.thread_id,
            account_id=message.account_id,
            agent_name=message.recipient,
            lane="deadletter",
        ),
        {"message": dead_lettered.model_dump_json()},
        maxlen=maxlen,
        approximate=True,
    )
    object.__setattr__(dead_lettered, "_was_dead_lettered", True)
    return dead_lettered
