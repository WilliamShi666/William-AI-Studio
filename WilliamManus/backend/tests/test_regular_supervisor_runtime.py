from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from services import regular_supervisor_runtime


@pytest.mark.asyncio
async def test_run_supervisor_loop_claims_up_to_pool_limit_and_drains_queue(
    monkeypatch,
):
    claimed_attempts = [
        {"attempt_id": "a1", "agent_run_id": "run-1"},
        {"attempt_id": "a2", "agent_run_id": "run-2"},
        {"attempt_id": "a3", "agent_run_id": "run-3"},
    ]
    started_attempts: list[str] = []
    peak_concurrency = 0
    current_concurrency = 0

    async def _fake_claim(*_args, **_kwargs):
        if claimed_attempts:
            return claimed_attempts.pop(0)
        return None

    async def _fake_mark_running(_client, *, attempt_id: str):
        return {
            "attempt_id": attempt_id,
            "agent_run_id": f"run-{attempt_id[-1]}",
            "status": "running",
        }

    async def _fake_executor(attempt_row, _owner_token):
        nonlocal peak_concurrency, current_concurrency
        started_attempts.append(attempt_row["attempt_id"])
        current_concurrency += 1
        peak_concurrency = max(peak_concurrency, current_concurrency)
        try:
            await asyncio.sleep(0.01)
        finally:
            current_concurrency -= 1

    monkeypatch.setattr(
        regular_supervisor_runtime.regular_run_attempts,
        "claim_next_attempt",
        _fake_claim,
    )
    monkeypatch.setattr(
        regular_supervisor_runtime.regular_run_attempts,
        "mark_attempt_running",
        _fake_mark_running,
    )

    completed = await regular_supervisor_runtime.run_supervisor_loop(
        client=object(),
        owner_token="sup-1:slot-1",
        supervisor_id="sup-1",
        executor=_fake_executor,
        max_concurrency=2,
    )

    assert started_attempts == ["a1", "a2", "a3"]
    assert sorted(completed) == ["a1", "a2", "a3"]
    assert peak_concurrency == 2


@pytest.mark.asyncio
async def test_run_supervisor_loop_returns_attempt_ids_in_completion_order(
    monkeypatch,
):
    claimed_attempts = [
        {"attempt_id": "a1", "agent_run_id": "run-1"},
        {"attempt_id": "a2", "agent_run_id": "run-2"},
        {"attempt_id": "a3", "agent_run_id": "run-3"},
    ]
    delays = {
        "a1": 0.03,
        "a2": 0.01,
        "a3": 0.0,
    }

    async def _fake_claim(*_args, **_kwargs):
        if claimed_attempts:
            return claimed_attempts.pop(0)
        return None

    async def _fake_mark_running(_client, *, attempt_id: str):
        return {
            "attempt_id": attempt_id,
            "agent_run_id": f"run-{attempt_id[-1]}",
            "status": "running",
        }

    async def _fake_executor(attempt_row, _owner_token):
        await asyncio.sleep(delays[attempt_row["attempt_id"]])

    monkeypatch.setattr(
        regular_supervisor_runtime.regular_run_attempts,
        "claim_next_attempt",
        _fake_claim,
    )
    monkeypatch.setattr(
        regular_supervisor_runtime.regular_run_attempts,
        "mark_attempt_running",
        _fake_mark_running,
    )

    completed = await regular_supervisor_runtime.run_supervisor_loop(
        client=object(),
        owner_token="sup-1:slot-1",
        supervisor_id="sup-1",
        executor=_fake_executor,
        max_concurrency=2,
    )

    assert completed == ["a2", "a3", "a1"]


@pytest.mark.asyncio
async def test_run_supervisor_loop_logs_executor_failures_and_continues(
    monkeypatch,
):
    claimed_attempts = [
        {"attempt_id": "a1", "agent_run_id": "run-1"},
        {"attempt_id": "a2", "agent_run_id": "run-2"},
        {"attempt_id": "a3", "agent_run_id": "run-3"},
    ]
    started_attempts: list[str] = []
    reconcile_calls: list[str] = []

    async def _fake_claim(*_args, **_kwargs):
        if claimed_attempts:
            return claimed_attempts.pop(0)
        return None

    async def _fake_mark_running(_client, *, attempt_id: str):
        return {
            "attempt_id": attempt_id,
            "agent_run_id": f"run-{attempt_id[-1]}",
            "status": "running",
        }

    async def _fake_reconcile(*_args, **_kwargs):
        reconcile_calls.append(_kwargs["attempt_id"])
        return None

    async def _fake_executor(attempt_row, _owner_token):
        started_attempts.append(attempt_row["attempt_id"])
        if attempt_row["attempt_id"] == "a2":
            raise RuntimeError("boom")
        await asyncio.sleep(0)

    monkeypatch.setattr(
        regular_supervisor_runtime.regular_run_attempts,
        "claim_next_attempt",
        _fake_claim,
    )
    monkeypatch.setattr(
        regular_supervisor_runtime.regular_run_attempts,
        "mark_attempt_running",
        _fake_mark_running,
    )
    monkeypatch.setattr(
        regular_supervisor_runtime.regular_run_attempts,
        "reconcile_lost_attempt",
        _fake_reconcile,
    )

    completed = await regular_supervisor_runtime.run_supervisor_loop(
        client=object(),
        owner_token="sup-1:slot-1",
        supervisor_id="sup-1",
        executor=_fake_executor,
        max_concurrency=2,
    )

    assert started_attempts == ["a1", "a2", "a3"]
    assert sorted(completed) == ["a1", "a2", "a3"]
    assert reconcile_calls == []


@pytest.mark.asyncio
async def test_run_supervisor_loop_rejects_non_positive_max_concurrency():
    with pytest.raises(ValueError, match="max_concurrency must be positive"):
        await regular_supervisor_runtime.run_supervisor_loop(
            client=object(),
            owner_token="sup-1:slot-1",
            supervisor_id="sup-1",
            executor=lambda *_args, **_kwargs: None,
            max_concurrency=0,
        )


@pytest.mark.asyncio
async def test_run_supervisor_loop_marks_claimed_attempt_running_before_execution(
    monkeypatch,
):
    claimed_attempts = [
        {"attempt_id": "a1", "agent_run_id": "run-1", "status": "claimed"},
    ]
    executor_rows: list[dict] = []

    async def _fake_claim(*_args, **_kwargs):
        if claimed_attempts:
            return claimed_attempts.pop(0)
        return None

    async def _fake_mark_running(*_args, **_kwargs):
        return {
            "attempt_id": "a1",
            "agent_run_id": "run-1",
            "status": "running",
        }

    async def _fake_executor(attempt_row, _owner_token):
        executor_rows.append(dict(attempt_row))

    monkeypatch.setattr(
        regular_supervisor_runtime.regular_run_attempts,
        "claim_next_attempt",
        _fake_claim,
    )
    monkeypatch.setattr(
        regular_supervisor_runtime.regular_run_attempts,
        "mark_attempt_running",
        _fake_mark_running,
    )

    await regular_supervisor_runtime.run_supervisor_loop(
        client=object(),
        owner_token="sup-1:slot-1",
        supervisor_id="sup-1",
        executor=_fake_executor,
        max_concurrency=1,
    )

    assert executor_rows == [
        {
            "attempt_id": "a1",
            "agent_run_id": "run-1",
            "status": "running",
        }
    ]


@pytest.mark.asyncio
async def test_run_supervisor_loop_finalizes_attempt_after_executor_returns(
    monkeypatch,
):
    claimed_attempts = [
        {"attempt_id": "a1", "agent_run_id": "run-1", "status": "claimed"},
    ]
    finalize_calls: list[str] = []

    async def _fake_claim(*_args, **_kwargs):
        if claimed_attempts:
            return claimed_attempts.pop(0)
        return None

    async def _fake_mark_running(*_args, **_kwargs):
        return {
            "attempt_id": "a1",
            "agent_run_id": "run-1",
            "status": "running",
        }

    async def _fake_executor(*_args, **_kwargs):
        await asyncio.sleep(0)

    async def _fake_finalize(*_args, **_kwargs):
        finalize_calls.append(_kwargs["attempt_id"])
        return {
            "attempt_id": _kwargs["attempt_id"],
            "status": "completed",
        }

    monkeypatch.setattr(
        regular_supervisor_runtime.regular_run_attempts,
        "claim_next_attempt",
        _fake_claim,
    )
    monkeypatch.setattr(
        regular_supervisor_runtime.regular_run_attempts,
        "mark_attempt_running",
        _fake_mark_running,
    )
    monkeypatch.setattr(
        regular_supervisor_runtime.regular_run_attempts,
        "finalize_completed_attempt",
        _fake_finalize,
        raising=False,
    )

    completed = await regular_supervisor_runtime.run_supervisor_loop(
        client=object(),
        owner_token="sup-1:slot-1",
        supervisor_id="sup-1",
        executor=_fake_executor,
        max_concurrency=1,
    )

    assert completed == ["a1"]
    assert finalize_calls == ["a1"]


@pytest.mark.asyncio
async def test_run_supervisor_loop_requeues_lost_attempt_after_supervisor_loss(
    monkeypatch,
):
    claimed_attempts = [
        {"attempt_id": "a1", "agent_run_id": "run-1", "status": "claimed"},
    ]
    reconcile_calls: list[tuple[str, str]] = []

    async def _fake_claim(*_args, **_kwargs):
        if claimed_attempts:
            return claimed_attempts.pop(0)
        return None

    async def _fake_mark_running(*_args, **_kwargs):
        return {
            "attempt_id": "a1",
            "agent_run_id": "run-1",
            "status": "running",
        }

    async def _fake_reconcile(*_args, **_kwargs):
        reconcile_calls.append(
            (_kwargs["attempt_id"], _kwargs["recovery_reason"])
        )
        return {
            "attempt_id": "a2",
            "agent_run_id": "run-1",
            "attempt_number": 2,
            "execution_epoch": 2,
            "status": "queued",
        }

    async def _failing_executor(_attempt_row, _owner_token):
        raise regular_supervisor_runtime.RegularSupervisorLostError(
            "supervisor dropped"
        )

    monkeypatch.setattr(
        regular_supervisor_runtime.regular_run_attempts,
        "claim_next_attempt",
        _fake_claim,
    )
    monkeypatch.setattr(
        regular_supervisor_runtime.regular_run_attempts,
        "mark_attempt_running",
        _fake_mark_running,
    )
    monkeypatch.setattr(
        regular_supervisor_runtime.regular_run_attempts,
        "reconcile_lost_attempt",
        _fake_reconcile,
    )

    completed = await regular_supervisor_runtime.run_supervisor_loop(
        client=object(),
        owner_token="sup-1:slot-1",
        supervisor_id="sup-1",
        executor=_failing_executor,
        max_concurrency=1,
    )

    assert completed == ["a1"]
    assert reconcile_calls == [("a1", "supervisor_lost")]


@pytest.mark.asyncio
async def test_run_supervisor_loop_refreshes_attempt_heartbeat_while_executor_runs(
    monkeypatch,
):
    claimed_attempts = [
        {"attempt_id": "a1", "agent_run_id": "run-1", "status": "claimed"},
    ]
    refresh_calls: list[tuple[str, str]] = []
    executor_release = asyncio.Event()

    async def _fake_claim(*_args, **_kwargs):
        if claimed_attempts:
            return claimed_attempts.pop(0)
        return None

    async def _fake_mark_running(*_args, **_kwargs):
        return {
            "attempt_id": "a1",
            "agent_run_id": "run-1",
            "status": "running",
        }

    async def _fake_refresh(*_args, **_kwargs):
        refresh_calls.append((_kwargs["attempt_id"], _kwargs["owner_token"]))
        if len(refresh_calls) >= 2:
            executor_release.set()
        return {
            "attempt_id": _kwargs["attempt_id"],
            "status": "running",
        }

    async def _fake_finalize(*_args, **_kwargs):
        return {
            "attempt_id": _kwargs["attempt_id"],
            "status": "completed",
        }

    async def _fake_executor(*_args, **_kwargs):
        await asyncio.wait_for(executor_release.wait(), timeout=0.5)

    monkeypatch.setattr(
        regular_supervisor_runtime.regular_run_attempts,
        "claim_next_attempt",
        _fake_claim,
    )
    monkeypatch.setattr(
        regular_supervisor_runtime.regular_run_attempts,
        "mark_attempt_running",
        _fake_mark_running,
    )
    monkeypatch.setattr(
        regular_supervisor_runtime.regular_run_attempts,
        "refresh_attempt_heartbeat",
        _fake_refresh,
        raising=False,
    )
    monkeypatch.setattr(
        regular_supervisor_runtime.regular_run_attempts,
        "finalize_completed_attempt",
        _fake_finalize,
        raising=False,
    )
    monkeypatch.setattr(
        regular_supervisor_runtime.regular_run_attempts,
        "reconcile_expired_attempts",
        lambda *_args, **_kwargs: asyncio.sleep(0, result=[]),
        raising=False,
    )
    monkeypatch.setattr(
        regular_supervisor_runtime.regular_run_attempts,
        "has_recoverable_attempts",
        lambda *_args, **_kwargs: asyncio.sleep(0, result=False),
        raising=False,
    )
    monkeypatch.setattr(
        regular_supervisor_runtime,
        "_get_attempt_heartbeat_interval_seconds",
        lambda: 0.01,
        raising=False,
    )

    completed = await regular_supervisor_runtime.run_supervisor_loop(
        client=object(),
        owner_token="sup-1:slot-1",
        supervisor_id="sup-1",
        executor=_fake_executor,
        max_concurrency=1,
    )

    assert completed == ["a1"]
    assert len(refresh_calls) >= 2
    assert refresh_calls[0] == ("a1", "sup-1:slot-1")


@pytest.mark.asyncio
async def test_run_supervisor_loop_passes_control_plane_slot_to_executor_and_unregisters_on_completion(
    monkeypatch,
):
    claimed_attempts = [
        {
            "attempt_id": "a1",
            "agent_run_id": "run-1",
            "execution_epoch": 3,
            "status": "claimed",
        },
    ]
    received_slots: list[object] = []
    refresh_calls: list[tuple[str, str]] = []
    refresh_observed = asyncio.Event()
    plane_instances = []

    class _FakeControlPlane:
        def __init__(self, *, supervisor_id: str, **_kwargs):
            self.supervisor_id = supervisor_id
            self.registered_slots = {}
            self.unregistered_agent_run_ids: list[str] = []
            self.closed = False
            plane_instances.append(self)

        def register(
            self,
            *,
            agent_run_id: str,
            attempt_id: str,
            execution_epoch: int,
            owner_token: str,
        ):
            slot = SimpleNamespace(
                agent_run_id=agent_run_id,
                attempt_id=attempt_id,
                execution_epoch=execution_epoch,
                owner_token=owner_token,
                stop_requested=False,
                control_plane_error=None,
                refresh_callback=None,
            )

            def _set_refresh_callback(callback):
                slot.refresh_callback = callback

            slot.set_refresh_callback = _set_refresh_callback
            self.registered_slots[agent_run_id] = slot
            return slot

        def unregister(self, *, agent_run_id: str):
            self.unregistered_agent_run_ids.append(agent_run_id)
            self.registered_slots.pop(agent_run_id, None)

        async def run_refresh_tick(self, refresh_callback=None):
            for slot in list(self.registered_slots.values()):
                resolved_callback = refresh_callback or slot.refresh_callback
                if resolved_callback is not None:
                    await resolved_callback(slot)

        async def run_stop_listener(self):
            await asyncio.Future()

        async def aclose(self):
            self.closed = True

    async def _fake_claim(*_args, **_kwargs):
        if claimed_attempts:
            return claimed_attempts.pop(0)
        return None

    async def _fake_mark_running(*_args, **_kwargs):
        return {
            "attempt_id": "a1",
            "agent_run_id": "run-1",
            "execution_epoch": 3,
            "status": "running",
        }

    async def _fake_refresh(*_args, **_kwargs):
        refresh_calls.append((_kwargs["attempt_id"], _kwargs["owner_token"]))
        refresh_observed.set()
        return {
            "attempt_id": _kwargs["attempt_id"],
            "status": "running",
        }

    async def _fake_finalize(*_args, **_kwargs):
        return {
            "attempt_id": _kwargs["attempt_id"],
            "status": "completed",
        }

    async def _fake_executor(attempt_row, _owner_token, *, control_plane_slot=None):
        received_slots.append(control_plane_slot)
        await asyncio.wait_for(refresh_observed.wait(), timeout=0.5)
        assert attempt_row["attempt_id"] == "a1"

    monkeypatch.setattr(
        regular_supervisor_runtime.regular_run_attempts,
        "claim_next_attempt",
        _fake_claim,
    )
    monkeypatch.setattr(
        regular_supervisor_runtime.regular_run_attempts,
        "mark_attempt_running",
        _fake_mark_running,
    )
    monkeypatch.setattr(
        regular_supervisor_runtime.regular_run_attempts,
        "refresh_attempt_heartbeat",
        _fake_refresh,
        raising=False,
    )
    monkeypatch.setattr(
        regular_supervisor_runtime.regular_run_attempts,
        "finalize_completed_attempt",
        _fake_finalize,
        raising=False,
    )
    monkeypatch.setattr(
        regular_supervisor_runtime.regular_run_attempts,
        "reconcile_expired_attempts",
        lambda *_args, **_kwargs: asyncio.sleep(0, result=[]),
        raising=False,
    )
    monkeypatch.setattr(
        regular_supervisor_runtime.regular_run_attempts,
        "has_recoverable_attempts",
        lambda *_args, **_kwargs: asyncio.sleep(0, result=False),
        raising=False,
    )
    monkeypatch.setattr(
        regular_supervisor_runtime,
        "RegularSupervisorControlPlane",
        _FakeControlPlane,
        raising=False,
    )
    monkeypatch.setattr(
        regular_supervisor_runtime,
        "_get_supervisor_idle_poll_interval_seconds",
        lambda: 0.01,
        raising=False,
    )
    monkeypatch.setattr(
        regular_supervisor_runtime,
        "_get_supervisor_idle_exit_seconds",
        lambda: 0.05,
        raising=False,
    )

    completed = await regular_supervisor_runtime.run_supervisor_loop(
        client=object(),
        owner_token="sup-1:slot-1",
        supervisor_id="sup-1",
        executor=_fake_executor,
        max_concurrency=1,
    )

    assert completed == ["a1"]
    assert len(received_slots) == 1
    assert received_slots[0].agent_run_id == "run-1"
    assert refresh_calls == [("a1", "sup-1:slot-1")]
    assert plane_instances[0].unregistered_agent_run_ids == ["run-1"]
    assert plane_instances[0].closed is True


@pytest.mark.asyncio
async def test_run_supervisor_loop_does_not_idle_exit_when_persistent_mode_enabled(
    monkeypatch,
):
    monkeypatch.setenv("REGULAR_SUPERVISOR_PERSISTENT", "true")

    async def _fake_claim(*_args, **_kwargs):
        await asyncio.sleep(0)
        return None

    monkeypatch.setattr(
        regular_supervisor_runtime.regular_run_attempts,
        "claim_next_attempt",
        _fake_claim,
    )
    monkeypatch.setattr(
        regular_supervisor_runtime.regular_run_attempts,
        "reconcile_expired_attempts",
        lambda *_args, **_kwargs: asyncio.sleep(0, result=[]),
        raising=False,
    )
    monkeypatch.setattr(
        regular_supervisor_runtime.regular_run_attempts,
        "has_recoverable_attempts",
        lambda *_args, **_kwargs: asyncio.sleep(0, result=False),
        raising=False,
    )
    monkeypatch.setattr(
        regular_supervisor_runtime,
        "_get_supervisor_idle_poll_interval_seconds",
        lambda: 0.01,
        raising=False,
    )
    monkeypatch.setattr(
        regular_supervisor_runtime,
        "_get_supervisor_idle_exit_seconds",
        lambda: 0.01,
        raising=False,
    )

    task = asyncio.create_task(
        regular_supervisor_runtime.run_supervisor_loop(
            client=object(),
            owner_token="sup-1:owner",
            supervisor_id="sup-1",
            executor=lambda *_args, **_kwargs: None,
            max_concurrency=1,
        )
    )

    await asyncio.sleep(0.05)
    assert task.done() is False
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_run_supervisor_loop_survives_transient_reconcile_error_and_keeps_serving_work(
    monkeypatch,
):
    started_attempts: list[str] = []
    reconcile_calls = {"count": 0}
    executor_started = asyncio.Event()

    async def _fake_reconcile(*_args, **_kwargs):
        reconcile_calls["count"] += 1
        if reconcile_calls["count"] == 1:
            raise RuntimeError("temporary reconcile outage")
        return []

    async def _fake_claim(*_args, **_kwargs):
        if reconcile_calls["count"] >= 2 and not started_attempts:
            return {
                "attempt_id": "a1",
                "agent_run_id": "run-1",
                "execution_epoch": 1,
                "status": "claimed",
            }
        return None

    async def _fake_mark_running(*_args, **_kwargs):
        return {
            "attempt_id": "a1",
            "agent_run_id": "run-1",
            "execution_epoch": 1,
            "status": "running",
        }

    async def _fake_refresh(*_args, **_kwargs):
        return {
            "attempt_id": _kwargs["attempt_id"],
            "status": "running",
        }

    async def _fake_executor(attempt_row, _owner_token, *, control_plane_slot=None):
        started_attempts.append(attempt_row["attempt_id"])
        assert control_plane_slot is not None
        executor_started.set()
        await asyncio.Future()

    monkeypatch.setenv("REGULAR_SUPERVISOR_PERSISTENT", "true")
    monkeypatch.setattr(
        regular_supervisor_runtime.regular_run_attempts,
        "reconcile_expired_attempts",
        _fake_reconcile,
        raising=False,
    )
    monkeypatch.setattr(
        regular_supervisor_runtime.regular_run_attempts,
        "claim_next_attempt",
        _fake_claim,
    )
    monkeypatch.setattr(
        regular_supervisor_runtime.regular_run_attempts,
        "mark_attempt_running",
        _fake_mark_running,
    )
    monkeypatch.setattr(
        regular_supervisor_runtime.regular_run_attempts,
        "refresh_attempt_heartbeat",
        _fake_refresh,
        raising=False,
    )
    monkeypatch.setattr(
        regular_supervisor_runtime.regular_run_attempts,
        "has_recoverable_attempts",
        lambda *_args, **_kwargs: asyncio.sleep(0, result=False),
        raising=False,
    )
    monkeypatch.setattr(
        regular_supervisor_runtime,
        "_get_attempt_heartbeat_interval_seconds",
        lambda: 0.01,
        raising=False,
    )
    monkeypatch.setattr(
        regular_supervisor_runtime,
        "_get_supervisor_idle_poll_interval_seconds",
        lambda: 0.01,
        raising=False,
    )

    task = asyncio.create_task(
        regular_supervisor_runtime.run_supervisor_loop(
            client=object(),
            owner_token="sup-1:owner",
            supervisor_id="sup-1",
            executor=_fake_executor,
            max_concurrency=1,
        )
    )

    await asyncio.wait_for(executor_started.wait(), timeout=0.5)
    assert started_attempts == ["a1"]
    assert reconcile_calls["count"] >= 2

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


@pytest.mark.asyncio
async def test_run_supervisor_loop_polls_recoverable_attempts_until_expiry_requeues_work(
    monkeypatch,
):
    state = {
        "reconcile_calls": 0,
        "requeued": False,
        "claimed": False,
    }
    started_attempts: list[str] = []

    async def _fake_reconcile(*_args, **_kwargs):
        state["reconcile_calls"] += 1
        if state["reconcile_calls"] >= 2 and not state["requeued"]:
            state["requeued"] = True
            return [
                {
                    "attempt_id": "a2",
                    "agent_run_id": "run-1",
                    "attempt_number": 2,
                    "execution_epoch": 2,
                    "status": "queued",
                }
            ]
        return []

    async def _fake_claim(*_args, **_kwargs):
        if state["requeued"] and not state["claimed"]:
            state["claimed"] = True
            return {
                "attempt_id": "a2",
                "agent_run_id": "run-1",
                "status": "claimed",
            }
        return None

    async def _fake_has_recoverable(*_args, **_kwargs):
        return not state["claimed"]

    async def _fake_mark_running(*_args, **_kwargs):
        return {
            "attempt_id": "a2",
            "agent_run_id": "run-1",
            "status": "running",
        }

    async def _fake_finalize(*_args, **_kwargs):
        return {
            "attempt_id": _kwargs["attempt_id"],
            "status": "completed",
        }

    async def _fake_executor(attempt_row, _owner_token):
        started_attempts.append(attempt_row["attempt_id"])

    monkeypatch.setattr(
        regular_supervisor_runtime.regular_run_attempts,
        "reconcile_expired_attempts",
        _fake_reconcile,
        raising=False,
    )
    monkeypatch.setattr(
        regular_supervisor_runtime.regular_run_attempts,
        "claim_next_attempt",
        _fake_claim,
    )
    monkeypatch.setattr(
        regular_supervisor_runtime.regular_run_attempts,
        "has_recoverable_attempts",
        _fake_has_recoverable,
        raising=False,
    )
    monkeypatch.setattr(
        regular_supervisor_runtime.regular_run_attempts,
        "mark_attempt_running",
        _fake_mark_running,
    )
    monkeypatch.setattr(
        regular_supervisor_runtime.regular_run_attempts,
        "finalize_completed_attempt",
        _fake_finalize,
        raising=False,
    )
    monkeypatch.setattr(
        regular_supervisor_runtime.regular_run_attempts,
        "refresh_attempt_heartbeat",
        lambda *_args, **_kwargs: asyncio.sleep(0, result={"status": "running"}),
        raising=False,
    )
    monkeypatch.setattr(
        regular_supervisor_runtime,
        "_get_attempt_heartbeat_interval_seconds",
        lambda: 0.01,
        raising=False,
    )
    monkeypatch.setattr(
        regular_supervisor_runtime,
        "_get_supervisor_idle_poll_interval_seconds",
        lambda: 0.01,
        raising=False,
    )
    monkeypatch.setattr(
        regular_supervisor_runtime,
        "_get_supervisor_idle_exit_seconds",
        lambda: 0.05,
        raising=False,
    )

    completed = await regular_supervisor_runtime.run_supervisor_loop(
        client=object(),
        owner_token="sup-1:slot-1",
        supervisor_id="sup-1",
        executor=_fake_executor,
        max_concurrency=1,
    )

    assert completed == ["a2"]
    assert started_attempts == ["a2"]
    assert state["reconcile_calls"] >= 2


@pytest.mark.asyncio
async def test_run_supervisor_loop_emits_claim_and_completion_metrics(monkeypatch):
    claimed_attempts = [
        {"attempt_id": "a1", "agent_run_id": "run-1", "status": "claimed"},
    ]
    metric_events: list[dict[str, object]] = []

    async def _fake_claim(*_args, **_kwargs):
        if claimed_attempts:
            return claimed_attempts.pop(0)
        return None

    async def _fake_mark_running(*_args, **_kwargs):
        return {
            "attempt_id": "a1",
            "agent_run_id": "run-1",
            "status": "running",
        }

    async def _fake_finalize(*_args, **_kwargs):
        return {
            "attempt_id": "a1",
            "status": "completed",
        }

    async def _fake_executor(*_args, **_kwargs):
        await asyncio.sleep(0)

    async def _fake_metrics_sink(snapshot: dict[str, object]) -> None:
        metric_events.append(dict(snapshot))

    monkeypatch.setattr(
        regular_supervisor_runtime.regular_run_attempts,
        "claim_next_attempt",
        _fake_claim,
    )
    monkeypatch.setattr(
        regular_supervisor_runtime.regular_run_attempts,
        "mark_attempt_running",
        _fake_mark_running,
    )
    monkeypatch.setattr(
        regular_supervisor_runtime.regular_run_attempts,
        "finalize_completed_attempt",
        _fake_finalize,
        raising=False,
    )
    monkeypatch.setattr(
        regular_supervisor_runtime.regular_run_attempts,
        "reconcile_expired_attempts",
        lambda *_args, **_kwargs: asyncio.sleep(0, result=[]),
        raising=False,
    )
    monkeypatch.setattr(
        regular_supervisor_runtime.regular_run_attempts,
        "has_recoverable_attempts",
        lambda *_args, **_kwargs: asyncio.sleep(0, result=False),
        raising=False,
    )

    completed = await regular_supervisor_runtime.run_supervisor_loop(
        client=object(),
        owner_token="sup-1:slot-1",
        supervisor_id="sup-1",
        executor=_fake_executor,
        max_concurrency=1,
        metrics_sink=_fake_metrics_sink,
    )

    assert completed == ["a1"]
    claim_events = [
        event for event in metric_events if event.get("event") == "claim"
    ]
    assert any(event.get("result") == "claimed" for event in claim_events)
    assert any(event.get("event") == "complete" for event in metric_events)
    assert max(int(event.get("peak_active_slots") or 0) for event in metric_events) == 1
