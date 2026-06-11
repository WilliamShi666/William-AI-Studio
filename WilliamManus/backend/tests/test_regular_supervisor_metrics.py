from __future__ import annotations

import importlib
import json
from types import SimpleNamespace

import pytest

import regular_supervisor_background as regular_supervisor_background_module


def _load_module():
    try:
        return importlib.import_module("services.regular_supervisor_metrics")
    except ModuleNotFoundError as exc:
        pytest.fail(f"regular_supervisor_metrics module is missing: {exc}")


class _FakeRedis:
    def __init__(self) -> None:
        self._values: dict[str, str] = {}

    async def set(self, key: str, value: str, ex: int | None = None):
        assert ex is None or ex > 0
        self._values[key] = value
        return True

    async def get(self, key: str):
        return self._values.get(key)

    async def keys(self, pattern: str):
        prefix = pattern[:-1] if pattern.endswith("*") else pattern
        return [key for key in self._values if key.startswith(prefix)]


class _AwaitableValue:
    def __init__(self, value: object):
        self._value = value

    def __await__(self):
        async def _resolve():
            return self._value

        return _resolve().__await__()


@pytest.mark.asyncio
async def test_build_redis_metrics_sink_persists_latest_snapshot():
    module = _load_module()
    redis_client = _FakeRedis()
    sink = module.build_redis_metrics_sink(
        redis_client=redis_client,
        supervisor_id="regular-supervisor-1",
        runtime_token="worktree-token",
    )

    await sink(
        {
            "event": "reconcile",
            "claim_success_count": 3,
            "claim_empty_count": 2,
            "reconcile_pass_count": 4,
        }
    )

    snapshots = await module.load_metrics_snapshots(
        redis_client=redis_client,
        runtime_token="worktree-token",
    )

    assert snapshots["regular-supervisor-1"]["event"] == "reconcile"
    assert snapshots["regular-supervisor-1"]["claim_success_count"] == 3
    assert snapshots["regular-supervisor-1"]["reconcile_pass_count"] == 4
    assert snapshots["regular-supervisor-1"]["runtime_token"] == "worktree-token"


@pytest.mark.asyncio
async def test_regular_supervisor_background_passes_metrics_sink_when_enabled(monkeypatch):
    module = _load_module()
    fake_client = object()
    captured: dict[str, object] = {}

    async def _noop_async():
        return None

    async def _fake_run_supervisor_loop(**kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setenv("REGULAR_LOAD_HARNESS_METRICS_ENABLED", "true")
    monkeypatch.setattr(regular_supervisor_background_module, "initialize", _noop_async)
    monkeypatch.setattr(
        regular_supervisor_background_module,
        "db",
        SimpleNamespace(client=_AwaitableValue(fake_client)),
    )
    monkeypatch.setattr(
        regular_supervisor_background_module.regular_supervisor_runtime,
        "run_supervisor_loop",
        _fake_run_supervisor_loop,
    )
    monkeypatch.setattr(
        regular_supervisor_background_module,
        "build_supervisor_id",
        lambda: "regular-supervisor-1",
        raising=False,
    )
    monkeypatch.setattr(module, "get_runtime_token", lambda: "worktree-token")

    await regular_supervisor_background_module.regular_supervisor_background.fn(
        supervisor_id="regular-supervisor-1"
    )

    assert captured["client"] is fake_client
    assert captured["supervisor_id"] == "regular-supervisor-1"
    assert callable(captured["metrics_sink"])
