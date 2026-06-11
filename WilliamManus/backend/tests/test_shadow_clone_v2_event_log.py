from __future__ import annotations

import json

import pytest

from agentscope_integration.shadow_clone_v2 import event_log
from agentscope_integration.shadow_clone_v2.models import EventType


class _FakeRedisClient:
    def __init__(self) -> None:
        self.xadd_calls = []
        self.stream_entries = []

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
        self.stream_entries.append((stream_id, dict(fields)))
        return stream_id

    async def xrange(self, key, min="-", max="+", count=None):  # noqa: A002
        return list(self.stream_entries[: count or None])


@pytest.mark.asyncio
async def test_append_event_writes_serialized_record_to_run_stream(monkeypatch) -> None:
    fake_client = _FakeRedisClient()

    async def _get_client():
        return fake_client

    monkeypatch.setattr(event_log.redis_service, "get_client", _get_client)

    record = await event_log.append_event(
        run_id="run-1",
        thread_id="thread-1",
        project_id="project-1",
        sequence=1,
        event_type=EventType.AGENT_IDLE,
        payload={"agent": "reviewer"},
        created_at="2026-05-31T00:00:00Z",
    )

    assert record.id == "1-0"
    assert fake_client.xadd_calls[0]["key"] == "sc_v2:run:run-1:events"
    serialized = fake_client.xadd_calls[0]["fields"]["event"]
    assert json.loads(serialized)["payload"] == {"agent": "reviewer"}


@pytest.mark.asyncio
async def test_read_events_replays_event_records(monkeypatch) -> None:
    fake_client = _FakeRedisClient()

    async def _get_client():
        return fake_client

    monkeypatch.setattr(event_log.redis_service, "get_client", _get_client)

    await event_log.append_event(
        run_id="run-1",
        thread_id="thread-1",
        project_id="project-1",
        sequence=1,
        event_type=EventType.AGENT_IDLE,
        payload={"agent": "reviewer"},
        created_at="2026-05-31T00:00:00Z",
    )

    events = await event_log.read_events("run-1")

    assert len(events) == 1
    assert events[0].type == EventType.AGENT_IDLE
    assert events[0].payload == {"agent": "reviewer"}
