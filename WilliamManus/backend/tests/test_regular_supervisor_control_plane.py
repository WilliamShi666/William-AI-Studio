from __future__ import annotations

import asyncio

import pytest

from services.regular_supervisor_control_plane import RegularSupervisorControlPlane


class _FakePubSub:
    def __init__(self, messages=None, *, idle_sleep_seconds: float = 0.005):
        self._messages = list(messages or [])
        self._idle_sleep_seconds = idle_sleep_seconds
        self.pattern_subscriptions = []
        self.unsubscribed = False
        self.closed = False

    async def psubscribe(self, *patterns):
        self.pattern_subscriptions.append(tuple(patterns))

    async def get_message(self, ignore_subscribe_messages=True, timeout=0.5):
        if self._messages:
            return self._messages.pop(0)
        await asyncio.sleep(self._idle_sleep_seconds)
        return None

    async def unsubscribe(self):
        self.unsubscribed = True

    async def aclose(self):
        self.closed = True


@pytest.mark.asyncio
async def test_control_plane_routes_stop_to_matching_local_slot():
    plane = RegularSupervisorControlPlane(supervisor_id="sup-1")
    first_slot = plane.register(
        agent_run_id="run-1",
        attempt_id="attempt-1",
        execution_epoch=1,
        owner_token="sup-1:owner-1",
    )
    second_slot = plane.register(
        agent_run_id="run-2",
        attempt_id="attempt-2",
        execution_epoch=2,
        owner_token="sup-1:owner-2",
    )

    await plane.route_control_message("run-2", "STOP")

    assert first_slot.stop_requested is False
    assert second_slot.stop_requested is True


@pytest.mark.asyncio
async def test_control_plane_batches_refresh_callbacks_for_all_slots():
    plane = RegularSupervisorControlPlane(supervisor_id="sup-1")
    refresh_calls: list[str] = []

    first_slot = plane.register(
        agent_run_id="run-1",
        attempt_id="attempt-1",
        execution_epoch=1,
        owner_token="sup-1:owner-1",
    )
    second_slot = plane.register(
        agent_run_id="run-2",
        attempt_id="attempt-2",
        execution_epoch=2,
        owner_token="sup-1:owner-2",
    )
    first_slot.set_refresh_callback(
        lambda slot: asyncio.sleep(0, result=refresh_calls.append(slot.agent_run_id))
    )
    second_slot.set_refresh_callback(
        lambda slot: asyncio.sleep(0, result=refresh_calls.append(slot.agent_run_id))
    )

    await plane.run_refresh_tick()

    assert refresh_calls == ["run-1", "run-2"]


@pytest.mark.asyncio
async def test_control_plane_listener_uses_global_pattern_and_routes_stop():
    pubsub = _FakePubSub(
        messages=[
            {
                "type": "pmessage",
                "channel": "agent_run:run-2:control",
                "data": "STOP",
            }
        ],
        idle_sleep_seconds=0.01,
    )
    plane = RegularSupervisorControlPlane(
        supervisor_id="sup-1",
        pubsub_factory=lambda: asyncio.sleep(0, result=pubsub),
    )
    target_slot = plane.register(
        agent_run_id="run-2",
        attempt_id="attempt-2",
        execution_epoch=2,
        owner_token="sup-1:owner-2",
    )

    listener_task = asyncio.create_task(plane.run_stop_listener())
    await asyncio.sleep(0.05)
    listener_task.cancel()
    await asyncio.gather(listener_task, return_exceptions=True)
    await plane.aclose()

    assert pubsub.pattern_subscriptions == [("agent_run:*:control",)]
    assert target_slot.stop_requested is True
    assert pubsub.unsubscribed is True
    assert pubsub.closed is True
