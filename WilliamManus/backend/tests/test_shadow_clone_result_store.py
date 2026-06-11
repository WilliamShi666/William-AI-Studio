"""Tests for Redis-based Shadow Clone result storage."""

import importlib.util
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from agentscope_integration.shadow_clone.result_store import (
    ShadowCloneResultClaimLostError,
    ShadowCloneResultStateUnavailableError,
    _truncate_to_summary,
    cleanup_results,
    delete_results,
    ensure_terminal_result_summaries_visible,
    read_full_result,
    read_summaries,
    submit_result,
)

BACKEND_ROOT = Path(__file__).resolve().parents[1]


def _load_real_redis_service_module():
    module_spec = importlib.util.spec_from_file_location(
        "shadow_clone_result_store_redis_service_for_test",
        BACKEND_ROOT / "services" / "redis.py",
    )
    assert module_spec is not None and module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_submit_result_uses_atomic_lua_write():
    with patch("agentscope_integration.shadow_clone.result_store.redis_service") as mock_redis:
        mock_redis.eval_script = AsyncMock(return_value=1)

        result = await submit_result(
            run_id="run-123",
            subtask_id="task-1",
            role="researcher",
            status="completed",
            full_result="Detailed result text",
        )

        assert result == {"status": "submitted", "subtask_id": "task-1"}
        mock_redis.eval_script.assert_called_once()
        kwargs = mock_redis.eval_script.call_args.kwargs
        assert kwargs["keys"] == [
            "shadow_clone:run-123:results",
            "shadow_clone:run-123:full:task-1",
            "shadow_clone:run-123:results:summary_cache",
        ]
        cache_row = json.loads(kwargs["args"][2])
        assert cache_row["subtask_id"] == "task-1"
        assert cache_row["role"] == "researcher"


@pytest.mark.asyncio
async def test_submit_result_accepts_summary_override_and_late_note_count():
    with patch("agentscope_integration.shadow_clone.result_store.redis_service") as mock_redis:
        mock_redis.eval_script = AsyncMock(return_value=1)

        await submit_result(
            run_id="run-123",
            subtask_id="task-1",
            role="researcher",
            status="completed",
            full_result="Base result\n\n[Late Peer Notes]\n- extra note",
            summary_override="Base result",
            late_peer_notes_count=1,
        )

        row_json = mock_redis.eval_script.call_args.kwargs["args"][1]
        row = json.loads(row_json)
        assert row["summary"] == "Base result"
        assert row["late_peer_notes_count"] == 1
        assert "full_key" not in row


@pytest.mark.asyncio
async def test_submit_result_persists_attempt_metadata():
    with patch("agentscope_integration.shadow_clone.result_store.redis_service") as mock_redis:
        mock_redis.eval_script = AsyncMock(return_value=1)

        await submit_result(
            run_id="run-123",
            subtask_id="task-1",
            role="researcher",
            status="failed",
            full_result="tool failed",
            attempt_index=2,
            failure_class="tooling",
        )

        row_json = mock_redis.eval_script.call_args.kwargs["args"][1]
        row = json.loads(row_json)
        assert row["attempt_index"] == 2
        assert row["failure_class"] == "tooling"


@pytest.mark.asyncio
async def test_read_summaries_sorted_by_submitted_time():
    mock_rows = {
        "task-2": (
            '{"role":"analyst","status":"completed","summary":"B",'
            '"full_key":"shadow_clone:run-123:full:task-2","submitted_at":"2026-03-05T10:05:00Z",'
            '"attempt_index":2,"failure_class":"timeout"}'
        ),
        "task-1": (
            '{"role":"researcher","status":"completed","summary":"A",'
            '"full_key":"shadow_clone:run-123:full:task-1","submitted_at":"2026-03-05T10:00:00Z"}'
        ),
    }
    with patch("agentscope_integration.shadow_clone.result_store.redis_service") as mock_redis:
        mock_redis.get = AsyncMock(return_value=None)
        mock_redis.hgetall = AsyncMock(return_value=mock_rows)
        mock_redis.set = AsyncMock(return_value=True)

        summaries = await read_summaries("run-123")

        assert [row["subtask_id"] for row in summaries] == ["task-1", "task-2"]
        assert summaries[0]["role"] == "researcher"
        assert summaries[1]["role"] == "analyst"
        assert summaries[1]["attempt_index"] == 2
        assert summaries[1]["failure_class"] == "timeout"
        assert "full_key" not in summaries[0]
        assert "full_key" not in summaries[1]
        mock_redis.set.assert_called_once()
        assert (
            mock_redis.set.call_args.args[0]
            == "shadow_clone:run-123:results:summary_cache"
        )


@pytest.mark.asyncio
async def test_read_summaries_prefers_cached_payload_and_skips_hash_scan():
    cached_rows = json.dumps(
        [
            {
                "subtask_id": "task-1",
                "role": "researcher",
                "status": "completed",
                "summary": "A",
                "submitted_at": "2026-03-05T10:00:00Z",
                "full_key": "shadow_clone:run-123:full:task-1",
            },
            {
                "subtask_id": "task-2",
                "role": "analyst",
                "status": "completed",
                "summary": "B",
                "submitted_at": "2026-03-05T10:05:00Z",
                "failure_class": "timeout",
            },
        ],
    )
    with patch("agentscope_integration.shadow_clone.result_store.redis_service") as mock_redis:
        mock_redis.get = AsyncMock(return_value=cached_rows)
        mock_redis.hgetall = AsyncMock()

        summaries = await read_summaries("run-123")

        assert [row["subtask_id"] for row in summaries] == ["task-1", "task-2"]
        assert summaries[0]["summary"] == "A"
        assert summaries[1]["failure_class"] == "timeout"
        assert "full_key" not in summaries[0]
        mock_redis.hgetall.assert_not_called()


@pytest.mark.asyncio
async def test_read_full_result_found_and_not_found():
    with patch("agentscope_integration.shadow_clone.result_store.redis_service") as mock_redis:
        mock_redis.get = AsyncMock(side_effect=["full text", None])

        found = await read_full_result("run-123", "task-1")
        missing = await read_full_result("run-123", "task-missing")

        assert found == "full text"
        assert missing is None


@pytest.mark.asyncio
async def test_cleanup_results_deletes_sheet_and_full_keys():
    with patch("agentscope_integration.shadow_clone.result_store.redis_service") as mock_redis:
        mock_redis.hgetall = AsyncMock(
            return_value={
                "task-1": '{"summary":"a"}',
                "task-2": '{"summary":"b"}',
            },
        )
        mock_redis.delete = AsyncMock(side_effect=[1, 0, 1, 1])

        deleted = await cleanup_results("run-123")

        assert deleted == 3
        assert mock_redis.delete.call_count == 4


@pytest.mark.asyncio
async def test_redis_hdel_delegates_to_hash_delete_with_timeout():
    redis_service_module = _load_real_redis_service_module()
    mock_client = AsyncMock()
    mock_client.hdel = AsyncMock(return_value=2)

    async def run_operation(_operation, call, *, timeout=None):
        assert timeout == 1.5
        return await call()

    with patch.object(
        redis_service_module,
        "get_client",
        AsyncMock(return_value=mock_client),
        create=True,
    ), patch.object(
        redis_service_module,
        "_run_redis_operation",
        AsyncMock(side_effect=run_operation),
        create=True,
    ) as run_operation_mock:
        deleted = await redis_service_module.hdel(
            "shadow_clone:run-123:results",
            "task-1",
            "task-2",
            timeout=1.5,
        )

    assert deleted == 2
    mock_client.hdel.assert_awaited_once_with(
        "shadow_clone:run-123:results",
        "task-1",
        "task-2",
    )
    run_operation_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_delete_results_invalidates_summary_cache():
    with patch("agentscope_integration.shadow_clone.result_store.redis_service") as mock_redis:
        mock_redis.hdel = AsyncMock(return_value=1)
        mock_redis.delete = AsyncMock(side_effect=[1, 0, 1])

        deleted = await delete_results("run-123", ["task-1"])

        assert deleted == 2
        mock_redis.hdel.assert_awaited_once_with(
            "shadow_clone:run-123:results",
            "task-1",
        )
        assert mock_redis.delete.call_args_list[-1].args[0] == (
            "shadow_clone:run-123:results:summary_cache"
        )


@pytest.mark.asyncio
async def test_ensure_terminal_result_summaries_visible_backfills_missing_terminal_rows():
    with patch(
        "agentscope_integration.shadow_clone.result_store.read_summaries",
        AsyncMock(
            return_value=[
                {
                    "subtask_id": "task-1",
                    "role": "researcher",
                    "status": "running",
                    "summary": "Subagent began executing tool: write_file",
                }
            ]
        ),
    ), patch(
        "agentscope_integration.shadow_clone.result_store.submit_result",
        AsyncMock(return_value={"status": "submitted", "subtask_id": "task-1"}),
    ) as submit_result_mock:
        updated = await ensure_terminal_result_summaries_visible(
            "run-123",
            [
                {
                    "subtask_id": "task-1",
                    "role": "researcher",
                    "status": "completed",
                    "attempt_index": 1,
                }
            ],
        )

        assert updated == ["task-1"]
        submit_result_mock.assert_awaited_once_with(
            run_id="run-123",
            subtask_id="task-1",
            role="researcher",
            status="completed",
            full_result="Subagent completed without a visible textual result.",
            summary_override="Subagent completed without a visible textual result.",
            attempt_index=1,
            failure_class=None,
            timeout=None,
        )


@pytest.mark.asyncio
async def test_ensure_terminal_result_summaries_visible_skips_when_terminal_row_is_visible():
    with patch(
        "agentscope_integration.shadow_clone.result_store.read_summaries",
        AsyncMock(
            return_value=[
                {
                    "subtask_id": "task-1",
                    "role": "researcher",
                    "status": "failed",
                    "summary": "tool failed",
                }
            ]
        ),
    ), patch(
        "agentscope_integration.shadow_clone.result_store.submit_result",
        AsyncMock(),
    ) as submit_result_mock:
        updated = await ensure_terminal_result_summaries_visible(
            "run-123",
            [
                {
                    "subtask_id": "task-1",
                    "role": "researcher",
                    "status": "failed",
                    "error": "tool failed",
                    "attempt_index": 2,
                    "failure_class": "tooling",
                }
            ],
        )

        assert updated == []
        submit_result_mock.assert_not_awaited()


def test_truncate_to_summary_behavior():
    short = "short"
    assert _truncate_to_summary(short, 200) == "short"

    long_text = ("Sentence one. Sentence two. " * 30).strip()
    summary = _truncate_to_summary(long_text, 200)
    assert len(summary) <= 201
    assert summary.endswith(".") or summary.endswith("...")


@pytest.mark.asyncio
async def test_submit_result_raises_when_epoch_state_is_missing():
    with patch("agentscope_integration.shadow_clone.result_store.redis_service") as mock_redis:
        mock_redis.eval_script = AsyncMock(return_value=0)

        with pytest.raises(ShadowCloneResultStateUnavailableError):
            await submit_result(
                run_id="run-404",
                subtask_id="task-1",
                role="researcher",
                status="completed",
                full_result="done",
                expected_epoch=0,
            )


@pytest.mark.asyncio
async def test_submit_result_raises_when_claim_is_lost():
    with patch("agentscope_integration.shadow_clone.result_store.redis_service") as mock_redis:
        mock_redis.eval_script = AsyncMock(return_value=-2)

        with pytest.raises(ShadowCloneResultClaimLostError):
            await submit_result(
                run_id="run-claim-lost",
                subtask_id="task-1",
                role="researcher",
                status="completed",
                full_result="done",
                expected_epoch=0,
                owner_token="claim-1",
            )
