import asyncio
from unittest.mock import AsyncMock

import pytest

from agentscope_integration.shadow_clone.confirmation_hook import (
    ConfirmationDecisionWriteStatus,
    get_confirmation_result,
    record_confirmation_result,
    wait_for_confirmation,
)
from agentscope_integration.shadow_clone.constants import ConfirmationResult


class _FakePubSub:
    def __init__(self, messages=None, error_once=False):
        self.messages = list(messages or [])
        self.error_once = error_once
        self.subscribed = []
        self.closed = False
        self.unsubscribed = []

    async def subscribe(self, *channels):
        self.subscribed.append(channels)

    async def unsubscribe(self, *channels):
        self.unsubscribed.append(channels)

    async def close(self):
        self.closed = True

    async def get_message(self, ignore_subscribe_messages=True, timeout=1.0):
        if self.error_once:
            self.error_once = False
            raise ConnectionError("temporary")
        if not self.messages:
            await asyncio.sleep(0)
            return None
        return self.messages.pop(0)


@pytest.mark.asyncio
async def test_wait_for_confirmation_confirmed(monkeypatch):
    fake_pubsub = _FakePubSub(
        messages=[{"type": "message", "data": "CONFIRMED"}],
    )

    async def _create_pubsub():
        return fake_pubsub

    monkeypatch.setattr(
        "agentscope_integration.shadow_clone.confirmation_hook.redis_service.create_pubsub",
        _create_pubsub,
    )
    result = await wait_for_confirmation("run-1", timeout=1)
    assert result == ConfirmationResult.CONFIRMED
    assert fake_pubsub.closed is True


@pytest.mark.asyncio
async def test_wait_for_confirmation_denied(monkeypatch):
    fake_pubsub = _FakePubSub(
        messages=[{"type": "message", "data": "DENIED"}],
    )

    async def _create_pubsub():
        return fake_pubsub

    monkeypatch.setattr(
        "agentscope_integration.shadow_clone.confirmation_hook.redis_service.create_pubsub",
        _create_pubsub,
    )
    result = await wait_for_confirmation("run-1", timeout=1)
    assert result == ConfirmationResult.DENIED


@pytest.mark.asyncio
async def test_wait_for_confirmation_stop_cancelled(monkeypatch):
    fake_pubsub = _FakePubSub(
        messages=[{"type": "message", "data": "STOP"}],
    )

    async def _create_pubsub():
        return fake_pubsub

    monkeypatch.setattr(
        "agentscope_integration.shadow_clone.confirmation_hook.redis_service.create_pubsub",
        _create_pubsub,
    )
    result = await wait_for_confirmation("run-1", timeout=1)
    assert result == ConfirmationResult.CANCELLED


@pytest.mark.asyncio
async def test_wait_for_confirmation_timeout(monkeypatch):
    fake_pubsub = _FakePubSub(messages=[])

    async def _create_pubsub():
        return fake_pubsub

    monkeypatch.setattr(
        "agentscope_integration.shadow_clone.confirmation_hook.redis_service.create_pubsub",
        _create_pubsub,
    )
    result = await wait_for_confirmation("run-1", timeout=0)
    assert result == ConfirmationResult.TIMEOUT


@pytest.mark.asyncio
async def test_wait_for_confirmation_without_timeout_waits_for_message(monkeypatch):
    fake_pubsub = _FakePubSub(
        messages=[
            None,
            {"type": "message", "data": "CONFIRMED"},
        ],
    )

    async def _create_pubsub():
        return fake_pubsub

    monkeypatch.setattr(
        "agentscope_integration.shadow_clone.confirmation_hook.redis_service.create_pubsub",
        _create_pubsub,
    )
    result = await asyncio.wait_for(
        wait_for_confirmation("run-1", timeout=None),
        timeout=1,
    )
    assert result == ConfirmationResult.CONFIRMED


@pytest.mark.asyncio
async def test_wait_for_confirmation_recovers_connection_error(monkeypatch):
    fake_pubsub = _FakePubSub(
        messages=[{"type": "message", "data": "CONFIRMED"}],
        error_once=True,
    )

    async def _create_pubsub():
        return fake_pubsub

    monkeypatch.setattr(
        "agentscope_integration.shadow_clone.confirmation_hook.redis_service.create_pubsub",
        _create_pubsub,
    )
    result = await wait_for_confirmation("run-1", timeout=1)
    assert result == ConfirmationResult.CONFIRMED
    # initial subscribe + re-subscribe
    assert len(fake_pubsub.subscribed) >= 2


@pytest.mark.asyncio
async def test_wait_for_confirmation_uses_durable_result_before_pubsub(monkeypatch):
    create_pubsub = AsyncMock(side_effect=AssertionError("pubsub should not be opened"))

    monkeypatch.setattr(
        "agentscope_integration.shadow_clone.confirmation_hook.redis_service.get",
        AsyncMock(return_value="confirmed"),
    )
    monkeypatch.setattr(
        "agentscope_integration.shadow_clone.confirmation_hook.redis_service.create_pubsub",
        create_pubsub,
    )

    result = await wait_for_confirmation("run-durable", timeout=1)

    assert result == ConfirmationResult.CONFIRMED
    create_pubsub.assert_not_awaited()


@pytest.mark.asyncio
async def test_record_confirmation_result_is_idempotent_and_detects_conflict(monkeypatch):
    eval_script = AsyncMock(side_effect=[1, 2, -1])
    get = AsyncMock(return_value="denied")

    monkeypatch.setattr(
        "agentscope_integration.shadow_clone.confirmation_hook.redis_service.eval_script",
        eval_script,
    )
    monkeypatch.setattr(
        "agentscope_integration.shadow_clone.confirmation_hook.redis_service.get",
        get,
    )

    first_status, first_result = await record_confirmation_result(
        "run-write",
        ConfirmationResult.CONFIRMED,
    )
    second_status, second_result = await record_confirmation_result(
        "run-write",
        ConfirmationResult.CONFIRMED,
    )
    third_status, third_result = await record_confirmation_result(
        "run-write",
        ConfirmationResult.CONFIRMED,
    )

    assert first_status == ConfirmationDecisionWriteStatus.RECORDED
    assert first_result == ConfirmationResult.CONFIRMED
    assert second_status == ConfirmationDecisionWriteStatus.DUPLICATE
    assert second_result == ConfirmationResult.CONFIRMED
    assert third_status == ConfirmationDecisionWriteStatus.CONFLICT
    assert third_result == ConfirmationResult.DENIED
    assert eval_script.await_count == 3
    get.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_confirmation_result_normalizes_known_values(monkeypatch):
    monkeypatch.setattr(
        "agentscope_integration.shadow_clone.confirmation_hook.redis_service.get",
        AsyncMock(side_effect=["CONFIRMED", "denied", "unexpected", None]),
    )

    assert await get_confirmation_result("run-1") == ConfirmationResult.CONFIRMED
    assert await get_confirmation_result("run-2") == ConfirmationResult.DENIED
    assert await get_confirmation_result("run-3") is None
    assert await get_confirmation_result("run-4") is None
