from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import types

import pytest


def _import_run_status_projection_with_temporary_stubs():
    original_services_redis = sys.modules.get("services.redis")
    installed_services_redis_stub = False

    if original_services_redis is None:
        services_redis_stub = types.ModuleType("services.redis")

        async def _default_lrange(*_args, **_kwargs):
            return []

        services_redis_stub.lrange = _default_lrange
        sys.modules["services.redis"] = services_redis_stub
        installed_services_redis_stub = True

    try:
        module_name = "test_agent_run_status_projection_module"
        sys.modules.pop(module_name, None)
        module_path = (
            Path(__file__).resolve().parents[1]
            / "agent"
            / "run_status_projection.py"
        )
        spec = importlib.util.spec_from_file_location(module_name, module_path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Unable to load module spec for {module_path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        if installed_services_redis_stub:
            sys.modules.pop("services.redis", None)
        if original_services_redis is not None:
            sys.modules["services.redis"] = original_services_redis


run_status_projection = _import_run_status_projection_with_temporary_stubs()


@pytest.mark.asyncio
async def test_read_terminal_status_from_response_tail_returns_latest_terminal(monkeypatch):
    async def _fake_lrange(_key: str, _start: int, _end: int):
        return [
            '{"type":"assistant","content":"partial"}',
            '{"type":"status","status":"failed","message":"old failure"}',
            '{"type":"status","status":"completed","message":"done"}',
        ]

    monkeypatch.setattr(run_status_projection.redis, "lrange", _fake_lrange)

    status, message = await run_status_projection.read_terminal_status_from_response_tail(
        "run-123",
    )

    assert status == "completed"
    assert message == "done"


@pytest.mark.asyncio
async def test_read_terminal_status_from_response_tail_ignores_nonterminal_status(monkeypatch):
    async def _fake_lrange(_key: str, _start: int, _end: int):
        return [
            '{"type":"status","status":"running","message":"still working"}',
            '{"type":"assistant","content":"partial"}',
        ]

    monkeypatch.setattr(run_status_projection.redis, "lrange", _fake_lrange)

    status, message = await run_status_projection.read_terminal_status_from_response_tail(
        "run-456",
    )

    assert status is None
    assert message is None


@pytest.mark.asyncio
async def test_read_terminal_status_from_response_tail_normalizes_error_to_failed(monkeypatch):
    async def _fake_lrange(_key: str, _start: int, _end: int):
        return [
            '{"type":"status","status":"error","message":"boom"}',
        ]

    monkeypatch.setattr(run_status_projection.redis, "lrange", _fake_lrange)

    status, message = await run_status_projection.read_terminal_status_from_response_tail(
        "run-error",
    )

    assert status == "failed"
    assert message == "boom"


@pytest.mark.asyncio
async def test_read_terminal_status_from_response_tail_returns_none_on_fetch_error(monkeypatch):
    async def _broken_lrange(_key: str, _start: int, _end: int):
        raise RuntimeError("redis unavailable")

    monkeypatch.setattr(run_status_projection.redis, "lrange", _broken_lrange)

    status, message = await run_status_projection.read_terminal_status_from_response_tail(
        "run-fetch-error",
    )

    assert status is None
    assert message is None


@pytest.mark.asyncio
async def test_attempt_epoch_tail_prefers_current_execution_epoch(monkeypatch):
    seen_keys: list[str] = []

    async def _fake_lrange(key: str, _start: int, _end: int):
        seen_keys.append(key)
        if key == "agent_run:run-epoch:epoch:2:responses":
            return [
                '{"type":"status","status":"completed","message":"epoch done"}',
            ]
        return [
            '{"type":"status","status":"failed","message":"legacy failed"}',
        ]

    monkeypatch.setattr(run_status_projection.redis, "lrange", _fake_lrange)

    status, message = await run_status_projection.read_terminal_status_from_current_attempt_tail(
        "run-epoch",
        current_execution_epoch=2,
    )

    assert status == "completed"
    assert message == "epoch done"
    assert seen_keys[0] == "agent_run:run-epoch:epoch:2:responses"


@pytest.mark.asyncio
async def test_attempt_epoch_tail_falls_back_to_legacy_response_tail(monkeypatch):
    seen_keys: list[str] = []

    async def _fake_lrange(key: str, _start: int, _end: int):
        seen_keys.append(key)
        if key == "agent_run:run-epoch-fallback:epoch:3:responses":
            return []
        return [
            '{"type":"status","status":"stopped","message":"legacy stop"}',
        ]

    monkeypatch.setattr(run_status_projection.redis, "lrange", _fake_lrange)

    status, message = await run_status_projection.read_terminal_status_from_current_attempt_tail(
        "run-epoch-fallback",
        current_execution_epoch=3,
    )

    assert status == "stopped"
    assert message == "legacy stop"
    assert seen_keys == [
        "agent_run:run-epoch-fallback:epoch:3:responses",
        "agent_run:run-epoch-fallback:responses",
    ]


@pytest.mark.asyncio
async def test_attempt_epoch_tail_can_disable_legacy_response_tail_fallback(monkeypatch):
    seen_keys: list[str] = []

    async def _fake_lrange(key: str, _start: int, _end: int):
        seen_keys.append(key)
        if key == "agent_run:run-epoch-strict:epoch:4:responses":
            return []
        return [
            '{"type":"status","status":"failed","message":"legacy failed"}',
        ]

    monkeypatch.setattr(run_status_projection.redis, "lrange", _fake_lrange)

    status, message = await run_status_projection.read_terminal_status_from_response_tail(
        "run-epoch-strict",
        current_execution_epoch=4,
        include_legacy_run_wide_fallback=False,
    )

    assert status is None
    assert message is None
    assert seen_keys == [
        "agent_run:run-epoch-strict:epoch:4:responses",
    ]


def test_read_terminal_status_from_attempt_tails_prefers_current_attempt_epoch():
    status, message = run_status_projection.read_terminal_status_from_attempt_tails(
        current_execution_epoch=2,
        tails_by_epoch={
            1: [
                {"type": "status", "status": "failed", "message": "old failure"},
            ],
            2: [
                {"type": "status", "status": "completed", "message": "done"},
            ],
        },
    )

    assert status == "completed"
    assert message == "done"
