from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import regular_supervisor_background as regular_supervisor_background_module


class _FakeTableQuery:
    def __init__(self, rows: list[dict[str, object]]):
        self._rows = rows
        self._filters: list[tuple[str, object]] = []

    def select(self, *_args, **_kwargs):
        return self

    def eq(self, field: str, value: object):
        self._filters.append((field, value))
        return self

    async def execute(self):
        matched_rows = [
            dict(row)
            for row in self._rows
            if all(row.get(field) == value for field, value in self._filters)
        ]
        return SimpleNamespace(data=matched_rows)


class _FakeClient:
    def __init__(
        self,
        *,
        agent_run_rows: list[dict[str, object]],
        thread_rows: list[dict[str, object]],
    ):
        self._tables = {
            "agent_runs": agent_run_rows,
            "threads": thread_rows,
        }

    def table(self, table_name: str):
        assert table_name in self._tables
        return _FakeTableQuery(self._tables[table_name])


class _AwaitableValue:
    def __init__(self, value: object):
        self._value = value

    def __await__(self):
        async def _resolve():
            return self._value

        return _resolve().__await__()


def test_get_regular_supervisor_actor_time_limit_ms_defaults_to_positive_value(
    monkeypatch,
):
    monkeypatch.setenv("REGULAR_SUPERVISOR_PERSISTENT", "false")
    monkeypatch.delenv("REGULAR_SUPERVISOR_TIME_LIMIT_MS", raising=False)

    assert (
        regular_supervisor_background_module.get_regular_supervisor_actor_time_limit_ms()
        == 86_400_000
    )


def test_get_regular_supervisor_actor_time_limit_ms_is_infinite_in_persistent_mode(
    monkeypatch,
):
    monkeypatch.setenv("REGULAR_SUPERVISOR_PERSISTENT", "true")
    monkeypatch.delenv("REGULAR_SUPERVISOR_TIME_LIMIT_MS", raising=False)

    assert (
        regular_supervisor_background_module.get_regular_supervisor_actor_time_limit_ms()
        == float("inf")
    )


def test_get_regular_supervisor_actor_max_retries_is_unbounded_in_persistent_mode(
    monkeypatch,
):
    monkeypatch.setenv("REGULAR_SUPERVISOR_PERSISTENT", "true")
    monkeypatch.delenv("REGULAR_SUPERVISOR_MAX_RETRIES", raising=False)

    assert (
        regular_supervisor_background_module.get_regular_supervisor_actor_max_retries()
        is None
    )


def test_regular_supervisor_background_actor_has_valid_time_limit_option():
    time_limit = regular_supervisor_background_module.regular_supervisor_background.options[
        "time_limit"
    ]
    assert time_limit == float("inf") or int(time_limit) > 0


def test_bootstrap_middleware_enqueues_bootstrap_supervisor_actor(monkeypatch):
    send_calls: list[dict[str, object]] = []

    monkeypatch.setattr(
        regular_supervisor_background_module.regular_supervisor_background,
        "send",
        lambda **kwargs: send_calls.append(dict(kwargs)),
        raising=False,
    )

    middleware = regular_supervisor_background_module._RegularSupervisorBootstrapMiddleware()
    middleware.after_worker_boot(broker=None, worker=object())

    assert len(send_calls) == 1
    assert send_calls[0]["supervisor_id"] == regular_supervisor_background_module.build_supervisor_id()


def test_build_supervisor_id_is_stable_per_process(monkeypatch):
    monkeypatch.setenv("REGULAR_SUPERVISOR_SHARD_PREFIX", "wm-phase3")
    monkeypatch.setattr(regular_supervisor_background_module.os, "getpid", lambda: 4242)

    assert regular_supervisor_background_module.build_supervisor_id() == "wm-phase3:4242"


def test_resolve_supervisor_id_maps_persistent_wakeup_to_local_process_id(monkeypatch):
    monkeypatch.setenv("REGULAR_SUPERVISOR_SHARD_PREFIX", "wm-phase3")
    monkeypatch.setattr(regular_supervisor_background_module.os, "getpid", lambda: 4242)

    assert (
        regular_supervisor_background_module.resolve_supervisor_id(
            regular_supervisor_background_module.PERSISTENT_SUPERVISOR_WAKEUP_SUPERVISOR_ID
        )
        == "wm-phase3:4242"
    )


def test_bootstrap_middleware_exposes_iterable_forks_contract():
    middleware = regular_supervisor_background_module._RegularSupervisorBootstrapMiddleware()

    assert middleware.actor_options == set()
    assert middleware.forks == []


@pytest.mark.asyncio
async def test_execute_claimed_attempt_invokes_attempt_aware_kernel(monkeypatch):
    kernel_calls: list[dict[str, object]] = []
    fake_client = _FakeClient(
        agent_run_rows=[
            {
                "agent_run_id": "run-1",
                "thread_id": "thread-1",
                "metadata": json.dumps(
                    {
                        "model_name": "gpt-test",
                        "enable_thinking": True,
                        "reasoning_effort": "high",
                        "enable_context_manager": False,
                        "agent_config_snapshot": {
                            "agent_id": "agent-1",
                            "name": "Regular Pool Agent",
                        },
                        "request_id": "req-1",
                        "resume_strategy": "manual",
                        "resume_window_minutes": 30,
                    }
                ),
            }
        ],
        thread_rows=[
            {
                "thread_id": "thread-1",
                "project_id": "project-1",
            }
        ],
    )

    async def _fake_kernel(**kwargs):
        kernel_calls.append(dict(kwargs))
        return "run-1"

    monkeypatch.setattr(
        regular_supervisor_background_module,
        "db",
        SimpleNamespace(client=_AwaitableValue(fake_client)),
    )
    monkeypatch.setattr(
        regular_supervisor_background_module,
        "_load_regular_run_kernel",
        lambda: (_fake_kernel, "phase2-instance"),
        raising=False,
    )

    await regular_supervisor_background_module._execute_claimed_attempt(
        {
            "attempt_id": "attempt-1",
            "agent_run_id": "run-1",
            "execution_epoch": 2,
        },
        "owner-1",
    )

    assert kernel_calls == [
        {
            "client": fake_client,
            "agent_run_id": "run-1",
            "thread_id": "thread-1",
            "instance_id": "phase2-instance",
            "project_id": "project-1",
            "model_name": "gpt-test",
            "enable_thinking": True,
            "reasoning_effort": "high",
            "stream": True,
            "enable_context_manager": False,
            "agent_config": {
                "agent_id": "agent-1",
                "name": "Regular Pool Agent",
            },
            "is_agent_builder": False,
            "target_agent_id": None,
            "request_id": "req-1",
            "resume_strategy": "manual",
            "resume_window_minutes": 30,
            "shadow_clone_mode": "off",
            "shadow_clone_main_model": None,
            "shadow_clone_subagent_model": None,
            "owner_token": "owner-1",
            "run_capacity_mode": "off",
            "attempt_id": "attempt-1",
            "execution_epoch": 2,
            "run_metadata": {
                "model_name": "gpt-test",
                "enable_thinking": True,
                "reasoning_effort": "high",
                "enable_context_manager": False,
                "agent_config_snapshot": {
                    "agent_id": "agent-1",
                    "name": "Regular Pool Agent",
                },
                "request_id": "req-1",
                "resume_strategy": "manual",
                "resume_window_minutes": 30,
            },
        }
    ]


@pytest.mark.asyncio
async def test_execute_claimed_attempt_preserves_shadow_clone_v2_metadata(
    monkeypatch,
):
    kernel_calls: list[dict[str, object]] = []
    fake_client = _FakeClient(
        agent_run_rows=[
            {
                "agent_run_id": "run-v2",
                "thread_id": "thread-v2",
                "metadata": json.dumps(
                    {
                        "model_name": "qwen-main",
                        "shadow_clone_mode": "auto",
                        "shadow_clone_runtime": "v2",
                        "shadow_clone_main_model": "qwen-main",
                        "shadow_clone_subagent_model": "qwen-worker",
                        "shadow_clone_v2_execution_chain": "regular_supervisor",
                        "regular_execution_mode": "phase2_supervisor",
                        "request_id": "req-v2",
                    }
                ),
            }
        ],
        thread_rows=[
            {
                "thread_id": "thread-v2",
                "project_id": "project-v2",
            }
        ],
    )

    async def _fake_kernel(**kwargs):
        kernel_calls.append(dict(kwargs))
        return "run-v2"

    monkeypatch.setattr(
        regular_supervisor_background_module,
        "db",
        SimpleNamespace(client=_AwaitableValue(fake_client)),
    )
    monkeypatch.setattr(
        regular_supervisor_background_module,
        "_load_regular_run_kernel",
        lambda: (_fake_kernel, "phase2-instance"),
        raising=False,
    )

    await regular_supervisor_background_module._execute_claimed_attempt(
        {
            "attempt_id": "attempt-v2",
            "agent_run_id": "run-v2",
            "execution_epoch": 3,
        },
        "owner-v2",
    )

    assert len(kernel_calls) == 1
    kernel_kwargs = kernel_calls[0]
    assert kernel_kwargs["shadow_clone_mode"] == "auto"
    assert kernel_kwargs["shadow_clone_main_model"] == "qwen-main"
    assert kernel_kwargs["shadow_clone_subagent_model"] == "qwen-worker"
    assert kernel_kwargs["run_capacity_mode"] == "auto"
    assert kernel_kwargs["run_metadata"] == {
        "model_name": "qwen-main",
        "shadow_clone_mode": "auto",
        "shadow_clone_runtime": "v2",
        "shadow_clone_main_model": "qwen-main",
        "shadow_clone_subagent_model": "qwen-worker",
        "shadow_clone_v2_execution_chain": "regular_supervisor",
        "regular_execution_mode": "phase2_supervisor",
        "request_id": "req-v2",
    }
