import asyncio
import json
import sys
import types
from pathlib import Path

import pytest

if "structlog" not in sys.modules:
    class _DummyBoundLogger:
        def info(self, *args, **kwargs):
            return None

        def warning(self, *args, **kwargs):
            return None

        def error(self, *args, **kwargs):
            return None

        def debug(self, *args, **kwargs):
            return None

    class _DummyProcessorFormatter:
        @staticmethod
        def wrap_for_formatter(*args, **kwargs):
            return None

    dummy_structlog = types.SimpleNamespace(
        configure=lambda **kwargs: None,
        get_logger=lambda *args, **kwargs: _DummyBoundLogger(),
        stdlib=types.SimpleNamespace(
            add_log_level=lambda *args, **kwargs: None,
            PositionalArgumentsFormatter=lambda *args, **kwargs: None,
            ProcessorFormatter=_DummyProcessorFormatter,
            LoggerFactory=lambda *args, **kwargs: None,
            BoundLogger=_DummyBoundLogger,
        ),
        processors=types.SimpleNamespace(TimeStamper=lambda *args, **kwargs: None),
        contextvars=types.SimpleNamespace(
            clear_contextvars=lambda: None,
            bind_contextvars=lambda **kwargs: None,
            get_contextvars=lambda: {},
        ),
    )
    sys.modules["structlog"] = dummy_structlog

if "services" not in sys.modules:
    sys.modules["services"] = types.ModuleType("services")

if "services.redis" not in sys.modules:
    async def _noop_async(*_args, **_kwargs):
        return True

    async def _noop_lrange(*_args, **_kwargs):
        return []

    services_redis_stub = types.SimpleNamespace(
        REDIS_KEY_TTL=3600,
        initialize_async=_noop_async,
        set=_noop_async,
        get=_noop_async,
        delete=_noop_async,
        expire=_noop_async,
        publish=_noop_async,
        rpush=_noop_async,
        lrange=_noop_lrange,
    )
    sys.modules["services.redis"] = services_redis_stub
    setattr(sys.modules["services"], "redis", services_redis_stub)
else:
    redis_mod = sys.modules["services.redis"]

    async def _noop_async(*_args, **_kwargs):
        return True

    async def _noop_lrange(*_args, **_kwargs):
        return []

    for attr, value in (
        ("REDIS_KEY_TTL", 3600),
        ("initialize_async", _noop_async),
        ("set", _noop_async),
        ("get", _noop_async),
        ("delete", _noop_async),
        ("expire", _noop_async),
        ("publish", _noop_async),
        ("rpush", _noop_async),
        ("lrange", _noop_lrange),
    ):
        if not hasattr(redis_mod, attr):
            setattr(redis_mod, attr, value)

if "services.postgresql" not in sys.modules:
    services_pg_stub = types.ModuleType("services.postgresql")

    class _DummyDBConnection:
        @property
        async def client(self):
            raise RuntimeError("DB client is not available in this unit test")

    services_pg_stub.DBConnection = _DummyDBConnection
    sys.modules["services.postgresql"] = services_pg_stub
    setattr(sys.modules["services"], "postgresql", services_pg_stub)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agentscope_integration.memory.post_run_review import (  # noqa: E402
    _REVIEW_TASKS,
    _chat_response_to_text,
    _collect_final_chat_response,
    _extract_cache_context,
    build_review_context,
    execute_post_run_review,
    extract_run_metrics,
    generate_memories,
    schedule_post_run_review,
    should_review,
)
from agentscope_integration.memory.review_types import (  # noqa: E402
    ReviewResult,
    ReviewSettings,
)


def _tool_call_chunk_response(
    *,
    tool_call_id: str,
    tool_name: str = "execute_command",
    created_at: str = "2026-03-04T09:00:00+00:00",
) -> dict:
    return {
        "type": "assistant",
        "content": json.dumps({"role": "assistant", "content": ""}, ensure_ascii=False),
        "metadata": json.dumps(
            {
                "stream_status": "tool_call_chunk",
                "tool_calls": [
                    {
                        "id": tool_call_id,
                        "function": {"name": tool_name, "arguments": "{\"cmd\":\"ls\"}"},
                    }
                ],
            },
            ensure_ascii=False,
        ),
        "created_at": created_at,
    }


def _tool_status_response(
    *,
    status_type: str,
    function_name: str = "execute_command",
    message: str = "",
    created_at: str = "2026-03-04T09:00:01+00:00",
) -> dict:
    return {
        "type": "status",
        "content": json.dumps(
            {
                "function_name": function_name,
                "status_type": status_type,
                "message": message,
            },
            ensure_ascii=False,
        ),
        "metadata": json.dumps({"thread_run_id": "run-1"}, ensure_ascii=False),
        "created_at": created_at,
    }


def _tool_result_response(
    *,
    tool_name: str = "execute_command",
    tool_call_id: str = "call-1",
    result: str = "",
    created_at: str = "2026-03-04T09:00:01+00:00",
) -> dict:
    return {
        "type": "tool",
        "content": json.dumps(
            {
                "tool_name": tool_name,
                "tool_call_id": tool_call_id,
                "result": result,
            },
            ensure_ascii=False,
        ),
        "metadata": json.dumps({"thread_run_id": "run-1"}, ensure_ascii=False),
        "created_at": created_at,
    }


def _terminal_status_response(
    *,
    status: str,
    message: str = "",
    created_at: str = "2026-03-04T09:00:05+00:00",
) -> dict:
    return {
        "type": "status",
        "status": status,
        "message": message,
        "metadata": json.dumps({"thread_run_id": "run-1"}, ensure_ascii=False),
        "created_at": created_at,
    }


def test_extract_run_metrics_smooth_run() -> None:
    responses = [
        _tool_call_chunk_response(tool_call_id="call-1"),
        _tool_status_response(status_type="tool_completed", message="ok"),
        _terminal_status_response(status="completed"),
    ]

    metrics = extract_run_metrics(responses, final_status="completed")
    assert metrics.total_tool_calls == 1
    assert metrics.tool_failures == 0
    assert metrics.max_consecutive_failures == 0
    assert metrics.final_status == "completed"
    assert metrics.total_responses == 3


def test_extract_run_metrics_high_friction() -> None:
    responses = [
        _tool_call_chunk_response(tool_call_id="call-1"),
        _tool_status_response(status_type="tool_failed", message="network timeout"),
        _tool_call_chunk_response(tool_call_id="call-2", created_at="2026-03-04T09:00:02+00:00"),
        _tool_status_response(
            status_type="tool_failed",
            message="sandbox unavailable",
            created_at="2026-03-04T09:00:03+00:00",
        ),
        _terminal_status_response(status="failed", message="run failed"),
    ]

    metrics = extract_run_metrics(responses, final_status="failed")
    assert metrics.total_tool_calls == 2
    assert metrics.tool_failures == 2
    assert metrics.max_consecutive_failures >= 2
    assert metrics.final_status == "failed"
    assert metrics.tool_error_details


def test_extract_run_metrics_counts_tool_result_errors() -> None:
    responses = [
        _tool_call_chunk_response(tool_call_id="call-1"),
        _tool_result_response(
            tool_call_id="call-1",
            result=json.dumps(
                {
                    "error": "Command exited with code 1 and error: cat missing file",
                    "command": "cat /workspace/missing",
                },
                ensure_ascii=False,
            ),
        ),
        _terminal_status_response(status="completed"),
    ]
    metrics = extract_run_metrics(responses, final_status="completed")
    assert metrics.total_tool_calls == 1
    assert metrics.tool_failures == 1
    assert metrics.max_consecutive_failures >= 1
    assert metrics.tool_error_details


def test_should_review_gate_rules() -> None:
    settings = ReviewSettings(enabled=True)

    smooth_metrics = extract_run_metrics(
        [
            _tool_call_chunk_response(tool_call_id="call-1"),
            _tool_status_response(status_type="tool_completed"),
            _terminal_status_response(status="completed"),
        ],
        final_status="completed",
    )
    assert should_review(smooth_metrics, settings) is False

    failed_metrics = extract_run_metrics(
        [_terminal_status_response(status="failed", message="fatal")],
        final_status="failed",
    )
    assert should_review(failed_metrics, settings) is True

    stopped_metrics = extract_run_metrics(
        [_terminal_status_response(status="stopped", message="user stop")],
        final_status="stopped",
    )
    assert should_review(stopped_metrics, settings) is False


def test_build_review_context_extracts_failures_and_respects_limit() -> None:
    responses = [
        _tool_call_chunk_response(tool_call_id="call-1"),
        _tool_status_response(status_type="tool_failed", message="permission denied"),
        _tool_status_response(status_type="tool_completed", message="retry ok"),
        _terminal_status_response(status="failed", message="final error"),
    ]
    metrics = extract_run_metrics(responses, final_status="failed")
    context = build_review_context(responses, metrics, max_chars=260)
    assert "tool_failed" in context
    assert "execute_command" in context
    assert len(context) <= 260


def test_build_review_context_extracts_tool_result_failure() -> None:
    responses = [
        _tool_call_chunk_response(tool_call_id="call-1"),
        _tool_result_response(
            tool_call_id="call-1",
            result=json.dumps({"error": "Command exited with code 1"}, ensure_ascii=False),
        ),
        _tool_result_response(
            tool_call_id="call-2",
            result="ok",
            created_at="2026-03-04T09:00:02+00:00",
        ),
        _terminal_status_response(status="completed"),
    ]
    metrics = extract_run_metrics(responses, final_status="completed")
    context = build_review_context(responses, metrics, max_chars=500)
    assert "tool_result_error=true" in context
    assert "execute_command" in context


def test_extract_cache_context_reads_kv_relay_metadata() -> None:
    responses = [
        {
            "type": "assistant",
            "metadata": json.dumps(
                {
                    "thread_run_id": "run-ctx-1",
                    "kv_cache_session_id": "sess-abc",
                                        "model_key": "glm-4.7",
                },
                ensure_ascii=False,
            ),
            "content": json.dumps({"role": "assistant", "content": "ok"}, ensure_ascii=False),
        },
    ]
    context = _extract_cache_context(responses, thread_id="thread-ctx")
    assert context["thread_id"] == "thread-ctx"
    assert context["thread_run_id"] == "run-ctx-1"
    assert context["session_id"] == "sess-abc"
    assert context["model_key"] == "glm-4.7"


@pytest.mark.asyncio
async def test_generate_memories_parses_json(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_invoke_review_llm(**_kwargs):
        return json.dumps(
            {
                "should_record": True,
                "confidence": 0.9,
                "reasons": ["high friction"],
                "task_memories": ["When retries fail twice, switch strategy immediately."],
                "tool_memories": [
                    {
                        "tool_name": "execute_command",
                        "input": {"cmd": "ls"},
                        "output": "works after fallback",
                        "success": True,
                        "time_cost": 0.1,
                    }
                ],
            },
            ensure_ascii=False,
        )

    monkeypatch.setattr(
        "agentscope_integration.memory.post_run_review._invoke_review_llm",
        _fake_invoke_review_llm,
    )

    metrics = extract_run_metrics(
        [_terminal_status_response(status="failed", message="error")],
        final_status="failed",
    )
    result = await generate_memories(
        review_context="failure context",
        metrics=metrics,
        settings=ReviewSettings(enabled=True),
    )

    assert result.should_record is True
    assert result.confidence == pytest.approx(0.9)
    assert len(result.task_memories) == 1
    assert len(result.tool_memories) == 1


@pytest.mark.asyncio
async def test_execute_post_run_review_end_to_end(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeLTM:
        def __init__(self):
            self.entered = False
            self.exited = False
            self.record_calls = []

        async def __aenter__(self):
            self.entered = True
            return self

        async def __aexit__(self, *_args):
            self.exited = True

        async def record_to_memory(self, **kwargs):
            self.record_calls.append(kwargs)

    fake_ltm = _FakeLTM()

    async def _always_lock(*_args, **_kwargs):
        return True

    monkeypatch.setattr(
        "agentscope_integration.memory.post_run_review._acquire_review_lock",
        _always_lock,
    )
    monkeypatch.setattr(
        "agentscope_integration.memory.post_run_review.load_ltm_settings_from_env",
        lambda: types.SimpleNamespace(enabled=True),
    )
    monkeypatch.setattr(
        "agentscope_integration.memory.post_run_review.create_long_term_memory",
        lambda **_kwargs: fake_ltm,
    )

    async def _fake_generate_memories(**_kwargs):
        return ReviewResult(
            should_record=True,
            confidence=0.92,
            task_memories=["Use fallback command path after repeated timeout failures."],
            tool_memories=['{"tool_name":"execute_command","output":"ok"}'],
            reasons=["tool failed repeatedly"],
            raw_response="{}",
        )

    monkeypatch.setattr(
        "agentscope_integration.memory.post_run_review.generate_memories",
        _fake_generate_memories,
    )

    payloads = [
        json.dumps(_tool_call_chunk_response(tool_call_id="call-1"), ensure_ascii=False),
        json.dumps(
            _tool_status_response(status_type="tool_failed", message="timeout"),
            ensure_ascii=False,
        ),
        json.dumps(_terminal_status_response(status="failed", message="final"), ensure_ascii=False),
    ]

    await execute_post_run_review(
        responses_json=payloads,
        thread_id="thread-1",
        agent_run_id="run-1",
        final_status="failed",
        settings=ReviewSettings(enabled=True, confidence_threshold=0.65),
    )

    assert fake_ltm.entered is True
    assert fake_ltm.exited is True
    assert len(fake_ltm.record_calls) == 1
    assert fake_ltm.record_calls[0]["content"]


@pytest.mark.asyncio
async def test_execute_post_run_review_forces_record_on_high_friction(monkeypatch: pytest.MonkeyPatch) -> None:
    class _FakeLTM:
        def __init__(self):
            self.record_calls = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def record_to_memory(self, **kwargs):
            self.record_calls.append(kwargs)

    fake_ltm = _FakeLTM()

    async def _always_lock(*_args, **_kwargs):
        return True

    monkeypatch.setattr(
        "agentscope_integration.memory.post_run_review._acquire_review_lock",
        _always_lock,
    )
    monkeypatch.setattr(
        "agentscope_integration.memory.post_run_review.load_ltm_settings_from_env",
        lambda: types.SimpleNamespace(enabled=True),
    )
    monkeypatch.setattr(
        "agentscope_integration.memory.post_run_review.create_long_term_memory",
        lambda **_kwargs: fake_ltm,
    )

    async def _fake_generate_memories(**_kwargs):
        return ReviewResult(
            should_record=False,
            confidence=0.2,
            task_memories=[],
            tool_memories=[],
            reasons=["llm said no"],
            raw_response="{\"should_record\":false}",
        )

    monkeypatch.setattr(
        "agentscope_integration.memory.post_run_review.generate_memories",
        _fake_generate_memories,
    )

    payloads = [
        json.dumps(_tool_call_chunk_response(tool_call_id="call-1"), ensure_ascii=False),
        json.dumps(
            _tool_result_response(
                tool_call_id="call-1",
                result=json.dumps(
                    {"error": "Command exited with code 1 and error: missing file"},
                    ensure_ascii=False,
                ),
            ),
            ensure_ascii=False,
        ),
        json.dumps(_terminal_status_response(status="completed"), ensure_ascii=False),
    ]

    await execute_post_run_review(
        responses_json=payloads,
        thread_id="thread-override",
        agent_run_id="run-override-1",
        final_status="completed",
        settings=ReviewSettings(
            enabled=True,
            tool_failure_threshold=1,
            min_responses=0,
            confidence_threshold=0.65,
            force_record_on_high_friction=True,
        ),
    )

    assert len(fake_ltm.record_calls) == 1
    recorded = fake_ltm.record_calls[0]["content"]
    assert any("high_friction_rule_override" in item for item in recorded)


@pytest.mark.asyncio
async def test_schedule_post_run_review_fire_and_forget(monkeypatch: pytest.MonkeyPatch) -> None:
    _REVIEW_TASKS.clear()
    event = asyncio.Event()

    monkeypatch.setattr(
        "agentscope_integration.memory.post_run_review.load_review_settings_from_env",
        lambda: ReviewSettings(enabled=True, review_timeout_seconds=5.0),
    )

    async def _fake_execute_post_run_review(**_kwargs):
        event.set()

    monkeypatch.setattr(
        "agentscope_integration.memory.post_run_review.execute_post_run_review",
        _fake_execute_post_run_review,
    )

    schedule_post_run_review(
        responses_json=[],
        thread_id="thread-1",
        agent_run_id="run-schedule-1",
        final_status="completed",
    )

    await asyncio.wait_for(event.wait(), timeout=1.0)
    await asyncio.sleep(0)
    assert "run-schedule-1" not in _REVIEW_TASKS


@pytest.mark.asyncio
async def test_response_helpers_tolerate_broken_dunder_getattr() -> None:
    class _BrokenDunderLookup:
        def __getattr__(self, name):
            raise KeyError(name)

        def __str__(self):
            return "broken-dunder-lookup"

    response = _BrokenDunderLookup()
    final = await _collect_final_chat_response(response)
    assert final is response
    assert _chat_response_to_text(response) == "broken-dunder-lookup"
