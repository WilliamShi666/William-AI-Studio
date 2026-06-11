"""Durable event-log helpers for Shadow Clone V2."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from services import redis as redis_service

from .constants import SHADOW_CLONE_V2_EVENT_STREAM_MAXLEN
from .keys import run_events_key
from .models import EventRecord, EventType


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _decode_event_payload(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value or "")


async def append_event(
    *,
    run_id: str,
    thread_id: str,
    project_id: str,
    sequence: int,
    event_type: EventType,
    payload: dict[str, Any] | None = None,
    activity_owner: str = "system",
    created_at: str | None = None,
    maxlen: int = SHADOW_CLONE_V2_EVENT_STREAM_MAXLEN,
) -> EventRecord:
    """Append one event to the run stream and return the stored record."""
    placeholder = EventRecord(
        id="pending",
        run_id=run_id,
        thread_id=thread_id,
        project_id=project_id,
        sequence=sequence,
        type=event_type,
        activity_owner=activity_owner,
        payload=dict(payload or {}),
        created_at=created_at or _now_iso(),
    )
    redis_client = await redis_service.get_client()
    stream_id = await redis_client.xadd(
        run_events_key(run_id),
        {"event": placeholder.model_dump_json()},
        maxlen=maxlen,
        approximate=True,
    )
    return placeholder.model_copy(update={"id": str(stream_id)})


async def read_events(
    run_id: str,
    *,
    start: str = "-",
    end: str = "+",
    count: int | None = None,
) -> list[EventRecord]:
    """Read event records from a run stream in append order."""
    redis_client = await redis_service.get_client()
    entries = await redis_client.xrange(run_events_key(run_id), min=start, max=end, count=count)
    records: list[EventRecord] = []
    for stream_id, fields in entries or []:
        raw_event = fields.get("event") if isinstance(fields, dict) else None
        if raw_event is None:
            continue
        record = EventRecord.model_validate_json(_decode_event_payload(raw_event))
        records.append(record.model_copy(update={"id": str(stream_id)}))
    return records
