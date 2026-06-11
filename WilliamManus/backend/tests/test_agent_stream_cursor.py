import asyncio
import json
from types import SimpleNamespace

import pytest

from agent import api as agent_api
from agentscope_integration.shadow_clone_v2.models import EventType


class _DummyDB:
    @property
    def client(self):
        async def _client():
            return object()

        return _client()


class _AttemptQuery:
    def __init__(self, rows):
        self._rows = rows
        self._filters = {}

    def select(self, *_args, **_kwargs):
        return self

    def eq(self, field, value):
        self._filters[field] = value
        return self

    async def execute(self):
        matched_rows = [
            dict(row)
            for row in self._rows
            if all(row.get(key) == value for key, value in self._filters.items())
        ]
        return type("_Result", (), {"data": matched_rows})()


class _AttemptClient:
    def __init__(self, rows):
        self._rows = list(rows)

    def table(self, table_name):
        assert table_name == "regular_run_attempts"
        return _AttemptQuery(self._rows)


class _SequencedAttemptClient:
    def __init__(self, row_sequences):
        self._row_sequences = [list(rows) for rows in row_sequences]
        self._index = 0

    def table(self, table_name):
        assert table_name == "regular_run_attempts"
        if self._index < len(self._row_sequences):
            rows = self._row_sequences[self._index]
            self._index += 1
        else:
            rows = self._row_sequences[-1]
        return _AttemptQuery(rows)


class _StaticClientDB:
    def __init__(self, client):
        self._client = client

    @property
    def client(self):
        async def _client():
            return self._client

        return _client()


class _AlwaysConnectedRequest:
    async def is_disconnected(self) -> bool:
        return False


class _ImmediatelyDisconnectedRequest:
    async def is_disconnected(self) -> bool:
        return True


class _DisconnectAfterChecksRequest:
    def __init__(self, *, disconnect_after: int):
        self._disconnect_after = disconnect_after
        self._checks = 0

    async def is_disconnected(self) -> bool:
        self._checks += 1
        return self._checks > self._disconnect_after


class _FakePubSub:
    def __init__(self, messages):
        self._messages = list(messages)

    async def subscribe(self, *_args, **_kwargs):
        return None

    async def unsubscribe(self, *_args, **_kwargs):
        return None

    async def close(self):
        return None

    async def get_message(self, *args, **kwargs):
        if self._messages:
            return self._messages.pop(0)
        await asyncio.sleep(0)
        return None


class _BrokenPubSub(_FakePubSub):
    def __init__(self, error: Exception):
        super().__init__(messages=[])
        self._error = error
        self._raised = False

    async def get_message(self, *args, **kwargs):
        if not self._raised:
            self._raised = True
            raise self._error
        return None


def _stream_data_payloads(chunks):
    data_lines = [
        line
        for chunk in chunks
        for line in chunk.splitlines()
        if line.startswith("data: ")
    ]
    return [json.loads(line[6:]) for line in data_lines]


def _v2_event(sequence, event_type, payload, *, event_id=None):
    return SimpleNamespace(
        id=event_id or f"{sequence}-0",
        sequence=sequence,
        type=event_type,
        payload=payload,
        created_at=f"2026-06-02T00:{sequence:02d}:00+00:00",
    )


@pytest.mark.asyncio
async def test_stream_agent_run_v2_uses_event_log_replay_path(monkeypatch):
    monkeypatch.setattr(agent_api, "db", _DummyDB())

    async def _fake_auth(*args, **kwargs):
        return "user-1"

    async def _fake_access(*args, **kwargs):
        return {
            "agent_run_id": "run-v2-stream",
            "status": "completed",
            "thread_id": "thread-1",
            "metadata": {"shadow_clone_mode": "v2"},
        }

    async def _forbidden_lrange(*_args, **_kwargs):
        raise AssertionError("V2 stream must not read legacy response list")

    async def _fake_read_events(run_id, *, start="-", end="+", count=None):
        assert run_id == "run-v2-stream"
        assert start == "-"
        return [
            _v2_event(0, EventType.RUN_STARTED, {"model_key": "frontend-model"}),
            _v2_event(
                1,
                EventType.TASK_CREATED,
                {
                    "task": {
                        "id": "task-1",
                        "task_id": "task-1",
                        "run_id": "run-v2-stream",
                        "thread_id": "thread-1",
                        "subject": "Analyst",
                        "description": "Analyze.",
                        "status": "pending",
                        "blocked_by": [],
                        "blocks": [],
                        "metadata": {"agent_name": "analyst"},
                        "plan_revision": 1,
                        "version": 1,
                        "error": {},
                        "created_at": "2026-06-02T00:01:00+00:00",
                        "updated_at": "2026-06-02T00:01:00+00:00",
                    }
                },
            ),
            _v2_event(2, EventType.RUN_COMPLETED, {"final_output": "Done."}),
        ]

    monkeypatch.setattr(agent_api, "get_user_id_from_stream_auth", _fake_auth)
    monkeypatch.setattr(agent_api, "get_agent_run_with_access_check", _fake_access)
    monkeypatch.setattr(agent_api.redis, "lrange", _forbidden_lrange)
    monkeypatch.setattr(
        agent_api.shadow_clone_v2_event_log, "read_events", _fake_read_events
    )

    response = await agent_api.stream_agent_run(
        agent_run_id="run-v2-stream",
        token="token",
        request=_ImmediatelyDisconnectedRequest(),
        from_index=0,
    )

    chunks = []
    async for item in response.body_iterator:
        chunks.append(item.decode() if isinstance(item, bytes) else item)

    payloads = _stream_data_payloads(chunks)
    projection_payloads = [
        payload
        for payload in payloads
        if payload["type"] == "shadow_clone_v2_projection"
    ]
    assert [payload["event_index"] for payload in projection_payloads] == [0, 1, 2]
    assert projection_payloads[-1]["projection"]["status"] == "completed"
    assert projection_payloads[-1]["projection"]["final_output"]["content"] == "Done."
    assert projection_payloads[-1]["metadata"]["stream_source"] == (
        "shadow_clone_v2_event_log"
    )


@pytest.mark.asyncio
async def test_stream_agent_run_v2_replays_user_facing_main_agent_assistant_messages(
    monkeypatch,
):
    monkeypatch.setattr(agent_api, "db", _DummyDB())

    async def _fake_auth(*args, **kwargs):
        return "user-1"

    async def _fake_access(*args, **kwargs):
        return {
            "agent_run_id": "run-v2-assistant-replay",
            "status": "running",
            "thread_id": "thread-1",
            "metadata": {"shadow_clone_mode": "v2"},
        }

    async def _fake_read_events(run_id, *, start="-", end="+", count=None):
        assert run_id == "run-v2-assistant-replay"
        return [
            _v2_event(0, EventType.RUN_STARTED, {}, event_id="1000-0"),
            _v2_event(1, EventType.TASK_CREATED, {"task_id": "task-1"}, event_id="1001-0"),
        ]

    response_payloads = [
        {
            "sequence": 9,
            "message_id": "shadow-clone-v2-planning-run-v2-assistant-replay",
            "thread_id": "thread-1",
            "project_id": "project-1",
            "type": "assistant",
            "is_llm_message": True,
            "content": json.dumps(
                {
                    "role": "assistant",
                    "content": "MAIN_PLANNING_NATURAL_LANGUAGE_OK_TEST",
                },
                ensure_ascii=False,
            ),
            "metadata": json.dumps(
                {
                    "stream_status": "complete",
                    "activity_owner": "main_agent",
                    "ui_phase": "planning",
                    "phase_reason": "shadow_clone_v2_main_agent_planning_output",
                },
                ensure_ascii=False,
            ),
        },
        {
            "sequence": 10,
            "thread_id": "thread-1",
            "type": "status",
            "status": "completed",
        },
    ]

    lrange_calls = []

    async def _fake_lrange(key, start, end):
        lrange_calls.append((key, start, end))
        assert key == agent_api.build_response_list_key("run-v2-assistant-replay")
        return [json.dumps(payload, ensure_ascii=False) for payload in response_payloads]

    monkeypatch.setattr(agent_api, "get_user_id_from_stream_auth", _fake_auth)
    monkeypatch.setattr(agent_api, "get_agent_run_with_access_check", _fake_access)
    monkeypatch.setattr(agent_api.redis, "lrange", _fake_lrange)
    monkeypatch.setattr(
        agent_api.shadow_clone_v2_event_log, "read_events", _fake_read_events
    )

    response = await agent_api.stream_agent_run(
        agent_run_id="run-v2-assistant-replay",
        token="token",
        request=_ImmediatelyDisconnectedRequest(),
        from_index=0,
    )

    chunks = []
    async for item in response.body_iterator:
        chunks.append(item.decode() if isinstance(item, bytes) else item)

    payloads = _stream_data_payloads(chunks)
    projection_payloads = [
        payload
        for payload in payloads
        if payload["type"] == "shadow_clone_v2_projection"
    ]
    assistant_payloads = [
        payload for payload in payloads if payload.get("type") == "assistant"
    ]

    assert [payload["event_index"] for payload in projection_payloads] == [0, 1]
    assert len(assistant_payloads) == 1
    assistant_payload = assistant_payloads[0]
    assert json.loads(assistant_payload["content"])["content"] == (
        "MAIN_PLANNING_NATURAL_LANGUAGE_OK_TEST"
    )
    assert json.loads(assistant_payload["metadata"])["phase_reason"] == (
        "shadow_clone_v2_main_agent_planning_output"
    )
    assert "event_index" not in assistant_payload
    assert lrange_calls == [(agent_api.build_response_list_key("run-v2-assistant-replay"), 0, -1)]


@pytest.mark.asyncio
async def test_stream_agent_run_v2_poll_emits_late_user_facing_assistant_without_new_projection_event(
    monkeypatch,
):
    monkeypatch.setattr(agent_api, "db", _DummyDB())
    monkeypatch.setattr(agent_api, "AGENT_STREAM_QUEUE_POLL_INTERVAL_SECONDS", 0)

    async def _fake_auth(*args, **kwargs):
        return "user-1"

    async def _fake_access(*args, **kwargs):
        return {
            "agent_run_id": "run-v2-late-assistant",
            "status": "running",
            "thread_id": "thread-1",
            "metadata": {"shadow_clone_mode": "v2"},
        }

    async def _fake_read_events(run_id, *, start="-", end="+", count=None):
        return [_v2_event(0, EventType.RUN_STARTED, {}, event_id="1000-0")]

    lrange_call_count = {"count": 0}

    async def _fake_lrange(key, start, end):
        lrange_call_count["count"] += 1
        if lrange_call_count["count"] == 1:
            return []
        assistant_payload = {
            "sequence": 9,
            "message_id": "shadow-clone-v2-planning-late",
            "thread_id": "thread-1",
            "project_id": "project-1",
            "type": "assistant",
            "is_llm_message": True,
            "content": json.dumps(
                {
                    "role": "assistant",
                    "content": "MAIN_PLANNING_LATE_VISIBLE_WITHOUT_NEW_EVENT",
                },
                ensure_ascii=False,
            ),
            "metadata": json.dumps(
                {
                    "stream_status": "complete",
                    "activity_owner": "main_agent",
                    "ui_phase": "planning",
                    "phase_reason": "shadow_clone_v2_main_agent_planning_output",
                },
                ensure_ascii=False,
            ),
        }
        return [json.dumps(assistant_payload, ensure_ascii=False)]

    monkeypatch.setattr(agent_api, "get_user_id_from_stream_auth", _fake_auth)
    monkeypatch.setattr(agent_api, "get_agent_run_with_access_check", _fake_access)
    monkeypatch.setattr(agent_api.redis, "lrange", _fake_lrange)
    monkeypatch.setattr(
        agent_api.shadow_clone_v2_event_log, "read_events", _fake_read_events
    )

    response = await agent_api.stream_agent_run(
        agent_run_id="run-v2-late-assistant",
        token="token",
        request=_DisconnectAfterChecksRequest(disconnect_after=2),
        from_index=0,
    )

    chunks = []
    async for item in response.body_iterator:
        chunks.append(item.decode() if isinstance(item, bytes) else item)

    payloads = _stream_data_payloads(chunks)
    assert [
        payload.get("event_index")
        for payload in payloads
        if payload.get("type") == "shadow_clone_v2_projection"
    ] == [0]
    assistant_payloads = [
        payload for payload in payloads if payload.get("type") == "assistant"
    ]
    assert len(assistant_payloads) == 1
    assert json.loads(assistant_payloads[0]["content"])["content"] == (
        "MAIN_PLANNING_LATE_VISIBLE_WITHOUT_NEW_EVENT"
    )


def _v2_main_agent_planning_response_payload(content: str, *, message_id: str):
    return {
        "sequence": 9,
        "message_id": message_id,
        "thread_id": "thread-1",
        "project_id": "project-1",
        "type": "assistant",
        "is_llm_message": True,
        "content": json.dumps(
            {"role": "assistant", "content": content},
            ensure_ascii=False,
        ),
        "metadata": json.dumps(
            {
                "stream_status": "complete",
                "activity_owner": "main_agent",
                "ui_phase": "planning",
                "phase_reason": "shadow_clone_v2_main_agent_planning_output",
            },
            ensure_ascii=False,
        ),
    }


def _v2_main_agent_planning_chunk_payload(content: str, *, sequence: int = 9):
    return {
        "sequence": sequence,
        "message_id": None,
        "thread_id": "thread-1",
        "project_id": "project-1",
        "type": "assistant",
        "is_llm_message": True,
        "content": json.dumps(
            {"role": "assistant", "content": content},
            ensure_ascii=False,
        ),
        "metadata": json.dumps(
            {
                "stream_status": "chunk",
                "activity_owner": "main_agent",
                "ui_phase": "planning",
                "phase_reason": "shadow_clone_v2_main_agent_planning_output",
            },
            ensure_ascii=False,
        ),
    }


@pytest.mark.asyncio
async def test_stream_agent_run_v2_replays_main_agent_natural_language_chunk(
    monkeypatch,
):
    monkeypatch.setattr(agent_api, "db", _DummyDB())

    async def _fake_auth(*args, **kwargs):
        return "user-1"

    async def _fake_access(*args, **kwargs):
        return {
            "agent_run_id": "run-v2-main-chunk",
            "status": "running",
            "thread_id": "thread-1",
            "metadata": {"shadow_clone_mode": "v2"},
        }

    async def _fake_read_events(run_id, *, start="-", end="+", count=None):
        assert run_id == "run-v2-main-chunk"
        return [_v2_event(0, EventType.RUN_STARTED, {}, event_id="1000-0")]

    async def _fake_lrange(key, start, end):
        assert key == agent_api.build_response_list_key("run-v2-main-chunk")
        return [
            json.dumps(
                _v2_main_agent_planning_chunk_payload(
                    "MAIN_STREAM_MARKER_chunk_visible_before_completion"
                ),
                ensure_ascii=False,
            )
        ]

    monkeypatch.setattr(agent_api, "get_user_id_from_stream_auth", _fake_auth)
    monkeypatch.setattr(agent_api, "get_agent_run_with_access_check", _fake_access)
    monkeypatch.setattr(agent_api.redis, "lrange", _fake_lrange)
    monkeypatch.setattr(
        agent_api.shadow_clone_v2_event_log, "read_events", _fake_read_events
    )

    response = await agent_api.stream_agent_run(
        agent_run_id="run-v2-main-chunk",
        token="token",
        request=_ImmediatelyDisconnectedRequest(),
        from_index=0,
    )

    chunks = []
    async for item in response.body_iterator:
        chunks.append(item.decode() if isinstance(item, bytes) else item)

    payloads = _stream_data_payloads(chunks)
    assistant_payloads = [
        payload for payload in payloads if payload.get("type") == "assistant"
    ]
    assert len(assistant_payloads) == 1
    assert json.loads(assistant_payloads[0]["content"])["content"] == (
        "MAIN_STREAM_MARKER_chunk_visible_before_completion"
    )
    assert json.loads(assistant_payloads[0]["metadata"])["stream_status"] == "chunk"


def _v2_subagent_activity_response_payload(
    content: str,
    *,
    subtask_id: str = "task-1",
    agent_name: str = "writer-agent-1",
    sequence: int = 12,
) -> dict:
    return {
        "type": "subagent_activity",
        "status": "running",
        "source": "shadow_clone_v2",
        "shadow_clone_mode": "v2",
        "thread_run_id": "thread-run-1",
        "agent_run_id": "run-v2-subagent-replay",
        "ui_phase": "subagents_running",
        "activity_owner": "shadow_clone",
        "phase_reason": "shadow_clone_v2_subagent_activity",
        "subtask_id": subtask_id,
        "sequence": sequence,
        "role": "writer",
        "agent_name": agent_name,
        "message_type": "assistant",
        "content": {
            "role": "assistant",
            "content": content,
        },
        "metadata": {
            "stream_status": "complete",
            "source": "shadow_clone_v2",
            "subtask_id": subtask_id,
            "agent_name": agent_name,
        },
        "created_at": "2026-06-02T00:03:00+00:00",
        "updated_at": "2026-06-02T00:03:00+00:00",
    }


@pytest.mark.asyncio
async def test_stream_agent_run_v2_replays_subagent_activity_natural_language_during_running_run(
    monkeypatch,
):
    monkeypatch.setattr(agent_api, "db", _DummyDB())

    async def _fake_auth(*args, **kwargs):
        return "user-1"

    async def _fake_access(*args, **kwargs):
        return {
            "agent_run_id": "run-v2-subagent-replay",
            "status": "running",
            "thread_id": "thread-1",
            "metadata": {"shadow_clone_mode": "v2"},
        }

    async def _fake_read_events(run_id, *, start="-", end="+", count=None):
        assert run_id == "run-v2-subagent-replay"
        return [
            _v2_event(0, EventType.RUN_STARTED, {}, event_id="1000-0"),
            _v2_event(
                1,
                EventType.TASK_CLAIMED,
                {"task_id": "task-1", "agent_name": "writer-agent-1"},
                event_id="1001-0",
            ),
        ]

    async def _fake_lrange(key, start, end):
        assert key == agent_api.build_response_list_key("run-v2-subagent-replay")
        return [
            json.dumps(
                _v2_subagent_activity_response_payload(
                    "SUB_STREAM_MARKER_writer_agent_1_visible_before_terminal"
                ),
                ensure_ascii=False,
            )
        ]

    monkeypatch.setattr(agent_api, "get_user_id_from_stream_auth", _fake_auth)
    monkeypatch.setattr(agent_api, "get_agent_run_with_access_check", _fake_access)
    monkeypatch.setattr(agent_api.redis, "lrange", _fake_lrange)
    monkeypatch.setattr(
        agent_api.shadow_clone_v2_event_log, "read_events", _fake_read_events
    )

    response = await agent_api.stream_agent_run(
        agent_run_id="run-v2-subagent-replay",
        token="token",
        request=_ImmediatelyDisconnectedRequest(),
        from_index=0,
    )

    chunks = []
    async for item in response.body_iterator:
        chunks.append(item.decode() if isinstance(item, bytes) else item)

    payloads = _stream_data_payloads(chunks)
    subagent_payloads = [
        payload for payload in payloads if payload.get("type") == "subagent_activity"
    ]
    assert len(subagent_payloads) == 1
    subagent_payload = subagent_payloads[0]
    assert subagent_payload["subtask_id"] == "task-1"
    assert subagent_payload["agent_name"] == "writer-agent-1"
    assert subagent_payload["metadata"]["stream_status"] == "complete"
    assert subagent_payload["content"]["content"] == (
        "SUB_STREAM_MARKER_writer_agent_1_visible_before_terminal"
    )
    assert "event_index" not in subagent_payload


@pytest.mark.asyncio
async def test_stream_agent_run_v2_replays_opening_subagent_activity_chunk_during_running_run(
    monkeypatch,
):
    monkeypatch.setattr(agent_api, "db", _DummyDB())

    async def _fake_auth(*args, **kwargs):
        return "user-1"

    async def _fake_access(*args, **kwargs):
        return {
            "agent_run_id": "run-v2-subagent-opening-replay",
            "status": "running",
            "thread_id": "thread-1",
            "metadata": {"shadow_clone_mode": "v2"},
        }

    async def _fake_read_events(run_id, *, start="-", end="+", count=None):
        assert run_id == "run-v2-subagent-opening-replay"
        return [
            _v2_event(0, EventType.RUN_STARTED, {}, event_id="1000-0"),
            _v2_event(
                1,
                EventType.TOOL_CALL_STARTED,
                {"task_id": "task-1", "agent_name": "writer-agent-1"},
                event_id="1001-0",
            ),
        ]

    async def _fake_lrange(key, start, end):
        assert key == agent_api.build_response_list_key(
            "run-v2-subagent-opening-replay"
        )
        return [
            json.dumps(
                _v2_subagent_activity_response_payload(
                    "SUB_STREAM_MARKER_writer_agent_1_opening_chunk_before_tool",
                    sequence=0,
                )
                | {
                    "agent_run_id": "run-v2-subagent-opening-replay",
                    "metadata": {
                        "stream_status": "chunk",
                        "opening_progress": True,
                        "source": "shadow_clone_v2",
                        "subtask_id": "task-1",
                        "agent_name": "writer-agent-1",
                    },
                },
                ensure_ascii=False,
            )
        ]

    monkeypatch.setattr(agent_api, "get_user_id_from_stream_auth", _fake_auth)
    monkeypatch.setattr(agent_api, "get_agent_run_with_access_check", _fake_access)
    monkeypatch.setattr(agent_api.redis, "lrange", _fake_lrange)
    monkeypatch.setattr(
        agent_api.shadow_clone_v2_event_log, "read_events", _fake_read_events
    )

    response = await agent_api.stream_agent_run(
        agent_run_id="run-v2-subagent-opening-replay",
        token="token",
        request=_ImmediatelyDisconnectedRequest(),
        from_index=0,
    )

    chunks = []
    async for item in response.body_iterator:
        chunks.append(item.decode() if isinstance(item, bytes) else item)

    payloads = _stream_data_payloads(chunks)
    subagent_payloads = [
        payload for payload in payloads if payload.get("type") == "subagent_activity"
    ]
    assert len(subagent_payloads) == 1
    subagent_payload = subagent_payloads[0]
    assert subagent_payload["metadata"]["stream_status"] == "chunk"
    assert subagent_payload["metadata"]["opening_progress"] is True
    assert subagent_payload["content"]["content"] == (
        "SUB_STREAM_MARKER_writer_agent_1_opening_chunk_before_tool"
    )
    assert "event_index" not in subagent_payload


@pytest.mark.asyncio
async def test_stream_agent_run_v2_terminal_event_cursor_replay_keeps_user_facing_assistant(
    monkeypatch,
):
    monkeypatch.setattr(agent_api, "db", _DummyDB())

    async def _fake_auth(*args, **kwargs):
        return "user-1"

    async def _fake_access(*args, **kwargs):
        return {
            "agent_run_id": "run-v2-terminal-cursor-assistant",
            "status": "completed",
            "thread_id": "thread-1",
            "metadata": {"shadow_clone_mode": "v2"},
        }

    async def _fake_read_events(run_id, *, start="-", end="+", count=None):
        return [
            _v2_event(0, EventType.RUN_STARTED, {}, event_id="1000-0"),
            _v2_event(10, EventType.RUN_COMPLETED, {}, event_id="1001-0"),
        ]

    async def _fake_lrange(key, start, end):
        return [
            json.dumps(
                _v2_main_agent_planning_response_payload(
                    "MAIN_TERMINAL_CURSOR_ASSISTANT_VISIBLE",
                    message_id="shadow-clone-v2-planning-terminal-cursor",
                ),
                ensure_ascii=False,
            )
        ]

    monkeypatch.setattr(agent_api, "get_user_id_from_stream_auth", _fake_auth)
    monkeypatch.setattr(agent_api, "get_agent_run_with_access_check", _fake_access)
    monkeypatch.setattr(agent_api.redis, "lrange", _fake_lrange)
    monkeypatch.setattr(
        agent_api.shadow_clone_v2_event_log, "read_events", _fake_read_events
    )

    response = await agent_api.stream_agent_run(
        agent_run_id="run-v2-terminal-cursor-assistant",
        token="token",
        request=_ImmediatelyDisconnectedRequest(),
        from_index=11,
        from_event_id="1001-0",
    )

    chunks = []
    async for item in response.body_iterator:
        chunks.append(item.decode() if isinstance(item, bytes) else item)

    payloads = _stream_data_payloads(chunks)
    assistant_payloads = [
        payload for payload in payloads if payload.get("type") == "assistant"
    ]
    assert len(assistant_payloads) == 1
    assert json.loads(assistant_payloads[0]["content"])["content"] == (
        "MAIN_TERMINAL_CURSOR_ASSISTANT_VISIBLE"
    )
    assert "event_index" not in assistant_payloads[0]
    assert payloads[-1]["type"] == "shadow_clone_v2_projection"
    assert payloads[-1]["metadata"]["terminal_summary"] is True


@pytest.mark.asyncio
async def test_stream_agent_run_v2_after_terminal_from_index_replay_keeps_user_facing_assistant(
    monkeypatch,
):
    monkeypatch.setattr(agent_api, "db", _DummyDB())

    async def _fake_auth(*args, **kwargs):
        return "user-1"

    async def _fake_access(*args, **kwargs):
        return {
            "agent_run_id": "run-v2-after-terminal-assistant",
            "status": "completed",
            "thread_id": "thread-1",
            "metadata": {"shadow_clone_mode": "v2"},
        }

    async def _fake_read_events(run_id, *, start="-", end="+", count=None):
        return [
            _v2_event(0, EventType.RUN_STARTED, {}, event_id="1000-0"),
            _v2_event(10, EventType.RUN_COMPLETED, {}, event_id="1001-0"),
        ]

    async def _fake_lrange(key, start, end):
        return [
            json.dumps(
                _v2_main_agent_planning_response_payload(
                    "MAIN_AFTER_TERMINAL_ASSISTANT_VISIBLE",
                    message_id="shadow-clone-v2-planning-after-terminal",
                ),
                ensure_ascii=False,
            )
        ]

    monkeypatch.setattr(agent_api, "get_user_id_from_stream_auth", _fake_auth)
    monkeypatch.setattr(agent_api, "get_agent_run_with_access_check", _fake_access)
    monkeypatch.setattr(agent_api.redis, "lrange", _fake_lrange)
    monkeypatch.setattr(
        agent_api.shadow_clone_v2_event_log, "read_events", _fake_read_events
    )

    response = await agent_api.stream_agent_run(
        agent_run_id="run-v2-after-terminal-assistant",
        token="token",
        request=_ImmediatelyDisconnectedRequest(),
        from_index=11,
    )

    chunks = []
    async for item in response.body_iterator:
        chunks.append(item.decode() if isinstance(item, bytes) else item)

    payloads = _stream_data_payloads(chunks)
    assistant_payloads = [
        payload for payload in payloads if payload.get("type") == "assistant"
    ]
    assert len(assistant_payloads) == 1
    assert json.loads(assistant_payloads[0]["content"])["content"] == (
        "MAIN_AFTER_TERMINAL_ASSISTANT_VISIBLE"
    )
    assert "event_index" not in assistant_payloads[0]
    assert payloads[-1]["type"] == "shadow_clone_v2_projection"
    assert payloads[-1]["metadata"]["terminal_summary"] is True


@pytest.mark.asyncio
async def test_stream_agent_run_v2_initial_replay_accepts_run_started_sequence_one(
    monkeypatch,
):
    monkeypatch.setattr(agent_api, "db", _DummyDB())

    async def _fake_auth(*args, **kwargs):
        return "user-1"

    async def _fake_access(*args, **kwargs):
        return {
            "agent_run_id": "run-v2-sequence-one",
            "status": "running",
            "thread_id": "thread-1",
            "metadata": {"shadow_clone_mode": "v2"},
        }

    async def _fake_read_events(run_id, *, start="-", end="+", count=None):
        return [
            _v2_event(1, EventType.RUN_STARTED, {"model_key": "frontend-model"}),
            _v2_event(2, EventType.RUN_COMPLETED, {"final_output": "Done."}),
        ]

    monkeypatch.setattr(agent_api, "get_user_id_from_stream_auth", _fake_auth)
    monkeypatch.setattr(agent_api, "get_agent_run_with_access_check", _fake_access)
    monkeypatch.setattr(
        agent_api.shadow_clone_v2_event_log, "read_events", _fake_read_events
    )

    response = await agent_api.stream_agent_run(
        agent_run_id="run-v2-sequence-one",
        token="token",
        request=_ImmediatelyDisconnectedRequest(),
        from_index=0,
    )

    chunks = []
    async for item in response.body_iterator:
        chunks.append(item.decode() if isinstance(item, bytes) else item)

    projection_payloads = [
        payload
        for payload in _stream_data_payloads(chunks)
        if payload["type"] == "shadow_clone_v2_projection"
    ]
    assert [payload["event_index"] for payload in projection_payloads] == [1, 2]
    assert projection_payloads[-1]["projection"]["status"] == "completed"


@pytest.mark.asyncio
async def test_stream_agent_run_v2_reconnect_from_cursor_skips_prior_events(
    monkeypatch,
):
    monkeypatch.setattr(agent_api, "db", _DummyDB())

    async def _fake_auth(*args, **kwargs):
        return "user-1"

    async def _fake_access(*args, **kwargs):
        return {
            "agent_run_id": "run-v2-reconnect",
            "status": "completed",
            "thread_id": "thread-1",
            "metadata": {"shadow_clone_mode": "v2"},
        }

    async def _fake_read_events(run_id, *, start="-", end="+", count=None):
        return [
            _v2_event(0, EventType.RUN_STARTED, {}),
            _v2_event(1, EventType.TASK_CREATED, {"task_id": "task-1"}),
            _v2_event(2, EventType.RUN_COMPLETED, {"final_output": "Done."}),
        ]

    monkeypatch.setattr(agent_api, "get_user_id_from_stream_auth", _fake_auth)
    monkeypatch.setattr(agent_api, "get_agent_run_with_access_check", _fake_access)
    monkeypatch.setattr(
        agent_api.shadow_clone_v2_event_log, "read_events", _fake_read_events
    )

    response = await agent_api.stream_agent_run(
        agent_run_id="run-v2-reconnect",
        token="token",
        request=_ImmediatelyDisconnectedRequest(),
        from_index=2,
    )

    chunks = []
    async for item in response.body_iterator:
        chunks.append(item.decode() if isinstance(item, bytes) else item)

    projection_payloads = [
        payload
        for payload in _stream_data_payloads(chunks)
        if payload["type"] == "shadow_clone_v2_projection"
    ]
    assert [payload["event_index"] for payload in projection_payloads] == [2]
    assert projection_payloads[0]["v2_event_type"] == "run_completed"


@pytest.mark.asyncio
async def test_stream_agent_run_v2_reconnect_from_event_cursor_does_not_skip_late_lower_sequence(
    monkeypatch,
):
    monkeypatch.setattr(agent_api, "db", _DummyDB())

    async def _fake_auth(*args, **kwargs):
        return "user-1"

    async def _fake_access(*args, **kwargs):
        return {
            "agent_run_id": "run-v2-event-cursor",
            "status": "running",
            "thread_id": "thread-1",
            "metadata": {"shadow_clone_mode": "v2"},
        }

    async def _fake_read_events(run_id, *, start="-", end="+", count=None):
        return [
            _v2_event(0, EventType.RUN_STARTED, {}, event_id="1000-0"),
            _v2_event(
                200,
                EventType.TASK_COMPLETED,
                {"task_id": "fast", "agent_name": "fast-agent"},
                event_id="1001-0",
            ),
            _v2_event(
                101,
                EventType.TASK_COMPLETED,
                {"task_id": "slow", "agent_name": "slow-agent"},
                event_id="1002-0",
            ),
        ]

    monkeypatch.setattr(agent_api, "get_user_id_from_stream_auth", _fake_auth)
    monkeypatch.setattr(agent_api, "get_agent_run_with_access_check", _fake_access)
    monkeypatch.setattr(
        agent_api.shadow_clone_v2_event_log, "read_events", _fake_read_events
    )

    response = await agent_api.stream_agent_run(
        agent_run_id="run-v2-event-cursor",
        token="token",
        request=_ImmediatelyDisconnectedRequest(),
        from_index=201,
        from_event_id="1001-0",
    )

    chunks = []
    async for item in response.body_iterator:
        chunks.append(item.decode() if isinstance(item, bytes) else item)

    projection_payloads = [
        payload
        for payload in _stream_data_payloads(chunks)
        if payload["type"] == "shadow_clone_v2_projection"
    ]
    assert [payload["event_id"] for payload in projection_payloads] == ["1002-0"]
    assert [payload["event_index"] for payload in projection_payloads] == [101]
    assert projection_payloads[0]["event_cursor"] == "1002-0"
    assert projection_payloads[0]["metadata"]["event_cursor"] == "1002-0"
    assert set(projection_payloads[0]["projection"]["tasks"]) >= {"fast", "slow"}
    assert projection_payloads[0]["projection"]["tasks"]["fast"]["status"] == (
        "completed"
    )
    assert projection_payloads[0]["projection"]["tasks"]["slow"]["status"] == (
        "completed"
    )


@pytest.mark.asyncio
async def test_stream_agent_run_v2_reconnect_from_terminal_event_cursor_returns_terminal_summary(
    monkeypatch,
):
    monkeypatch.setattr(agent_api, "db", _DummyDB())

    async def _fake_auth(*args, **kwargs):
        return "user-1"

    async def _fake_access(*args, **kwargs):
        return {
            "agent_run_id": "run-v2-terminal-cursor",
            "status": "completed",
            "thread_id": "thread-1",
            "metadata": {"shadow_clone_mode": "v2"},
        }

    async def _fake_read_events(run_id, *, start="-", end="+", count=None):
        return [
            _v2_event(0, EventType.RUN_STARTED, {}, event_id="1000-0"),
            _v2_event(
                10,
                EventType.RUN_COMPLETED,
                {"final_output": "Terminal answer."},
                event_id="1001-0",
            ),
        ]

    monkeypatch.setattr(agent_api, "get_user_id_from_stream_auth", _fake_auth)
    monkeypatch.setattr(agent_api, "get_agent_run_with_access_check", _fake_access)
    monkeypatch.setattr(
        agent_api.shadow_clone_v2_event_log, "read_events", _fake_read_events
    )

    response = await agent_api.stream_agent_run(
        agent_run_id="run-v2-terminal-cursor",
        token="token",
        request=_ImmediatelyDisconnectedRequest(),
        from_index=11,
        from_event_id="1001-0",
    )

    chunks = []
    async for item in response.body_iterator:
        chunks.append(item.decode() if isinstance(item, bytes) else item)

    payloads = _stream_data_payloads(chunks)
    terminal_payload = payloads[-1]
    assert terminal_payload["type"] == "shadow_clone_v2_projection"
    assert terminal_payload["event_id"] == "1001-0"
    assert terminal_payload["projection"]["status"] == "completed"
    assert terminal_payload["projection"]["final_output"]["content"] == (
        "Terminal answer."
    )
    assert terminal_payload["metadata"]["terminal_summary"] is True
    assert terminal_payload["metadata"]["requested_from_event_id"] == "1001-0"
    assert terminal_payload["metadata"]["event_cursor_found"] is True


@pytest.mark.asyncio
async def test_stream_agent_run_v2_reconnect_from_stale_event_cursor_returns_terminal_summary(
    monkeypatch,
):
    monkeypatch.setattr(agent_api, "db", _DummyDB())

    async def _fake_auth(*args, **kwargs):
        return "user-1"

    async def _fake_access(*args, **kwargs):
        return {
            "agent_run_id": "run-v2-stale-cursor",
            "status": "completed",
            "thread_id": "thread-1",
            "metadata": {"shadow_clone_mode": "v2"},
        }

    async def _fake_read_events(run_id, *, start="-", end="+", count=None):
        return [
            _v2_event(0, EventType.RUN_STARTED, {}, event_id="1000-0"),
            _v2_event(
                10,
                EventType.RUN_COMPLETED,
                {"final_output": "Terminal answer."},
                event_id="1001-0",
            ),
        ]

    monkeypatch.setattr(agent_api, "get_user_id_from_stream_auth", _fake_auth)
    monkeypatch.setattr(agent_api, "get_agent_run_with_access_check", _fake_access)
    monkeypatch.setattr(
        agent_api.shadow_clone_v2_event_log, "read_events", _fake_read_events
    )

    response = await agent_api.stream_agent_run(
        agent_run_id="run-v2-stale-cursor",
        token="token",
        request=_ImmediatelyDisconnectedRequest(),
        from_index=999,
        from_event_id="stale-0",
    )

    chunks = []
    async for item in response.body_iterator:
        chunks.append(item.decode() if isinstance(item, bytes) else item)

    payloads = _stream_data_payloads(chunks)
    terminal_payload = payloads[-1]
    assert terminal_payload["type"] == "shadow_clone_v2_projection"
    assert terminal_payload["event_id"] == "1001-0"
    assert terminal_payload["projection"]["status"] == "completed"
    assert terminal_payload["metadata"]["terminal_summary"] is True
    assert terminal_payload["metadata"]["requested_from_event_id"] == "stale-0"
    assert terminal_payload["metadata"]["event_cursor_found"] is False


@pytest.mark.asyncio
async def test_stream_agent_run_auto_runtime_v2_routes_to_event_log_stream(monkeypatch):
    monkeypatch.setattr(agent_api, "db", _DummyDB())

    async def _fake_auth(*args, **kwargs):
        return "user-1"

    async def _fake_access(*args, **kwargs):
        return {
            "agent_run_id": "run-auto-v2-stream",
            "status": "completed",
            "thread_id": "thread-1",
            "metadata": {
                "requested_shadow_clone_mode": "auto",
                "shadow_clone_runtime": "v2",
            },
        }

    async def _forbidden_lrange(*_args, **_kwargs):
        raise AssertionError("auto runtime v2 must not read legacy response list")

    async def _fake_read_events(run_id, *, start="-", end="+", count=None):
        return [
            _v2_event(1, EventType.RUN_STARTED, {}),
            _v2_event(2, EventType.RUN_COMPLETED, {}),
        ]

    monkeypatch.setattr(agent_api, "get_user_id_from_stream_auth", _fake_auth)
    monkeypatch.setattr(agent_api, "get_agent_run_with_access_check", _fake_access)
    monkeypatch.setattr(agent_api.redis, "lrange", _forbidden_lrange)
    monkeypatch.setattr(
        agent_api.shadow_clone_v2_event_log, "read_events", _fake_read_events
    )

    response = await agent_api.stream_agent_run(
        agent_run_id="run-auto-v2-stream",
        token="token",
        request=_ImmediatelyDisconnectedRequest(),
        from_index=0,
    )

    chunks = []
    async for item in response.body_iterator:
        chunks.append(item.decode() if isinstance(item, bytes) else item)

    assert any(
        payload["type"] == "shadow_clone_v2_projection"
        for payload in _stream_data_payloads(chunks)
    )


@pytest.mark.asyncio
async def test_stream_agent_run_v2_poll_detects_new_event_after_initial_replay(
    monkeypatch,
):
    monkeypatch.setattr(agent_api, "db", _DummyDB())
    monkeypatch.setattr(agent_api, "AGENT_STREAM_QUEUE_POLL_INTERVAL_SECONDS", 0)

    async def _fake_auth(*args, **kwargs):
        return "user-1"

    async def _fake_access(*args, **kwargs):
        return {
            "agent_run_id": "run-v2-live",
            "status": "running",
            "thread_id": "thread-1",
            "metadata": {"shadow_clone_mode": "v2"},
        }

    read_count = {"count": 0}

    async def _fake_read_events(run_id, *, start="-", end="+", count=None):
        read_count["count"] += 1
        if read_count["count"] == 1:
            return [_v2_event(0, EventType.RUN_STARTED, {})]
        return [
            _v2_event(0, EventType.RUN_STARTED, {}),
            _v2_event(1, EventType.RUN_COMPLETED, {"final_output": "Done."}),
        ]

    monkeypatch.setattr(agent_api, "get_user_id_from_stream_auth", _fake_auth)
    monkeypatch.setattr(agent_api, "get_agent_run_with_access_check", _fake_access)
    monkeypatch.setattr(
        agent_api.shadow_clone_v2_event_log, "read_events", _fake_read_events
    )

    response = await agent_api.stream_agent_run(
        agent_run_id="run-v2-live",
        token="token",
        request=_AlwaysConnectedRequest(),
        from_index=0,
    )

    chunks = []
    async for item in response.body_iterator:
        chunks.append(item.decode() if isinstance(item, bytes) else item)

    projection_payloads = [
        payload
        for payload in _stream_data_payloads(chunks)
        if payload["type"] == "shadow_clone_v2_projection"
    ]
    assert [payload["event_index"] for payload in projection_payloads] == [0, 1]
    assert projection_payloads[-1]["projection"]["final_output"]["content"] == "Done."


@pytest.mark.asyncio
async def test_stream_agent_run_v2_poll_does_not_skip_late_lower_sequence_event(
    monkeypatch,
):
    monkeypatch.setattr(agent_api, "db", _DummyDB())
    monkeypatch.setattr(agent_api, "AGENT_STREAM_QUEUE_POLL_INTERVAL_SECONDS", 0)

    async def _fake_auth(*args, **kwargs):
        return "user-1"

    async def _fake_access(*args, **kwargs):
        return {
            "agent_run_id": "run-v2-out-of-order-live",
            "status": "running",
            "thread_id": "thread-1",
            "metadata": {"shadow_clone_mode": "v2"},
        }

    read_count = {"count": 0}

    async def _fake_read_events(run_id, *, start="-", end="+", count=None):
        read_count["count"] += 1
        if read_count["count"] == 1:
            return [
                _v2_event(0, EventType.RUN_STARTED, {}),
                _v2_event(200, EventType.TASK_COMPLETED, {"task_id": "fast"}),
            ]
        return [
            _v2_event(0, EventType.RUN_STARTED, {}),
            _v2_event(200, EventType.TASK_COMPLETED, {"task_id": "fast"}),
            _v2_event(101, EventType.TASK_COMPLETED, {"task_id": "slow"}),
        ]

    monkeypatch.setattr(agent_api, "get_user_id_from_stream_auth", _fake_auth)
    monkeypatch.setattr(agent_api, "get_agent_run_with_access_check", _fake_access)
    monkeypatch.setattr(
        agent_api.shadow_clone_v2_event_log, "read_events", _fake_read_events
    )

    response = await agent_api.stream_agent_run(
        agent_run_id="run-v2-out-of-order-live",
        token="token",
        request=_DisconnectAfterChecksRequest(disconnect_after=2),
        from_index=0,
    )

    chunks = []
    async for item in response.body_iterator:
        chunks.append(item.decode() if isinstance(item, bytes) else item)

    projection_payloads = [
        payload
        for payload in _stream_data_payloads(chunks)
        if payload["type"] == "shadow_clone_v2_projection"
    ]
    assert [payload["event_index"] for payload in projection_payloads] == [
        0,
        200,
        101,
    ]


@pytest.mark.asyncio
async def test_stream_agent_run_running_v2_skips_legacy_terminal_tail_projection(
    monkeypatch,
):
    monkeypatch.setattr(agent_api, "db", _DummyDB())

    async def _fake_auth(*args, **kwargs):
        return "user-1"

    async def _fake_access(*args, **kwargs):
        return {
            "agent_run_id": "run-v2-running-isolated",
            "status": "running",
            "thread_id": "thread-1",
            "metadata": {"shadow_clone_mode": "v2"},
        }

    async def _forbidden_lrange(*_args, **_kwargs):
        raise AssertionError("running V2 stream must not read legacy response list")

    async def _fake_read_events(run_id, *, start="-", end="+", count=None):
        return [
            _v2_event(1, EventType.RUN_STARTED, {}),
            _v2_event(2, EventType.RUN_COMPLETED, {"final_output": "Done."}),
        ]

    monkeypatch.setattr(agent_api, "get_user_id_from_stream_auth", _fake_auth)
    monkeypatch.setattr(agent_api, "get_agent_run_with_access_check", _fake_access)
    monkeypatch.setattr(agent_api.redis, "lrange", _forbidden_lrange)
    monkeypatch.setattr(
        agent_api.shadow_clone_v2_event_log, "read_events", _fake_read_events
    )

    response = await agent_api.stream_agent_run(
        agent_run_id="run-v2-running-isolated",
        token="token",
        request=_ImmediatelyDisconnectedRequest(),
        from_index=0,
    )

    chunks = []
    async for item in response.body_iterator:
        chunks.append(item.decode() if isinstance(item, bytes) else item)

    assert any(
        payload["type"] == "shadow_clone_v2_projection"
        for payload in _stream_data_payloads(chunks)
    )


@pytest.mark.asyncio
async def test_stream_agent_run_v2_reconnect_after_terminal_returns_event_projection(
    monkeypatch,
):
    monkeypatch.setattr(agent_api, "db", _DummyDB())

    async def _fake_auth(*args, **kwargs):
        return "user-1"

    async def _fake_access(*args, **kwargs):
        return {
            "agent_run_id": "run-v2-after-terminal",
            "status": "running",
            "thread_id": "thread-1",
            "metadata": {"shadow_clone_mode": "v2"},
        }

    async def _fake_read_events(run_id, *, start="-", end="+", count=None):
        return [
            _v2_event(0, EventType.RUN_STARTED, {}),
            _v2_event(
                10, EventType.RUN_COMPLETED, {"final_output": "Terminal answer."}
            ),
        ]

    monkeypatch.setattr(agent_api, "get_user_id_from_stream_auth", _fake_auth)
    monkeypatch.setattr(agent_api, "get_agent_run_with_access_check", _fake_access)
    monkeypatch.setattr(
        agent_api.shadow_clone_v2_event_log, "read_events", _fake_read_events
    )

    response = await agent_api.stream_agent_run(
        agent_run_id="run-v2-after-terminal",
        token="token",
        request=_AlwaysConnectedRequest(),
        from_index=11,
    )

    chunks = []
    async for item in response.body_iterator:
        chunks.append(item.decode() if isinstance(item, bytes) else item)

    payloads = _stream_data_payloads(chunks)
    terminal_payload = payloads[-1]
    assert terminal_payload["type"] == "shadow_clone_v2_projection"
    assert terminal_payload["event_index"] == 10
    assert terminal_payload["projection"]["status"] == "completed"
    assert terminal_payload["projection"]["final_output"]["content"] == (
        "Terminal answer."
    )
    assert terminal_payload["metadata"]["terminal_summary"] is True
    assert terminal_payload["metadata"]["requested_from_index"] == 11
    assert terminal_payload["metadata"]["latest_terminal_sequence"] == 10


@pytest.mark.asyncio
async def test_stream_agent_run_v2_trimmed_history_returns_replay_unavailable(
    monkeypatch,
):
    monkeypatch.setattr(agent_api, "db", _DummyDB())

    async def _fake_auth(*args, **kwargs):
        return "user-1"

    async def _fake_access(*args, **kwargs):
        return {
            "agent_run_id": "run-v2-trimmed",
            "status": "running",
            "thread_id": "thread-1",
            "metadata": {"shadow_clone_mode": "v2"},
        }

    async def _fake_read_events(run_id, *, start="-", end="+", count=None):
        return [_v2_event(50, EventType.TASK_UPDATED, {"task_id": "task-1"})]

    monkeypatch.setattr(agent_api, "get_user_id_from_stream_auth", _fake_auth)
    monkeypatch.setattr(agent_api, "get_agent_run_with_access_check", _fake_access)
    monkeypatch.setattr(
        agent_api.shadow_clone_v2_event_log, "read_events", _fake_read_events
    )

    response = await agent_api.stream_agent_run(
        agent_run_id="run-v2-trimmed",
        token="token",
        request=_ImmediatelyDisconnectedRequest(),
        from_index=0,
    )

    chunks = []
    async for item in response.body_iterator:
        chunks.append(item.decode() if isinstance(item, bytes) else item)

    payloads = _stream_data_payloads(chunks)
    assert payloads == [
        {
            "type": "replay_unavailable",
            "status": "error",
            "event_index": 0,
            "message": "Shadow Clone V2 event replay is unavailable for the requested cursor.",
            "metadata": {"stream_source": "shadow_clone_v2_event_log"},
        }
    ]


@pytest.mark.asyncio
async def test_stream_agent_run_v2_retained_history_without_run_started_is_unavailable(
    monkeypatch,
):
    monkeypatch.setattr(agent_api, "db", _DummyDB())

    async def _fake_auth(*args, **kwargs):
        return "user-1"

    async def _fake_access(*args, **kwargs):
        return {
            "agent_run_id": "run-v2-trimmed-inside",
            "status": "running",
            "thread_id": "thread-1",
            "metadata": {"shadow_clone_mode": "v2"},
        }

    async def _fake_read_events(run_id, *, start="-", end="+", count=None):
        return [_v2_event(10, EventType.TASK_UPDATED, {"task_id": "task-1"})]

    monkeypatch.setattr(agent_api, "get_user_id_from_stream_auth", _fake_auth)
    monkeypatch.setattr(agent_api, "get_agent_run_with_access_check", _fake_access)
    monkeypatch.setattr(
        agent_api.shadow_clone_v2_event_log, "read_events", _fake_read_events
    )

    response = await agent_api.stream_agent_run(
        agent_run_id="run-v2-trimmed-inside",
        token="token",
        request=_ImmediatelyDisconnectedRequest(),
        from_index=10,
    )

    chunks = []
    async for item in response.body_iterator:
        chunks.append(item.decode() if isinstance(item, bytes) else item)

    assert _stream_data_payloads(chunks) == [
        {
            "type": "replay_unavailable",
            "status": "error",
            "event_index": 10,
            "message": "Shadow Clone V2 event replay is unavailable for the requested cursor.",
            "metadata": {"stream_source": "shadow_clone_v2_event_log"},
        }
    ]


@pytest.mark.asyncio
async def test_stream_agent_run_v2_empty_event_log_ignores_db_terminal_status(
    monkeypatch,
):
    monkeypatch.setattr(agent_api, "db", _DummyDB())

    async def _fake_auth(*args, **kwargs):
        return "user-1"

    async def _fake_access(*args, **kwargs):
        return {
            "agent_run_id": "run-v2-empty-log",
            "status": "completed",
            "thread_id": "thread-1",
            "metadata": {"shadow_clone_mode": "v2"},
        }

    async def _fake_read_events(run_id, *, start="-", end="+", count=None):
        return []

    monkeypatch.setattr(agent_api, "get_user_id_from_stream_auth", _fake_auth)
    monkeypatch.setattr(agent_api, "get_agent_run_with_access_check", _fake_access)
    monkeypatch.setattr(
        agent_api.shadow_clone_v2_event_log, "read_events", _fake_read_events
    )

    response = await agent_api.stream_agent_run(
        agent_run_id="run-v2-empty-log",
        token="token",
        request=_ImmediatelyDisconnectedRequest(),
        from_index=0,
    )

    chunks = []
    async for item in response.body_iterator:
        chunks.append(item.decode() if isinstance(item, bytes) else item)

    assert _stream_data_payloads(chunks) == []


@pytest.mark.asyncio
async def test_stream_agent_run_respects_from_index_and_adds_event_index(monkeypatch):
    monkeypatch.setattr(agent_api, "db", _DummyDB())

    async def _fake_auth(*args, **kwargs):
        return "user-1"

    async def _fake_access(*args, **kwargs):
        return {"status": "completed", "thread_id": "thread-1"}

    all_payloads = [
        {
            "sequence": 0,
            "thread_id": "thread-1",
            "type": "assistant",
            "content": json.dumps({"role": "assistant", "content": "A"}),
            "metadata": json.dumps({"stream_status": "chunk"}),
        },
        {
            "sequence": 1,
            "thread_id": "thread-1",
            "type": "assistant",
            "content": json.dumps({"role": "assistant", "content": "B"}),
            "metadata": json.dumps({"stream_status": "chunk"}),
        },
        {
            "sequence": 2,
            "thread_id": "thread-1",
            "type": "assistant",
            "content": json.dumps({"role": "assistant", "content": "C"}),
            "metadata": json.dumps({"stream_status": "chunk"}),
        },
    ]

    calls = []

    async def _fake_lrange(_key, start, _end):
        calls.append(start)
        return [json.dumps(p, ensure_ascii=False) for p in all_payloads[start:]]

    monkeypatch.setattr(agent_api, "get_user_id_from_stream_auth", _fake_auth)
    monkeypatch.setattr(agent_api, "get_agent_run_with_access_check", _fake_access)
    monkeypatch.setattr(agent_api.redis, "lrange", _fake_lrange)

    response = await agent_api.stream_agent_run(
        agent_run_id="run-1",
        token="token",
        request=object(),
        from_index=2,
    )

    chunks = []
    async for item in response.body_iterator:
        if isinstance(item, bytes):
            chunks.append(item.decode())
        else:
            chunks.append(item)

    assert calls == [2]

    payloads = _stream_data_payloads(chunks)
    assert len(payloads) == 2

    first_payload = payloads[0]
    assert first_payload["event_index"] == 2
    assert json.loads(first_payload["content"])["content"] == "C"

    second_payload = payloads[1]
    assert second_payload["type"] == "status"
    assert second_payload["status"] == "completed"


@pytest.mark.asyncio
async def test_stream_agent_run_drains_pending_responses_before_control_exit(
    monkeypatch,
):
    monkeypatch.setattr(agent_api, "db", _DummyDB())

    async def _fake_auth(*args, **kwargs):
        return "user-1"

    async def _fake_access(*args, **kwargs):
        return {"status": "running", "thread_id": "thread-1"}

    late_payloads = [
        {
            "sequence": 5,
            "thread_id": "thread-1",
            "type": "assistant",
            "content": json.dumps({"role": "assistant", "content": "late-chunk"}),
            "metadata": json.dumps({"stream_status": "chunk"}),
        },
    ]

    lrange_calls = []
    lrange_call_count = {"count": 0}

    async def _fake_lrange(_key, start, _end):
        lrange_calls.append(start)
        # Initial fetch has no data, subsequent fetch (drain path) returns chunk.
        if lrange_call_count["count"] == 0:
            lrange_call_count["count"] += 1
            return []
        return [json.dumps(p, ensure_ascii=False) for p in late_payloads]

    async def _fake_llen(_key):
        return 0

    response_pubsub = _FakePubSub(messages=[])
    control_pubsub = _FakePubSub(
        messages=[
            {
                "type": "message",
                "channel": b"agent_run:run-ctrl:control",
                "data": b"END_STREAM",
            }
        ]
    )
    pubsub_index = {"count": 0}

    async def _fake_create_pubsub():
        if pubsub_index["count"] == 0:
            pubsub_index["count"] += 1
            return response_pubsub
        return control_pubsub

    monkeypatch.setattr(agent_api, "get_user_id_from_stream_auth", _fake_auth)
    monkeypatch.setattr(agent_api, "get_agent_run_with_access_check", _fake_access)
    monkeypatch.setattr(agent_api.redis, "lrange", _fake_lrange)
    monkeypatch.setattr(agent_api.redis, "llen", _fake_llen)
    monkeypatch.setattr(agent_api.redis, "create_pubsub", _fake_create_pubsub)

    response = await agent_api.stream_agent_run(
        agent_run_id="run-ctrl",
        token="token",
        request=_AlwaysConnectedRequest(),
        from_index=0,
    )

    chunks = []
    async for item in response.body_iterator:
        if isinstance(item, bytes):
            chunks.append(item.decode())
        else:
            chunks.append(item)

    payloads = _stream_data_payloads(chunks)

    assert lrange_calls[:2] == [0, 0]
    assert any(
        payload.get("type") == "assistant"
        and json.loads(payload.get("content", "{}")).get("content") == "late-chunk"
        for payload in payloads
    )
    assert any(
        payload.get("type") == "status"
        and payload.get("status") == "completed"
        and payload.get("control_signal") == "END_STREAM"
        for payload in payloads
    )


@pytest.mark.asyncio
async def test_stream_agent_run_filters_stale_failed_payloads_when_stop_control_arrives(
    monkeypatch,
):
    monkeypatch.setattr(agent_api, "db", _DummyDB())

    async def _fake_auth(*args, **kwargs):
        return "user-1"

    async def _fake_access(*args, **kwargs):
        return {"status": "running", "thread_id": "thread-1", "error": None}

    replay_payloads = [
        {
            "sequence": 5,
            "thread_id": "thread-1",
            "type": "status",
            "status": "failed",
            "message": "stale failure",
        },
    ]
    lrange_call_count = {"count": 0}

    async def _fake_lrange(_key, _start, _end):
        if lrange_call_count["count"] == 0:
            lrange_call_count["count"] += 1
            return []
        return [json.dumps(payload, ensure_ascii=False) for payload in replay_payloads]

    async def _fake_llen(_key):
        return 0

    response_pubsub = _FakePubSub(messages=[])
    control_pubsub = _FakePubSub(
        messages=[
            {
                "type": "message",
                "channel": b"agent_run:run-stop-drain:control",
                "data": b"STOP",
            }
        ]
    )
    pubsub_index = {"count": 0}

    async def _fake_create_pubsub():
        if pubsub_index["count"] == 0:
            pubsub_index["count"] += 1
            return response_pubsub
        return control_pubsub

    monkeypatch.setattr(agent_api, "get_user_id_from_stream_auth", _fake_auth)
    monkeypatch.setattr(agent_api, "get_agent_run_with_access_check", _fake_access)
    monkeypatch.setattr(agent_api.redis, "lrange", _fake_lrange)
    monkeypatch.setattr(agent_api.redis, "llen", _fake_llen)
    monkeypatch.setattr(agent_api.redis, "create_pubsub", _fake_create_pubsub)

    response = await agent_api.stream_agent_run(
        agent_run_id="run-stop-drain",
        token="token",
        request=_AlwaysConnectedRequest(),
        from_index=0,
    )

    chunks = []
    async for item in response.body_iterator:
        chunks.append(item.decode() if isinstance(item, bytes) else item)

    payloads = _stream_data_payloads(chunks)
    statuses = [
        payload.get("status") for payload in payloads if payload.get("type") == "status"
    ]

    assert statuses == ["stopped"]


@pytest.mark.asyncio
async def test_stream_agent_run_prefers_stopped_terminal_over_listener_error(
    monkeypatch,
):
    monkeypatch.setattr(agent_api, "db", _DummyDB())

    async def _fake_auth(*args, **kwargs):
        return "user-1"

    access_results = iter(
        [
            {"status": "running", "thread_id": "thread-1", "error": None},
            {"status": "stopped", "thread_id": "thread-1", "error": None},
        ]
    )

    async def _fake_access(*args, **kwargs):
        return next(access_results)

    async def _fake_lrange(_key, _start, _end):
        return []

    async def _fake_llen(_key):
        return 0

    response_pubsub = _FakePubSub(messages=[])
    control_pubsub = _BrokenPubSub(RuntimeError("listener boom"))
    pubsub_index = {"count": 0}

    async def _fake_create_pubsub():
        if pubsub_index["count"] == 0:
            pubsub_index["count"] += 1
            return response_pubsub
        return control_pubsub

    monkeypatch.setattr(agent_api, "get_user_id_from_stream_auth", _fake_auth)
    monkeypatch.setattr(agent_api, "get_agent_run_with_access_check", _fake_access)
    monkeypatch.setattr(agent_api.redis, "lrange", _fake_lrange)
    monkeypatch.setattr(agent_api.redis, "llen", _fake_llen)
    monkeypatch.setattr(agent_api.redis, "create_pubsub", _fake_create_pubsub)

    response = await agent_api.stream_agent_run(
        agent_run_id="run-listener-stop",
        token="token",
        request=_AlwaysConnectedRequest(),
        from_index=0,
    )

    chunks = []
    async for item in response.body_iterator:
        chunks.append(item.decode() if isinstance(item, bytes) else item)

    payloads = _stream_data_payloads(chunks)
    statuses = [
        payload.get("status") for payload in payloads if payload.get("type") == "status"
    ]

    assert statuses == ["stopped"]


@pytest.mark.asyncio
async def test_stream_agent_run_caps_large_initial_replay_for_running_zero_cursor(
    monkeypatch,
):
    monkeypatch.setattr(agent_api, "db", _DummyDB())
    monkeypatch.setattr(agent_api, "AGENT_STREAM_INITIAL_REPLAY_LIMIT", 2)
    monkeypatch.setattr(agent_api, "AGENT_STREAM_PRIORITY_BACKLOG_MULTIPLIER", 4)

    async def _fake_auth(*args, **kwargs):
        return "user-1"

    async def _fake_access(*args, **kwargs):
        return {"status": "running", "thread_id": "thread-1"}

    all_payloads = [
        {
            "sequence": index,
            "thread_id": "thread-1",
            "type": "assistant",
            "content": json.dumps(
                {"role": "assistant", "content": f"chunk-{index}"},
            ),
            "metadata": json.dumps({"stream_status": "chunk"}),
        }
        for index in range(10)
    ]

    lrange_calls = []

    async def _fake_lrange(_key, start, _end):
        lrange_calls.append(start)
        return [json.dumps(p, ensure_ascii=False) for p in all_payloads[start:]]

    async def _fake_llen(_key):
        return len(all_payloads)

    response_pubsub = _FakePubSub(messages=[])
    control_pubsub = _FakePubSub(
        messages=[
            {
                "type": "message",
                "channel": b"agent_run:run-cap:control",
                "data": b"END_STREAM",
            }
        ]
    )
    pubsub_index = {"count": 0}

    async def _fake_create_pubsub():
        if pubsub_index["count"] == 0:
            pubsub_index["count"] += 1
            return response_pubsub
        return control_pubsub

    monkeypatch.setattr(agent_api, "get_user_id_from_stream_auth", _fake_auth)
    monkeypatch.setattr(agent_api, "get_agent_run_with_access_check", _fake_access)
    monkeypatch.setattr(agent_api.redis, "lrange", _fake_lrange)
    monkeypatch.setattr(agent_api.redis, "llen", _fake_llen)
    monkeypatch.setattr(agent_api.redis, "create_pubsub", _fake_create_pubsub)

    response = await agent_api.stream_agent_run(
        agent_run_id="run-cap",
        token="token",
        request=_AlwaysConnectedRequest(),
        from_index=0,
    )

    chunks = []
    async for item in response.body_iterator:
        if isinstance(item, bytes):
            chunks.append(item.decode())
        else:
            chunks.append(item)

    payloads = _stream_data_payloads(chunks)

    assistant_payloads = [
        payload for payload in payloads if payload.get("type") == "assistant"
    ]
    assert len(assistant_payloads) == 2
    assert assistant_payloads[0]["event_index"] == 8
    assert json.loads(assistant_payloads[0]["content"])["content"] == "chunk-8"
    assert assistant_payloads[1]["event_index"] == 9
    assert any(
        payload.get("type") == "status"
        and payload.get("status") == "completed"
        and payload.get("control_signal") == "END_STREAM"
        for payload in payloads
    )


@pytest.mark.asyncio
async def test_stream_agent_run_caps_running_initial_catchup_for_nonzero_cursor(
    monkeypatch,
):
    monkeypatch.setattr(agent_api, "db", _DummyDB())
    monkeypatch.setattr(agent_api, "AGENT_STREAM_RUNNING_CATCHUP_LIMIT", 3)
    monkeypatch.setattr(agent_api, "AGENT_STREAM_PRIORITY_BACKLOG_MULTIPLIER", 4)

    async def _fake_auth(*args, **kwargs):
        return "user-1"

    async def _fake_access(*args, **kwargs):
        return {"status": "running", "thread_id": "thread-1"}

    all_payloads = [
        {
            "sequence": index,
            "thread_id": "thread-1",
            "type": "assistant",
            "content": json.dumps(
                {"role": "assistant", "content": f"chunk-{index}"},
            ),
            "metadata": json.dumps({"stream_status": "chunk"}),
        }
        for index in range(10)
    ]

    lrange_calls = []

    async def _fake_lrange(_key, start, _end):
        lrange_calls.append(start)
        return [json.dumps(p, ensure_ascii=False) for p in all_payloads[start:]]

    async def _fake_llen(_key):
        return len(all_payloads)

    response_pubsub = _FakePubSub(messages=[])
    control_pubsub = _FakePubSub(
        messages=[
            {
                "type": "message",
                "channel": b"agent_run:run-nonzero:control",
                "data": b"END_STREAM",
            }
        ]
    )
    pubsub_index = {"count": 0}

    async def _fake_create_pubsub():
        if pubsub_index["count"] == 0:
            pubsub_index["count"] += 1
            return response_pubsub
        return control_pubsub

    monkeypatch.setattr(agent_api, "get_user_id_from_stream_auth", _fake_auth)
    monkeypatch.setattr(agent_api, "get_agent_run_with_access_check", _fake_access)
    monkeypatch.setattr(agent_api.redis, "lrange", _fake_lrange)
    monkeypatch.setattr(agent_api.redis, "llen", _fake_llen)
    monkeypatch.setattr(agent_api.redis, "create_pubsub", _fake_create_pubsub)

    response = await agent_api.stream_agent_run(
        agent_run_id="run-nonzero",
        token="token",
        request=_AlwaysConnectedRequest(),
        from_index=2,
    )

    chunks = []
    async for item in response.body_iterator:
        if isinstance(item, bytes):
            chunks.append(item.decode())
        else:
            chunks.append(item)

    payloads = _stream_data_payloads(chunks)

    assistant_payloads = [
        payload for payload in payloads if payload.get("type") == "assistant"
    ]
    assert [payload["event_index"] for payload in assistant_payloads] == [7, 8, 9]
    assert any(
        payload.get("type") == "status"
        and payload.get("status") == "completed"
        and payload.get("control_signal") == "END_STREAM"
        for payload in payloads
    )


@pytest.mark.asyncio
async def test_stream_agent_run_prioritizes_productive_chunks_over_non_progress_backlog(
    monkeypatch,
):
    monkeypatch.setattr(agent_api, "db", _DummyDB())
    monkeypatch.setattr(agent_api, "AGENT_STREAM_INITIAL_REPLAY_LIMIT", 3)
    monkeypatch.setattr(agent_api, "AGENT_STREAM_PRIORITY_BACKLOG_MULTIPLIER", 2)

    async def _fake_auth(*args, **kwargs):
        return "user-1"

    async def _fake_access(*args, **kwargs):
        return {"status": "running", "thread_id": "thread-1"}

    all_payloads = [
        {
            "sequence": 0,
            "thread_id": "thread-1",
            "type": "assistant",
            "content": json.dumps({"role": "assistant", "content": "prod-0"}),
            "metadata": json.dumps({"stream_status": "chunk"}),
        },
        {
            "sequence": 1,
            "thread_id": "thread-1",
            "type": "assistant",
            "metadata": json.dumps(
                {
                    "stream_status": "tool_call_chunk",
                    "tool_calls": [
                        {
                            "function": {
                                "name": "write_file",
                                "arguments": json.dumps({"path": "/workspace/a.txt"}),
                            }
                        }
                    ],
                }
            ),
        },
        {
            "sequence": 2,
            "thread_id": "thread-1",
            "type": "assistant",
            "metadata": json.dumps({"stream_status": "heartbeat"}),
        },
        {
            "sequence": 3,
            "thread_id": "thread-1",
            "type": "assistant",
            "content": json.dumps({"role": "assistant", "content": "prod-3"}),
            "metadata": json.dumps({"stream_status": "chunk"}),
        },
        {
            "sequence": 4,
            "thread_id": "thread-1",
            "type": "assistant",
            "metadata": json.dumps(
                {
                    "stream_status": "tool_call_chunk",
                    "tool_calls": [
                        {
                            "function": {
                                "name": "write_file",
                                "arguments": json.dumps({"path": "/workspace/b.txt"}),
                            }
                        }
                    ],
                }
            ),
        },
        {
            "sequence": 5,
            "thread_id": "thread-1",
            "type": "assistant",
            "content": json.dumps({"role": "assistant", "content": "prod-5"}),
            "metadata": json.dumps({"stream_status": "chunk"}),
        },
    ]

    async def _fake_lrange(_key, start, _end):
        return [json.dumps(p, ensure_ascii=False) for p in all_payloads[start:]]

    async def _fake_llen(_key):
        return len(all_payloads)

    response_pubsub = _FakePubSub(messages=[])
    control_pubsub = _FakePubSub(
        messages=[
            {
                "type": "message",
                "channel": b"agent_run:run-priority:control",
                "data": b"END_STREAM",
            }
        ]
    )
    pubsub_index = {"count": 0}

    async def _fake_create_pubsub():
        if pubsub_index["count"] == 0:
            pubsub_index["count"] += 1
            return response_pubsub
        return control_pubsub

    monkeypatch.setattr(agent_api, "get_user_id_from_stream_auth", _fake_auth)
    monkeypatch.setattr(agent_api, "get_agent_run_with_access_check", _fake_access)
    monkeypatch.setattr(agent_api.redis, "lrange", _fake_lrange)
    monkeypatch.setattr(agent_api.redis, "llen", _fake_llen)
    monkeypatch.setattr(agent_api.redis, "create_pubsub", _fake_create_pubsub)

    response = await agent_api.stream_agent_run(
        agent_run_id="run-priority",
        token="token",
        request=_AlwaysConnectedRequest(),
        from_index=0,
    )

    chunks = []
    async for item in response.body_iterator:
        chunks.append(item.decode() if isinstance(item, bytes) else item)

    payloads = _stream_data_payloads(chunks)
    assistant_payloads = [
        payload for payload in payloads if payload.get("type") == "assistant"
    ]

    assert [payload["event_index"] for payload in assistant_payloads] == [0, 3, 5]
    assert all("non_progress" not in payload for payload in assistant_payloads)


def test_select_priority_backlog_window_deprioritizes_tool_lifecycle_statuses():
    responses = [
        {
            "sequence": 0,
            "type": "assistant",
            "content": json.dumps({"role": "assistant", "content": "alpha"}),
            "metadata": json.dumps({"stream_status": "chunk"}),
        },
        {
            "sequence": 1,
            "type": "status",
            "content": json.dumps({"status_type": "tool_started"}),
        },
        {
            "sequence": 2,
            "type": "status",
            "content": json.dumps({"status_type": "tool_completed"}),
        },
        {
            "sequence": 3,
            "type": "assistant",
            "content": json.dumps({"role": "assistant", "content": "omega"}),
            "metadata": json.dumps({"stream_status": "chunk"}),
        },
        {
            "sequence": 4,
            "type": "status",
            "content": json.dumps({"status_type": "tool_failed"}),
        },
    ]

    selected = agent_api._select_priority_backlog_window(responses, limit=2)

    assert [response["sequence"] for response in selected] == [0, 3]


def test_select_priority_backlog_window_keeps_latest_tool_lifecycle_when_only_signal():
    responses = [
        {
            "sequence": 0,
            "type": "status",
            "content": json.dumps({"status_type": "tool_started"}),
        },
        {
            "sequence": 1,
            "type": "status",
            "content": json.dumps({"status_type": "tool_completed"}),
        },
        {
            "sequence": 2,
            "type": "status",
            "content": json.dumps({"status_type": "tool_failed"}),
        },
    ]

    selected = agent_api._select_priority_backlog_window(responses, limit=2)

    assert [response["sequence"] for response in selected] == [2]


@pytest.mark.asyncio
@pytest.mark.parametrize("run_status", ["failed", "stopped"])
async def test_stream_agent_run_preserves_terminal_status_for_late_subscribers(
    monkeypatch,
    run_status,
):
    monkeypatch.setattr(agent_api, "db", _DummyDB())

    async def _fake_auth(*args, **kwargs):
        return "user-1"

    async def _fake_access(*args, **kwargs):
        return {
            "status": run_status,
            "thread_id": "thread-1",
            "error": "sandbox unavailable" if run_status == "failed" else None,
        }

    async def _fake_lrange(_key, _start, _end):
        return []

    monkeypatch.setattr(agent_api, "get_user_id_from_stream_auth", _fake_auth)
    monkeypatch.setattr(agent_api, "get_agent_run_with_access_check", _fake_access)
    monkeypatch.setattr(agent_api.redis, "lrange", _fake_lrange)

    response = await agent_api.stream_agent_run(
        agent_run_id=f"run-{run_status}",
        token="token",
        request=object(),
        from_index=0,
    )

    chunks = []
    async for item in response.body_iterator:
        chunks.append(item.decode() if isinstance(item, bytes) else item)

    payloads = _stream_data_payloads(chunks)
    assert payloads[-1]["type"] == "status"
    assert payloads[-1]["status"] == run_status
    if run_status == "failed":
        assert payloads[-1]["message"] == "sandbox unavailable"


@pytest.mark.asyncio
async def test_stream_agent_run_projects_terminal_status_from_response_tail_before_subscribing(
    monkeypatch,
):
    monkeypatch.setattr(agent_api, "db", _DummyDB())

    async def _fake_auth(*args, **kwargs):
        return "user-1"

    async def _fake_access(*args, **kwargs):
        return {
            "agent_run_id": "run-projected-terminal",
            "status": "running",
            "thread_id": "thread-1",
            "error": None,
        }

    async def _fake_lrange(_key, _start, _end):
        return []

    async def _fake_tail_projection(_agent_run_id: str, **_kwargs):
        return ("failed", "projected failure")

    async def _unexpected_create_pubsub():
        raise AssertionError(
            "stream should not subscribe after projected terminal truth"
        )

    monkeypatch.setattr(agent_api, "get_user_id_from_stream_auth", _fake_auth)
    monkeypatch.setattr(agent_api, "get_agent_run_with_access_check", _fake_access)
    monkeypatch.setattr(
        agent_api, "read_terminal_status_from_response_tail", _fake_tail_projection
    )
    monkeypatch.setattr(agent_api.redis, "lrange", _fake_lrange)
    monkeypatch.setattr(agent_api.redis, "create_pubsub", _unexpected_create_pubsub)

    response = await agent_api.stream_agent_run(
        agent_run_id="run-projected-terminal",
        token="token",
        request=object(),
        from_index=0,
    )

    chunks = []
    async for item in response.body_iterator:
        chunks.append(item.decode() if isinstance(item, bytes) else item)

    payloads = _stream_data_payloads(chunks)
    assert payloads[-1] == {
        "type": "status",
        "status": "failed",
        "message": "projected failure",
    }


@pytest.mark.asyncio
async def test_project_agent_run_row_terminal_status_phase2_uses_latest_attempt_epoch(
    monkeypatch,
):
    fake_client = _AttemptClient(
        [
            {
                "attempt_id": "attempt-1",
                "agent_run_id": "run-phase2-projection",
                "attempt_number": 1,
                "execution_epoch": 1,
                "status": "abandoned",
            },
            {
                "attempt_id": "attempt-2",
                "agent_run_id": "run-phase2-projection",
                "attempt_number": 2,
                "execution_epoch": 2,
                "status": "running",
            },
        ]
    )
    monkeypatch.setattr(agent_api, "db", _StaticClientDB(fake_client))

    projection_calls = []

    async def _fake_tail_projection(
        _agent_run_id: str, *, current_execution_epoch=None
    ):
        projection_calls.append(current_execution_epoch)
        return ("failed", "attempt scoped failure")

    monkeypatch.setattr(
        agent_api,
        "read_terminal_status_from_response_tail",
        _fake_tail_projection,
    )

    projected_row = await agent_api._project_agent_run_row_terminal_status(
        {
            "agent_run_id": "run-phase2-projection",
            "status": "running",
            "metadata": json.dumps(
                {
                    "shadow_clone_mode": "off",
                    "regular_execution_mode": "phase2_supervisor",
                }
            ),
        }
    )

    assert projection_calls == [2]
    assert projected_row["status"] == "failed"
    assert projected_row["error"] == "attempt scoped failure"


@pytest.mark.asyncio
async def test_project_agent_run_row_terminal_status_phase2_prefers_parent_pointer_without_attempt_scan(
    monkeypatch,
):
    class _FailIfAttemptTableTouched:
        def table(self, table_name):
            assert table_name != "regular_run_attempts"
            raise AssertionError("regular_run_attempts scan should not happen")

    monkeypatch.setattr(agent_api, "db", _StaticClientDB(_FailIfAttemptTableTouched()))

    projection_calls = []

    async def _fake_tail_projection(
        _agent_run_id: str, *, current_execution_epoch=None
    ):
        projection_calls.append(current_execution_epoch)
        return ("failed", "projected")

    monkeypatch.setattr(
        agent_api,
        "read_terminal_status_from_response_tail",
        _fake_tail_projection,
    )

    projected_row = await agent_api._project_agent_run_row_terminal_status(
        {
            "agent_run_id": "run-phase2-pointer",
            "status": "running",
            "current_execution_epoch": 7,
            "stream_source_epoch": 6,
            "regular_execution_backend": "phase2_supervisor",
            "metadata": json.dumps(
                {
                    "shadow_clone_mode": "off",
                    "regular_execution_mode": "phase2_supervisor",
                }
            ),
        }
    )

    assert projection_calls == [6]
    assert projected_row["status"] == "failed"
    assert projected_row["error"] == "projected"


@pytest.mark.asyncio
async def test_project_agent_run_row_terminal_status_phase2_skips_projection_when_attempt_lookup_fails(
    monkeypatch,
):
    class _FailingAttemptQuery:
        def select(self, *_args, **_kwargs):
            return self

        def eq(self, *_args, **_kwargs):
            return self

        async def execute(self):
            raise RuntimeError("attempt lookup unavailable")

    class _FailingAttemptClient:
        def table(self, table_name):
            assert table_name == "regular_run_attempts"
            return _FailingAttemptQuery()

    monkeypatch.setattr(agent_api, "db", _StaticClientDB(_FailingAttemptClient()))

    async def _unexpected_tail_projection(*_args, **_kwargs):
        raise AssertionError("phase2 fallback projection should not run")

    monkeypatch.setattr(
        agent_api,
        "read_terminal_status_from_response_tail",
        _unexpected_tail_projection,
    )

    projected_row = await agent_api._project_agent_run_row_terminal_status(
        {
            "agent_run_id": "run-phase2-lookup-failure",
            "status": "running",
            "metadata": json.dumps(
                {
                    "shadow_clone_mode": "off",
                    "regular_execution_mode": "phase2_supervisor",
                }
            ),
        }
    )

    assert projected_row["status"] == "running"
    assert projected_row.get("error") is None


@pytest.mark.asyncio
async def test_resolve_public_response_stream_source_phase2_uses_parent_stream_source_epoch():
    class _FailIfAttemptTableTouched:
        def table(self, table_name):
            assert table_name != "regular_run_attempts"
            raise AssertionError("regular_run_attempts scan should not happen")

    execution_epoch, response_list_key, lookup_succeeded = (
        await agent_api._resolve_public_response_stream_source(
            _FailIfAttemptTableTouched(),
            agent_run_id="run-phase2-pointer",
            agent_run_row={
                "agent_run_id": "run-phase2-pointer",
                "current_execution_epoch": 7,
                "stream_source_epoch": 6,
                "regular_execution_backend": "phase2_supervisor",
                "metadata": json.dumps(
                    {
                        "shadow_clone_mode": "off",
                        "regular_execution_mode": "phase2_supervisor",
                    }
                ),
            },
        )
    )

    assert execution_epoch == 6
    assert response_list_key == "agent_run:run-phase2-pointer:epoch:6:responses"
    assert lookup_succeeded is True


@pytest.mark.asyncio
async def test_stream_agent_run_phase2_initial_replay_uses_latest_attempt_response_list(
    monkeypatch,
):
    fake_client = _AttemptClient(
        [
            {
                "attempt_id": "attempt-1",
                "agent_run_id": "run-phase2-late-subscriber",
                "attempt_number": 1,
                "execution_epoch": 1,
                "status": "abandoned",
            },
            {
                "attempt_id": "attempt-2",
                "agent_run_id": "run-phase2-late-subscriber",
                "attempt_number": 2,
                "execution_epoch": 2,
                "status": "stopped",
            },
        ]
    )
    monkeypatch.setattr(agent_api, "db", _StaticClientDB(fake_client))

    async def _fake_auth(*args, **kwargs):
        return "user-1"

    async def _fake_access(*args, **kwargs):
        return {
            "agent_run_id": "run-phase2-late-subscriber",
            "status": "stopped",
            "thread_id": "thread-1",
            "error": None,
            "metadata": json.dumps(
                {
                    "shadow_clone_mode": "off",
                    "regular_execution_mode": "phase2_supervisor",
                }
            ),
        }

    seen_keys = []

    async def _fake_lrange(key, _start, _end):
        seen_keys.append(key)
        if key == "agent_run:run-phase2-late-subscriber:epoch:2:responses":
            return [
                json.dumps(
                    {
                        "sequence": 0,
                        "thread_id": "thread-1",
                        "type": "assistant",
                        "content": json.dumps(
                            {"role": "assistant", "content": "attempt-2-visible"}
                        ),
                        "metadata": json.dumps({"stream_status": "chunk"}),
                    },
                    ensure_ascii=False,
                )
            ]
        return [
            json.dumps(
                {
                    "sequence": 0,
                    "thread_id": "thread-1",
                    "type": "assistant",
                    "content": json.dumps(
                        {"role": "assistant", "content": "stale-run-wide"}
                    ),
                    "metadata": json.dumps({"stream_status": "chunk"}),
                },
                ensure_ascii=False,
            )
        ]

    monkeypatch.setattr(agent_api, "get_user_id_from_stream_auth", _fake_auth)
    monkeypatch.setattr(agent_api, "get_agent_run_with_access_check", _fake_access)
    monkeypatch.setattr(agent_api.redis, "lrange", _fake_lrange)

    response = await agent_api.stream_agent_run(
        agent_run_id="run-phase2-late-subscriber",
        token="token",
        request=object(),
        from_index=0,
    )

    chunks = []
    async for item in response.body_iterator:
        chunks.append(item.decode() if isinstance(item, bytes) else item)

    payloads = _stream_data_payloads(chunks)

    assert seen_keys[0] == "agent_run:run-phase2-late-subscriber:epoch:2:responses"
    assert "agent_run:run-phase2-late-subscriber:responses" not in seen_keys
    assert any(
        payload.get("type") == "assistant"
        and json.loads(payload.get("content", "{}")).get("content")
        == "attempt-2-visible"
        for payload in payloads
    )


@pytest.mark.asyncio
async def test_stream_agent_run_phase2_initial_replay_falls_back_to_latest_nonempty_terminal_attempt(
    monkeypatch,
):
    fake_client = _AttemptClient(
        [
            {
                "attempt_id": "attempt-1",
                "agent_run_id": "run-phase2-terminal-fallback",
                "attempt_number": 1,
                "execution_epoch": 1,
                "status": "completed",
            },
            {
                "attempt_id": "attempt-2",
                "agent_run_id": "run-phase2-terminal-fallback",
                "attempt_number": 2,
                "execution_epoch": 2,
                "status": "stopped",
            },
        ]
    )
    monkeypatch.setattr(agent_api, "db", _StaticClientDB(fake_client))

    async def _fake_auth(*args, **kwargs):
        return "user-1"

    async def _fake_access(*args, **kwargs):
        return {
            "agent_run_id": "run-phase2-terminal-fallback",
            "status": "stopped",
            "thread_id": "thread-1",
            "error": None,
            "metadata": json.dumps(
                {
                    "shadow_clone_mode": "off",
                    "regular_execution_mode": "phase2_supervisor",
                }
            ),
        }

    seen_keys = []

    async def _fake_lrange(key, _start, _end):
        seen_keys.append(key)
        if key == "agent_run:run-phase2-terminal-fallback:epoch:2:responses":
            return []
        if key == "agent_run:run-phase2-terminal-fallback:epoch:1:responses":
            return [
                json.dumps(
                    {
                        "sequence": 0,
                        "thread_id": "thread-1",
                        "type": "assistant",
                        "content": json.dumps(
                            {"role": "assistant", "content": "attempt-1-visible"}
                        ),
                        "metadata": json.dumps({"stream_status": "chunk"}),
                    },
                    ensure_ascii=False,
                )
            ]
        return []

    monkeypatch.setattr(agent_api, "get_user_id_from_stream_auth", _fake_auth)
    monkeypatch.setattr(agent_api, "get_agent_run_with_access_check", _fake_access)
    monkeypatch.setattr(agent_api.redis, "lrange", _fake_lrange)

    response = await agent_api.stream_agent_run(
        agent_run_id="run-phase2-terminal-fallback",
        token="token",
        request=object(),
        from_index=0,
    )

    chunks = []
    async for item in response.body_iterator:
        chunks.append(item.decode() if isinstance(item, bytes) else item)

    payloads = _stream_data_payloads(chunks)

    assert seen_keys[0] == "agent_run:run-phase2-terminal-fallback:epoch:2:responses"
    assert "agent_run:run-phase2-terminal-fallback:epoch:1:responses" in seen_keys
    assert any(
        payload.get("type") == "assistant"
        and json.loads(payload.get("content", "{}")).get("content")
        == "attempt-1-visible"
        for payload in payloads
    )


@pytest.mark.asyncio
async def test_stream_agent_run_phase2_terminal_replay_fallback_skips_nonterminal_previous_attempts(
    monkeypatch,
):
    fake_client = _AttemptClient(
        [
            {
                "attempt_id": "attempt-1",
                "agent_run_id": "run-phase2-terminal-no-abandoned-fallback",
                "attempt_number": 1,
                "execution_epoch": 1,
                "status": "abandoned",
            },
            {
                "attempt_id": "attempt-2",
                "agent_run_id": "run-phase2-terminal-no-abandoned-fallback",
                "attempt_number": 2,
                "execution_epoch": 2,
                "status": "completed",
            },
        ]
    )
    monkeypatch.setattr(agent_api, "db", _StaticClientDB(fake_client))

    async def _fake_auth(*args, **kwargs):
        return "user-1"

    async def _fake_access(*args, **kwargs):
        return {
            "agent_run_id": "run-phase2-terminal-no-abandoned-fallback",
            "status": "completed",
            "thread_id": "thread-1",
            "error": None,
            "metadata": json.dumps(
                {
                    "shadow_clone_mode": "off",
                    "regular_execution_mode": "phase2_supervisor",
                }
            ),
        }

    seen_keys = []

    async def _fake_lrange(key, _start, _end):
        seen_keys.append(key)
        if (
            key
            == "agent_run:run-phase2-terminal-no-abandoned-fallback:epoch:2:responses"
        ):
            return []
        if (
            key
            == "agent_run:run-phase2-terminal-no-abandoned-fallback:epoch:1:responses"
        ):
            return [
                json.dumps(
                    {
                        "sequence": 0,
                        "thread_id": "thread-1",
                        "type": "assistant",
                        "content": json.dumps(
                            {
                                "role": "assistant",
                                "content": "attempt-1-abandoned-visible",
                            }
                        ),
                        "metadata": json.dumps({"stream_status": "chunk"}),
                    },
                    ensure_ascii=False,
                )
            ]
        return []

    monkeypatch.setattr(agent_api, "get_user_id_from_stream_auth", _fake_auth)
    monkeypatch.setattr(agent_api, "get_agent_run_with_access_check", _fake_access)
    monkeypatch.setattr(agent_api.redis, "lrange", _fake_lrange)

    response = await agent_api.stream_agent_run(
        agent_run_id="run-phase2-terminal-no-abandoned-fallback",
        token="token",
        request=object(),
        from_index=0,
    )

    chunks = []
    async for item in response.body_iterator:
        chunks.append(item.decode() if isinstance(item, bytes) else item)

    payloads = _stream_data_payloads(chunks)

    assert (
        seen_keys[0]
        == "agent_run:run-phase2-terminal-no-abandoned-fallback:epoch:2:responses"
    )
    assert (
        "agent_run:run-phase2-terminal-no-abandoned-fallback:epoch:1:responses"
        not in seen_keys
    )
    assert not any(
        payload.get("type") == "assistant"
        and json.loads(payload.get("content", "{}")).get("content")
        == "attempt-1-abandoned-visible"
        for payload in payloads
    )


@pytest.mark.asyncio
async def test_stream_agent_run_phase2_attempt_lookup_failure_does_not_fallback_to_run_wide_responses(
    monkeypatch,
):
    class _FailingAttemptQuery:
        def select(self, *_args, **_kwargs):
            return self

        def eq(self, *_args, **_kwargs):
            return self

        async def execute(self):
            raise RuntimeError("attempt lookup unavailable")

    class _FailingAttemptClient:
        def table(self, table_name):
            assert table_name == "regular_run_attempts"
            return _FailingAttemptQuery()

    monkeypatch.setattr(agent_api, "db", _StaticClientDB(_FailingAttemptClient()))

    async def _fake_auth(*args, **kwargs):
        return "user-1"

    async def _fake_access(*args, **kwargs):
        return {
            "agent_run_id": "run-phase2-lookup-failure",
            "status": "completed",
            "thread_id": "thread-1",
            "error": None,
            "metadata": json.dumps(
                {
                    "shadow_clone_mode": "off",
                    "regular_execution_mode": "phase2_supervisor",
                }
            ),
        }

    seen_keys = []

    async def _fake_lrange(key, _start, _end):
        seen_keys.append(key)
        return [
            json.dumps(
                {
                    "sequence": 0,
                    "thread_id": "thread-1",
                    "type": "assistant",
                    "content": json.dumps(
                        {"role": "assistant", "content": "stale-run-wide"}
                    ),
                    "metadata": json.dumps({"stream_status": "chunk"}),
                },
                ensure_ascii=False,
            )
        ]

    monkeypatch.setattr(agent_api, "get_user_id_from_stream_auth", _fake_auth)
    monkeypatch.setattr(agent_api, "get_agent_run_with_access_check", _fake_access)
    monkeypatch.setattr(agent_api.redis, "lrange", _fake_lrange)

    response = await agent_api.stream_agent_run(
        agent_run_id="run-phase2-lookup-failure",
        token="token",
        request=object(),
        from_index=0,
    )

    chunks = []
    async for item in response.body_iterator:
        chunks.append(item.decode() if isinstance(item, bytes) else item)

    payloads = _stream_data_payloads(chunks)

    assert seen_keys == []
    assert not any(
        payload.get("type") == "assistant"
        and json.loads(payload.get("content", "{}")).get("content") == "stale-run-wide"
        for payload in payloads
    )


@pytest.mark.asyncio
async def test_stream_agent_run_phase2_reconnect_after_recovery_replays_current_epoch_from_start(
    monkeypatch,
):
    fake_client = _AttemptClient(
        [
            {
                "attempt_id": "attempt-1",
                "agent_run_id": "run-phase2-reconnect",
                "attempt_number": 1,
                "execution_epoch": 1,
                "status": "abandoned",
            },
            {
                "attempt_id": "attempt-2",
                "agent_run_id": "run-phase2-reconnect",
                "attempt_number": 2,
                "execution_epoch": 2,
                "status": "stopped",
            },
        ]
    )
    monkeypatch.setattr(agent_api, "db", _StaticClientDB(fake_client))

    async def _fake_auth(*args, **kwargs):
        return "user-1"

    async def _fake_access(*args, **kwargs):
        return {
            "agent_run_id": "run-phase2-reconnect",
            "status": "stopped",
            "thread_id": "thread-1",
            "error": None,
            "metadata": json.dumps(
                {
                    "shadow_clone_mode": "off",
                    "regular_execution_mode": "phase2_supervisor",
                }
            ),
        }

    lrange_calls = []

    async def _fake_lrange(key, start, _end):
        lrange_calls.append((key, start))
        if key == "agent_run:run-phase2-reconnect:epoch:2:responses":
            return [
                json.dumps(
                    {
                        "sequence": 0,
                        "thread_id": "thread-1",
                        "type": "assistant",
                        "content": json.dumps(
                            {"role": "assistant", "content": "epoch-2-replayed"}
                        ),
                        "metadata": json.dumps({"stream_status": "chunk"}),
                    },
                    ensure_ascii=False,
                )
            ]
        return []

    monkeypatch.setattr(agent_api, "get_user_id_from_stream_auth", _fake_auth)
    monkeypatch.setattr(agent_api, "get_agent_run_with_access_check", _fake_access)
    monkeypatch.setattr(agent_api.redis, "lrange", _fake_lrange)

    old_epoch_cursor = agent_api._build_phase2_public_event_index(
        execution_epoch=1,
        local_event_index=3,
    )

    response = await agent_api.stream_agent_run(
        agent_run_id="run-phase2-reconnect",
        token="token",
        request=object(),
        from_index=old_epoch_cursor + 1,
    )

    chunks = []
    async for item in response.body_iterator:
        chunks.append(item.decode() if isinstance(item, bytes) else item)

    payloads = _stream_data_payloads(chunks)

    assert lrange_calls[0] == ("agent_run:run-phase2-reconnect:epoch:2:responses", 0)
    assert any(
        payload.get("type") == "assistant"
        and payload.get("event_index")
        == agent_api._build_phase2_public_event_index(
            execution_epoch=2,
            local_event_index=0,
        )
        and json.loads(payload.get("content", "{}")).get("content")
        == "epoch-2-replayed"
        for payload in payloads
    )


@pytest.mark.asyncio
async def test_stream_agent_run_phase2_backlog_trim_uses_epoch_local_start_index(
    monkeypatch,
):
    fake_client = _AttemptClient(
        [
            {
                "attempt_id": "attempt-2",
                "agent_run_id": "run-phase2-backlog-trim",
                "attempt_number": 2,
                "execution_epoch": 2,
                "status": "running",
            }
        ]
    )
    monkeypatch.setattr(agent_api, "db", _StaticClientDB(fake_client))

    async def _fake_auth(*args, **kwargs):
        return "user-1"

    async def _fake_access(*args, **kwargs):
        return {
            "agent_run_id": "run-phase2-backlog-trim",
            "status": "running",
            "thread_id": "thread-1",
            "error": None,
            "metadata": json.dumps(
                {
                    "shadow_clone_mode": "off",
                    "regular_execution_mode": "phase2_supervisor",
                }
            ),
        }

    payloads = [
        {
            "sequence": index,
            "thread_id": "thread-1",
            "type": "assistant",
            "content": json.dumps({"role": "assistant", "content": f"chunk-{index}"}),
            "metadata": json.dumps({"stream_status": "chunk"}),
        }
        for index in range(20)
    ]
    lrange_calls = []

    async def _fake_lrange(key, start, _end):
        lrange_calls.append((key, start))
        if key == "agent_run:run-phase2-backlog-trim:epoch:2:responses":
            return [
                json.dumps(payload, ensure_ascii=False) for payload in payloads[start:]
            ]
        return []

    async def _fake_llen(_key):
        return len(payloads)

    async def _fake_tail_projection(*_args, **_kwargs):
        return None, None

    monkeypatch.setattr(agent_api, "get_user_id_from_stream_auth", _fake_auth)
    monkeypatch.setattr(agent_api, "get_agent_run_with_access_check", _fake_access)
    monkeypatch.setattr(agent_api.redis, "lrange", _fake_lrange)
    monkeypatch.setattr(agent_api.redis, "llen", _fake_llen)
    monkeypatch.setattr(
        agent_api.redis,
        "create_pubsub",
        lambda: asyncio.sleep(0, result=_FakePubSub(messages=[])),
    )
    monkeypatch.setattr(
        agent_api,
        "read_terminal_status_from_response_tail",
        _fake_tail_projection,
    )
    monkeypatch.setattr(agent_api, "AGENT_STREAM_INITIAL_REPLAY_LIMIT", 2)
    monkeypatch.setattr(agent_api, "AGENT_STREAM_RUNNING_CATCHUP_LIMIT", 2)
    monkeypatch.setattr(agent_api, "AGENT_STREAM_PRIORITY_BACKLOG_MULTIPLIER", 2)

    old_epoch_cursor = agent_api._build_phase2_public_event_index(
        execution_epoch=1,
        local_event_index=9,
    )

    response = await agent_api.stream_agent_run(
        agent_run_id="run-phase2-backlog-trim",
        token="token",
        request=_ImmediatelyDisconnectedRequest(),
        from_index=old_epoch_cursor + 1,
    )

    chunks = []
    async for item in response.body_iterator:
        chunks.append(item.decode() if isinstance(item, bytes) else item)

    assert any(
        key == "agent_run:run-phase2-backlog-trim:epoch:2:responses" and start == 16
        for key, start in lrange_calls
    )


@pytest.mark.asyncio
async def test_stream_agent_run_phase2_switches_to_new_attempt_epoch_after_recovery(
    monkeypatch,
):
    fake_client = _SequencedAttemptClient(
        [
            [
                {
                    "attempt_id": "attempt-1",
                    "agent_run_id": "run-phase2-recovery",
                    "attempt_number": 1,
                    "execution_epoch": 1,
                    "status": "running",
                }
            ],
            [
                {
                    "attempt_id": "attempt-1",
                    "agent_run_id": "run-phase2-recovery",
                    "attempt_number": 1,
                    "execution_epoch": 1,
                    "status": "running",
                }
            ],
            [
                {
                    "attempt_id": "attempt-1",
                    "agent_run_id": "run-phase2-recovery",
                    "attempt_number": 1,
                    "execution_epoch": 1,
                    "status": "abandoned",
                },
                {
                    "attempt_id": "attempt-2",
                    "agent_run_id": "run-phase2-recovery",
                    "attempt_number": 2,
                    "execution_epoch": 2,
                    "status": "running",
                },
            ],
        ]
    )
    monkeypatch.setattr(agent_api, "db", _StaticClientDB(fake_client))

    async def _fake_auth(*args, **kwargs):
        return "user-1"

    async def _fake_access(*args, **kwargs):
        return {
            "agent_run_id": "run-phase2-recovery",
            "status": "running",
            "thread_id": "thread-1",
            "error": None,
            "metadata": json.dumps(
                {
                    "shadow_clone_mode": "off",
                    "regular_execution_mode": "phase2_supervisor",
                }
            ),
        }

    seen_keys = []

    async def _fake_lrange(key, _start, _end):
        seen_keys.append(key)
        if key == "agent_run:run-phase2-recovery:epoch:1:responses":
            return []
        if key == "agent_run:run-phase2-recovery:epoch:2:responses":
            return [
                json.dumps(
                    {
                        "sequence": 0,
                        "thread_id": "thread-1",
                        "type": "assistant",
                        "content": json.dumps(
                            {"role": "assistant", "content": "attempt-2-after-recovery"}
                        ),
                        "metadata": json.dumps({"stream_status": "chunk"}),
                    },
                    ensure_ascii=False,
                ),
                json.dumps(
                    {
                        "sequence": 1,
                        "thread_id": "thread-1",
                        "type": "status",
                        "status": "completed",
                        "message": "attempt-2 completed",
                    },
                    ensure_ascii=False,
                ),
            ]
        return []

    async def _fake_llen(_key):
        return 0

    response_pubsub = _FakePubSub(
        messages=[
            {
                "type": "message",
                "channel": b"agent_run:run-phase2-recovery:new_response",
                "data": b"new",
            }
        ]
    )
    control_pubsub = _FakePubSub(messages=[])
    pubsub_index = {"count": 0}

    async def _fake_create_pubsub():
        if pubsub_index["count"] == 0:
            pubsub_index["count"] += 1
            return response_pubsub
        return control_pubsub

    monkeypatch.setattr(agent_api, "get_user_id_from_stream_auth", _fake_auth)
    monkeypatch.setattr(agent_api, "get_agent_run_with_access_check", _fake_access)
    monkeypatch.setattr(
        agent_api,
        "read_terminal_status_from_response_tail",
        lambda *_args, **_kwargs: asyncio.sleep(0, result=(None, None)),
    )
    monkeypatch.setattr(agent_api.redis, "lrange", _fake_lrange)
    monkeypatch.setattr(agent_api.redis, "llen", _fake_llen)
    monkeypatch.setattr(agent_api.redis, "create_pubsub", _fake_create_pubsub)

    response = await agent_api.stream_agent_run(
        agent_run_id="run-phase2-recovery",
        token="token",
        request=_AlwaysConnectedRequest(),
        from_index=0,
    )

    chunks = []
    async for item in response.body_iterator:
        chunks.append(item.decode() if isinstance(item, bytes) else item)

    payloads = _stream_data_payloads(chunks)

    assert seen_keys[0] == "agent_run:run-phase2-recovery:epoch:1:responses"
    assert "agent_run:run-phase2-recovery:epoch:2:responses" in seen_keys
    assert seen_keys.index(
        "agent_run:run-phase2-recovery:epoch:2:responses"
    ) > seen_keys.index(
        "agent_run:run-phase2-recovery:epoch:1:responses",
    )
    assert any(
        payload.get("type") == "assistant"
        and json.loads(payload.get("content", "{}")).get("content")
        == "attempt-2-after-recovery"
        for payload in payloads
    )


@pytest.mark.asyncio
async def test_stream_agent_run_filters_stale_replayed_terminal_for_stopped_late_subscriber(
    monkeypatch,
):
    monkeypatch.setattr(agent_api, "db", _DummyDB())

    async def _fake_auth(*args, **kwargs):
        return "user-1"

    async def _fake_access(*args, **kwargs):
        return {
            "status": "stopped",
            "thread_id": "thread-1",
            "error": None,
        }

    replay_payloads = [
        {
            "sequence": 0,
            "thread_id": "thread-1",
            "type": "assistant",
            "content": json.dumps({"role": "assistant", "content": "kept"}),
            "metadata": json.dumps({"stream_status": "chunk"}),
        },
        {
            "sequence": 1,
            "thread_id": "thread-1",
            "type": "status",
            "status": "completed",
            "message": "stale completion",
        },
    ]

    async def _fake_lrange(_key, _start, _end):
        return [json.dumps(payload, ensure_ascii=False) for payload in replay_payloads]

    monkeypatch.setattr(agent_api, "get_user_id_from_stream_auth", _fake_auth)
    monkeypatch.setattr(agent_api, "get_agent_run_with_access_check", _fake_access)
    monkeypatch.setattr(agent_api.redis, "lrange", _fake_lrange)

    response = await agent_api.stream_agent_run(
        agent_run_id="run-stopped-replay",
        token="token",
        request=object(),
        from_index=0,
    )

    chunks = []
    async for item in response.body_iterator:
        chunks.append(item.decode() if isinstance(item, bytes) else item)

    payloads = _stream_data_payloads(chunks)
    statuses = [
        payload.get("status") for payload in payloads if payload.get("type") == "status"
    ]

    assert any(
        payload.get("type") == "assistant"
        and json.loads(payload.get("content", "{}")).get("content") == "kept"
        for payload in payloads
    )
    assert statuses == ["stopped"]


def test_decorate_stream_response_marks_non_progress_payloads():
    ping_payload = agent_api._decorate_stream_response({"type": "ping"})
    assert ping_payload["non_progress"] is True

    tool_started_payload = agent_api._decorate_stream_response(
        {
            "type": "status",
            "content": json.dumps({"status_type": "tool_started"}),
        }
    )
    assert tool_started_payload["non_progress"] is True

    tool_completed_payload = agent_api._decorate_stream_response(
        {
            "type": "status",
            "content": json.dumps({"status_type": "tool_completed"}),
        }
    )
    assert tool_completed_payload["non_progress"] is True

    tool_failed_payload = agent_api._decorate_stream_response(
        {
            "type": "status",
            "content": json.dumps({"status_type": "tool_failed"}),
        }
    )
    assert tool_failed_payload["non_progress"] is True

    path_only_tool_call_chunk_payload = agent_api._decorate_stream_response(
        {
            "type": "assistant",
            "metadata": json.dumps(
                {
                    "stream_status": "tool_call_chunk",
                    "tool_calls": [
                        {
                            "function": {
                                "name": "write_file",
                                "arguments": json.dumps(
                                    {"path": "/workspace/output.txt"}
                                ),
                            }
                        }
                    ],
                }
            ),
        }
    )
    assert path_only_tool_call_chunk_payload["non_progress"] is True

    meaningful_tool_call_chunk_payload = agent_api._decorate_stream_response(
        {
            "type": "assistant",
            "metadata": json.dumps(
                {
                    "stream_status": "tool_call_chunk",
                    "tool_calls": [
                        {
                            "function": {
                                "name": "write_file",
                                "arguments": json.dumps(
                                    {
                                        "path": "/workspace/output.txt",
                                        "content": "x" * 256,
                                    }
                                ),
                            }
                        }
                    ],
                }
            ),
        }
    )
    assert "non_progress" not in meaningful_tool_call_chunk_payload

    productive_payload = agent_api._decorate_stream_response(
        {
            "type": "assistant",
            "content": json.dumps({"role": "assistant", "content": "ok"}),
            "metadata": json.dumps({"stream_status": "chunk"}),
        }
    )
    assert "non_progress" not in productive_payload
