from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import pytest

import agent.api as agent_api


def _v2_event(sequence: int, event_type: str, payload: dict[str, Any]):
    return SimpleNamespace(
        id=f"{sequence}-0",
        sequence=sequence,
        type=event_type,
        payload=payload,
        created_at=f"2026-06-03T00:{sequence:02d}:00+00:00",
    )


async def _verify_thread_access(*_args: Any) -> None:
    return None


@dataclass(frozen=True)
class _FakeResult:
    data: list[dict[str, Any]]
    count: int | None = None


class _FakeQuery:
    def __init__(self, rows: list[dict[str, Any]]):
        self._rows = rows
        self._filters: tuple[tuple[str, Any], ...] = ()
        self._range: tuple[int, int] | None = None
        self._count_requested = False

    def select(self, *_args: Any, **kwargs: Any) -> "_FakeQuery":
        self._count_requested = kwargs.get("count") == "exact"
        return self

    def eq(self, key: str, value: Any) -> "_FakeQuery":
        self._filters = (*self._filters, (key, value))
        return self

    def order(self, *_args: Any, **_kwargs: Any) -> "_FakeQuery":
        return self

    def range(self, start: int, end: int) -> "_FakeQuery":
        self._range = (start, end)
        return self

    async def execute(self) -> _FakeResult:
        matched_rows = [
            dict(row)
            for row in self._rows
            if all(row.get(key) == value for key, value in self._filters)
        ]
        if self._range is not None:
            start, end = self._range
            matched_rows = matched_rows[start : end + 1]
        count = len(matched_rows) if self._count_requested else None
        return _FakeResult(data=matched_rows, count=count)


class _FakeClient:
    def __init__(self, tables: dict[str, list[dict[str, Any]]]):
        self._tables = tables

    def table(self, table_name: str) -> _FakeQuery:
        return _FakeQuery(self._tables.get(table_name, []))

    def schema(self, _schema_name: str) -> "_FakeClient":
        return self


class _MutableFakeQuery(_FakeQuery):
    def __init__(self, table_name: str, tables: dict[str, list[dict[str, Any]]]):
        super().__init__(tables.setdefault(table_name, []))
        self._table_name = table_name
        self._tables = tables

    async def insert(self, payload: dict[str, Any]) -> _FakeResult:
        row = dict(payload)
        self._tables.setdefault(self._table_name, []).append(row)
        return _FakeResult(data=[row])

    async def update(self, payload: dict[str, Any]) -> _FakeResult:
        rows = self._tables.setdefault(self._table_name, [])
        for row in rows:
            if all(row.get(key) == value for key, value in self._filters):
                row.update(payload)
                return _FakeResult(data=[dict(row)])
        return _FakeResult(data=[])

    async def delete(self) -> _FakeResult:
        rows = self._tables.setdefault(self._table_name, [])
        self._tables[self._table_name] = [
            row for row in rows if not all(row.get(key) == value for key, value in self._filters)
        ]
        return _FakeResult(data=[])


class _MutableFakeClient(_FakeClient):
    def table(self, table_name: str) -> _MutableFakeQuery:
        return _MutableFakeQuery(table_name, self._tables)


class _FakeDB:
    def __init__(self, client: _FakeClient):
        self._client = client

    @property
    async def client(self) -> _FakeClient:
        return self._client


class _FakeSandbox:
    sandbox_id = "sandbox-1"

    def get_host(self, port: int) -> str:
        return f"sandbox-{port}.example.test"


@pytest.mark.asyncio
async def test_get_thread_messages_includes_assistant_events_fallback(monkeypatch):
    thread_id = "thread-1"
    client = _FakeClient(
        {
            "messages": [
                {
                    "message_id": "assistant-message-1",
                    "thread_id": thread_id,
                    "type": "assistant",
                    "role": "assistant",
                    "content": {"role": "assistant", "content": "first durable reply"},
                    "metadata": {},
                    "created_at": "2026-04-27T00:02:00Z",
                    "updated_at": "2026-04-27T00:02:00Z",
                }
            ],
            "events": [
                {
                    "id": "user-event-1",
                    "session_id": thread_id,
                    "author": "user",
                    "content": {"parts": [{"text": "first question"}]},
                    "timestamp": "2026-04-27T00:01:00Z",
                },
                {
                    "id": "user-event-2",
                    "session_id": thread_id,
                    "author": "user",
                    "content": {"parts": [{"text": "second question"}]},
                    "timestamp": "2026-04-27T00:03:00Z",
                },
                {
                    "id": "assistant-event-2",
                    "session_id": thread_id,
                    "author": "assistant",
                    "content": {"parts": [{"text": "second fallback reply"}]},
                    "timestamp": "2026-04-27T00:04:00Z",
                },
            ],
        }
    )
    monkeypatch.setattr(agent_api, "db", _FakeDB(client))
    monkeypatch.setattr(agent_api, "verify_thread_access", _verify_thread_access)

    result = await agent_api.get_thread_messages(thread_id, user_id="user-1", order="asc")

    message_ids = [message["message_id"] for message in result["messages"]]
    message_types = [message["type"] for message in result["messages"]]

    assert message_ids == [
        "user-event-1",
        "assistant-message-1",
        "user-event-2",
        "assistant-event-2",
    ]
    assert message_types == ["user", "assistant", "user", "assistant"]


@pytest.mark.asyncio
async def test_get_thread_messages_deduplicates_assistant_event_when_message_exists(monkeypatch):
    thread_id = "thread-1"
    client = _FakeClient(
        {
            "messages": [
                {
                    "message_id": "assistant-message-1",
                    "thread_id": thread_id,
                    "type": "assistant",
                    "role": "assistant",
                    "content": {"role": "assistant", "content": "durable reply"},
                    "metadata": {"event_id": "assistant-event-1"},
                    "created_at": "2026-04-27T00:02:00Z",
                    "updated_at": "2026-04-27T00:02:00Z",
                }
            ],
            "events": [
                {
                    "id": "assistant-event-1",
                    "session_id": thread_id,
                    "author": "assistant",
                    "content": {"parts": [{"text": "durable reply"}]},
                    "timestamp": "2026-04-27T00:02:00Z",
                }
            ],
        }
    )
    monkeypatch.setattr(agent_api, "db", _FakeDB(client))
    monkeypatch.setattr(agent_api, "verify_thread_access", _verify_thread_access)

    result = await agent_api.get_thread_messages(thread_id, user_id="user-1", order="asc")

    assert [message["message_id"] for message in result["messages"]] == [
        "assistant-message-1"
    ]


@pytest.mark.asyncio
async def test_get_thread_messages_synthesizes_shadow_clone_v2_final_output_from_event_log(
    monkeypatch,
):
    thread_id = "thread-v2"
    client = _FakeClient(
        {
            "messages": [],
            "events": [
                {
                    "id": "user-event-1",
                    "session_id": thread_id,
                    "author": "user",
                    "content": {"parts": [{"text": "use five subagents"}]},
                    "timestamp": "2026-06-03T00:01:00Z",
                }
            ],
            "agent_runs": [
                {
                    "agent_run_id": "run-v2",
                    "thread_id": thread_id,
                    "status": "completed",
                    "metadata": {"shadow_clone_mode": "v2"},
                    "created_at": "2026-06-03T00:00:00Z",
                }
            ],
        }
    )

    async def _fake_read_events(agent_run_id: str, *, start="-", end="+", count=None):
        assert agent_run_id == "run-v2"
        return [
            _v2_event(1, "run_started", {}),
            _v2_event(
                2,
                "run_completed",
                {"final_output": "MAIN_CHAT_PANEL_VISIBLE_FROM_V2_EVENT_LOG"},
            ),
        ]

    monkeypatch.setattr(agent_api, "db", _FakeDB(client))
    monkeypatch.setattr(agent_api, "verify_thread_access", _verify_thread_access)
    monkeypatch.setattr(
        agent_api.shadow_clone_v2_event_log,
        "read_events",
        _fake_read_events,
    )

    result = await agent_api.get_thread_messages(
        thread_id,
        user_id="user-1",
        order="asc",
    )

    assert [message["type"] for message in result["messages"]] == [
        "user",
        "assistant",
    ]
    assistant = result["messages"][1]
    assert assistant["message_id"] == "shadow-clone-v2-final-run-v2"
    assert (
        assistant["content"]
        == '{"role": "assistant", "content": "MAIN_CHAT_PANEL_VISIBLE_FROM_V2_EVENT_LOG"}'
    )
    assert '"source": "shadow_clone_v2_event_log_final_output"' in assistant["metadata"]




@pytest.mark.asyncio
async def test_get_thread_messages_deduplicates_shadow_clone_v2_final_fallback_when_table_final_exists(
    monkeypatch,
):
    thread_id = "thread-v2-dedupe-final"
    final_text = "SAME_FINAL_TEXT_FROM_V2"
    client = _FakeClient(
        {
            "messages": [
                {
                    "message_id": "assistant-table-final",
                    "thread_id": thread_id,
                    "type": "assistant",
                    "role": "assistant",
                    "content": {"role": "assistant", "content": final_text},
                    "metadata": {
                        "agent_run_id": "run-v2",
                        "shadow_clone_mode": "v2",
                        "stream_status": "complete",
                    },
                    "created_at": "2026-06-03T00:02:00+00:00",
                    "updated_at": "2026-06-03T00:02:00+00:00",
                }
            ],
            "events": [
                {
                    "id": "user-event-1",
                    "session_id": thread_id,
                    "author": "user",
                    "content": {"parts": [{"text": "use v2"}]},
                    "timestamp": "2026-06-03T00:01:00+00:00",
                }
            ],
            "agent_runs": [
                {
                    "agent_run_id": "run-v2",
                    "thread_id": thread_id,
                    "status": "completed",
                    "metadata": {"shadow_clone_mode": "v2"},
                    "created_at": "2026-06-03T00:00:00+00:00",
                }
            ],
        }
    )

    async def _fake_read_events(agent_run_id: str, *, start="-", end="+", count=None):
        assert agent_run_id == "run-v2"
        return [
            _v2_event(1, "run_started", {}),
            _v2_event(5, "run_completed", {"final_output": final_text}),
        ]

    monkeypatch.setattr(agent_api, "db", _FakeDB(client))
    monkeypatch.setattr(agent_api, "verify_thread_access", _verify_thread_access)
    monkeypatch.setattr(
        agent_api.shadow_clone_v2_event_log,
        "read_events",
        _fake_read_events,
    )

    result = await agent_api.get_thread_messages(thread_id, user_id="user-1", order="asc")

    assistant_messages = [
        message for message in result["messages"] if message["type"] == "assistant"
    ]
    assert [message["message_id"] for message in assistant_messages] == [
        "assistant-table-final"
    ]


@pytest.mark.asyncio
async def test_get_thread_messages_sorts_mixed_datetime_and_string_timestamps(
    monkeypatch,
):
    thread_id = "thread-v2-mixed-timestamps"
    client = _FakeClient(
        {
            "messages": [
                {
                    "message_id": "assistant-table-message",
                    "thread_id": thread_id,
                    "type": "assistant",
                    "role": "assistant",
                    "content": {"role": "assistant", "content": "table reply"},
                    "metadata": {},
                    "created_at": datetime(2026, 6, 3, 0, 2, tzinfo=timezone.utc),
                    "updated_at": datetime(2026, 6, 3, 0, 2, tzinfo=timezone.utc),
                }
            ],
            "events": [
                {
                    "id": "user-event-1",
                    "session_id": thread_id,
                    "author": "user",
                    "content": {"parts": [{"text": "use five subagents"}]},
                    "timestamp": "2026-06-03T00:01:00+00:00",
                }
            ],
            "agent_runs": [
                {
                    "agent_run_id": "run-v2",
                    "thread_id": thread_id,
                    "status": "completed",
                    "metadata": {"shadow_clone_mode": "v2"},
                    "created_at": datetime(2026, 6, 3, 0, 0, tzinfo=timezone.utc),
                }
            ],
        }
    )

    async def _fake_read_events(agent_run_id: str, *, start="-", end="+", count=None):
        assert agent_run_id == "run-v2"
        return [
            _v2_event(1, "run_started", {}),
            _v2_event(3, "run_completed", {"final_output": "v2 final reply"}),
        ]

    monkeypatch.setattr(agent_api, "db", _FakeDB(client))
    monkeypatch.setattr(agent_api, "verify_thread_access", _verify_thread_access)
    monkeypatch.setattr(
        agent_api.shadow_clone_v2_event_log,
        "read_events",
        _fake_read_events,
    )

    result = await agent_api.get_thread_messages(
        thread_id,
        user_id="user-1",
        order="asc",
    )

    assert [message["message_id"] for message in result["messages"]] == [
        "user-event-1",
        "assistant-table-message",
        "shadow-clone-v2-final-run-v2",
    ]


@pytest.mark.asyncio
async def test_fetch_thread_visible_message_count_uses_transcript_sources():
    thread_id = "thread-1"
    client = _FakeClient(
        {
            "messages": [
                {
                    "message_id": "assistant-message-1",
                    "thread_id": thread_id,
                    "type": "assistant",
                    "metadata": {"event_id": "assistant-event-1"},
                },
                {
                    "message_id": "tool-message-1",
                    "thread_id": thread_id,
                    "type": "tool",
                    "metadata": {},
                },
            ],
            "events": [
                {
                    "id": "user-event-1",
                    "session_id": thread_id,
                    "author": "user",
                },
                {
                    "id": "user-event-2",
                    "session_id": thread_id,
                    "author": "user",
                },
                {
                    "id": "assistant-event-1",
                    "session_id": thread_id,
                    "author": "assistant",
                },
                {
                    "id": "assistant-event-2",
                    "session_id": thread_id,
                    "author": "assistant",
                },
            ],
        }
    )

    count = await agent_api._fetch_thread_visible_message_count(client, thread_id)

    assert count == 5


@pytest.mark.asyncio
async def test_dedupes_assistant_event_by_timestamp_and_content_without_metadata(monkeypatch):
    thread_id = "thread-1"
    client = _FakeClient(
        {
            "messages": [
                {
                    "message_id": "durable-assistant-different-id",
                    "thread_id": thread_id,
                    "type": "assistant",
                    "role": "assistant",
                    "content": {"role": "assistant", "content": "same reply"},
                    "metadata": {},
                    "created_at": "2026-04-27T00:02:00Z",
                    "updated_at": "2026-04-27T00:02:00Z",
                }
            ],
            "events": [
                {
                    "id": "assistant-event-1",
                    "session_id": thread_id,
                    "author": "assistant",
                    "content": {"parts": [{"text": "same reply"}]},
                    "timestamp": "2026-04-27T00:02:00Z",
                }
            ],
        }
    )
    monkeypatch.setattr(agent_api, "db", _FakeDB(client))
    monkeypatch.setattr(agent_api, "verify_thread_access", _verify_thread_access)

    result = await agent_api.get_thread_messages(thread_id, user_id="user-1", order="asc")

    assert [message["message_id"] for message in result["messages"]] == [
        "durable-assistant-different-id"
    ]


@pytest.mark.asyncio
async def test_get_thread_messages_returns_claude_sdk_aggregate_as_renderable_assistant(monkeypatch):
    thread_id = "thread-claude"
    client = _FakeClient(
        {
            "messages": [
                {
                    "message_id": None,
                    "thread_id": thread_id,
                    "type": "assistant",
                    "role": "assistant",
                    "is_llm_message": True,
                    "content": {
                        "role": "assistant",
                        "content": "Persistent Claude answer",
                    },
                    "metadata": {
                        "source": "claude_sdk_aggregate",
                        "stream_status": "complete",
                        "thread_run_id": "run-claude",
                    },
                    "created_at": "2026-04-27T00:02:00Z",
                    "updated_at": "2026-04-27T00:02:00Z",
                }
            ],
            "events": [
                {
                    "id": "user-event-1",
                    "session_id": thread_id,
                    "author": "user",
                    "content": {"parts": [{"text": "question"}]},
                    "timestamp": "2026-04-27T00:01:00Z",
                }
            ],
        }
    )
    monkeypatch.setattr(agent_api, "db", _FakeDB(client))
    monkeypatch.setattr(agent_api, "verify_thread_access", _verify_thread_access)

    result = await agent_api.get_thread_messages(thread_id, user_id="user-1", order="asc")

    assistant = [message for message in result["messages"] if message["type"] == "assistant"][0]
    assert assistant["content"] == '{"role": "assistant", "content": "Persistent Claude answer"}'
    assert '"source": "claude_sdk_aggregate"' in assistant["metadata"]


@pytest.mark.asyncio
async def test_preserves_same_content_assistant_event_with_different_timestamp(monkeypatch):
    thread_id = "thread-1"
    client = _FakeClient(
        {
            "messages": [
                {
                    "message_id": "durable-assistant-1",
                    "thread_id": thread_id,
                    "type": "assistant",
                    "role": "assistant",
                    "content": {"role": "assistant", "content": "same reply"},
                    "metadata": {},
                    "created_at": "2026-04-27T00:02:00Z",
                    "updated_at": "2026-04-27T00:02:00Z",
                }
            ],
            "events": [
                {
                    "id": "assistant-event-2",
                    "session_id": thread_id,
                    "author": "assistant",
                    "content": {"parts": [{"text": "same reply"}]},
                    "timestamp": "2026-04-27T00:03:00Z",
                }
            ],
        }
    )
    monkeypatch.setattr(agent_api, "db", _FakeDB(client))
    monkeypatch.setattr(agent_api, "verify_thread_access", _verify_thread_access)

    result = await agent_api.get_thread_messages(thread_id, user_id="user-1", order="asc")

    assert [message["message_id"] for message in result["messages"]] == [
        "durable-assistant-1",
        "assistant-event-2",
    ]


@pytest.mark.asyncio
async def test_visible_message_count_does_not_overcount_table_user_when_user_events_exist():
    thread_id = "thread-1"
    client = _FakeClient(
        {
            "messages": [
                {
                    "message_id": "table-user-1",
                    "thread_id": thread_id,
                    "type": "user",
                    "metadata": {},
                },
                {
                    "message_id": "assistant-message-1",
                    "thread_id": thread_id,
                    "type": "assistant",
                    "metadata": {},
                },
            ],
            "events": [
                {
                    "id": "user-event-1",
                    "session_id": thread_id,
                    "author": "user",
                }
            ],
        }
    )

    count = await agent_api._fetch_thread_visible_message_count(client, thread_id)

    assert count == 2


@pytest.mark.asyncio
async def test_assistant_event_created_at_fallback_is_returned_and_sorted(monkeypatch):
    thread_id = "thread-1"
    client = _FakeClient(
        {
            "messages": [],
            "events": [
                {
                    "id": "assistant-event-created-at",
                    "session_id": thread_id,
                    "author": "assistant",
                    "content": [{"text": "created at reply"}],
                    "created_at": "2026-04-27T00:01:00Z",
                },
                {
                    "id": "assistant-event-createdAt",
                    "session_id": thread_id,
                    "author": "assistant",
                    "content": [{"content": "createdAt reply"}],
                    "createdAt": "2026-04-27T00:02:00Z",
                },
            ],
        }
    )
    monkeypatch.setattr(agent_api, "db", _FakeDB(client))
    monkeypatch.setattr(agent_api, "verify_thread_access", _verify_thread_access)

    result = await agent_api.get_thread_messages(thread_id, user_id="user-1", order="asc")

    assert [message["message_id"] for message in result["messages"]] == [
        "assistant-event-created-at",
        "assistant-event-createdAt",
    ]
    assert [message["created_at"] for message in result["messages"]] == [
        "2026-04-27T00:01:00Z",
        "2026-04-27T00:02:00Z",
    ]


@pytest.mark.asyncio
async def test_create_thread_creates_matching_adk_session(monkeypatch):
    client = _MutableFakeClient({"projects": [], "threads": []})
    session_calls: list[dict[str, str]] = []

    async def _create_sandbox(_sandbox_pass: str, _project_id: str) -> _FakeSandbox:
        return _FakeSandbox()

    async def _capture_session(
        _client: Any,
        user_id: str,
        session_id: str,
        app_name: str = "fufanmanus",
    ) -> None:
        session_calls.append(
            {
                "user_id": user_id,
                "session_id": session_id,
                "app_name": app_name,
            }
        )

    async def _to_thread(func: Any, *args: Any, **kwargs: Any) -> Any:
        return func(*args, **kwargs)

    monkeypatch.setattr(agent_api, "db", _FakeDB(client))
    monkeypatch.setattr(agent_api, "create_sandbox", _create_sandbox)
    monkeypatch.setattr(agent_api.asyncio, "to_thread", _to_thread)
    monkeypatch.setattr(agent_api, "_create_adk_session_if_not_exists", _capture_session)

    result = await agent_api.create_thread(name="E2E session test", user_id="user-1")

    assert result["thread_id"]
    assert result["project_id"]
    assert session_calls == [
        {
            "user_id": "user-1",
            "session_id": result["thread_id"],
            "app_name": "fufanmanus",
        }
    ]


@pytest.mark.asyncio
async def test_create_user_message_forwards_image_media_refs_to_adk_event(monkeypatch):
    thread_id = "thread-1"
    captured_event: dict[str, Any] = {}
    client = _FakeClient({"messages": [], "events": []})

    async def _capture_user_event(
        _client: Any,
        user_id: str,
        message_content: str,
        session_id: str,
        message_id: str,
        **kwargs: Any,
    ) -> str:
        captured_event.update(
            {
                "user_id": user_id,
                "message_content": message_content,
                "session_id": session_id,
                "message_id": message_id,
                "media_refs": kwargs.get("media_refs"),
            }
        )
        return "event-1"

    media_refs = [
        {
            "kind": "image",
            "path": "/workspace/demo.png",
            "mime_type": "image/png",
            "filename": "demo.png",
            "sha256": "abc123",
        }
    ]

    monkeypatch.setattr(agent_api, "db", _FakeDB(client))
    monkeypatch.setattr(agent_api, "verify_thread_access", _verify_thread_access)
    monkeypatch.setattr(agent_api, "_log_adk_user_message_event", _capture_user_event)

    result = await agent_api.create_message(
        thread_id,
        agent_api.MessageCreateRequest(
            type="user",
            content="Analyze this image",
            is_llm_message=False,
            media_refs=media_refs,
        ),
        user_id="user-1",
    )

    assert result["event_id"] == "event-1"
    assert captured_event["message_content"] == "Analyze this image"
    assert captured_event["session_id"] == thread_id
    assert captured_event["media_refs"] == media_refs
