from __future__ import annotations

import importlib
import json
from types import SimpleNamespace

import pytest


def _load_module():
    try:
        return importlib.import_module("agent.regular_run_admission")
    except ModuleNotFoundError as exc:
        pytest.fail(f"regular_run_admission module is missing: {exc}")


class _FakeAgentRunTable:
    def __init__(self, client: "_FakeClient") -> None:
        self._client = client

    async def insert(self, payload):
        inserted = dict(payload)
        self._client.inserted_rows["agent_runs"].append(inserted)
        return SimpleNamespace(
            data=[
                {
                    "agent_run_id": inserted.get("agent_run_id"),
                    "id": inserted.get("agent_run_id"),
                }
            ]
        )


class _FakeSchema:
    def __init__(self, client: "_FakeClient") -> None:
        self._client = client

    def table(self, table_name: str):
        assert table_name == "agent_runs"
        return _FakeAgentRunTable(self._client)


class _FakeClient:
    def __init__(self) -> None:
        self.inserted_rows: dict[str, list[dict[str, object]]] = {"agent_runs": []}

    def schema(self, schema_name: str):
        assert schema_name == "public"
        return _FakeSchema(self)


def _read_inserted_metadata(client: _FakeClient) -> dict[str, object]:
    raw_metadata = client.inserted_rows["agent_runs"][0]["metadata"]
    if isinstance(raw_metadata, str):
        return json.loads(raw_metadata)
    assert isinstance(raw_metadata, dict)
    return dict(raw_metadata)


@pytest.mark.asyncio
async def test_admit_queued_regular_run_phase2_creates_parent_attempt_and_dispatch():
    module = _load_module()
    client = _FakeClient()
    dispatched_modes: list[str] = []
    created_attempts: list[str] = []

    async def _fake_ensure_attempt_capacity(_client):
        return None

    async def _fake_create_initial_attempt(_client, *, agent_run_id: str):
        created_attempts.append(agent_run_id)

    def _fake_dispatch(*, execution_mode: str):
        dispatched_modes.append(execution_mode)

    async def _unexpected_attempt_cleanup(*_args, **_kwargs):
        raise AssertionError("phase2 attempt cleanup should not run on the happy path")

    async def _unexpected_status_update(*_args, **_kwargs):
        raise AssertionError("status update should not run on the happy path")

    result = await module.admit_queued_regular_run(
        client=client,
        thread_id="thread-1",
        project_id="project-1",
        agent_run_id="run-1",
        agent_config_snapshot={"agent_id": "agent-1", "current_version_id": "ver-1"},
        agent_run_metadata={"model_name": "openai/gpt-4.1"},
        execution_mode="phase2_supervisor",
        ensure_phase2_attempt_queue_capacity=_fake_ensure_attempt_capacity,
        create_initial_attempt=_fake_create_initial_attempt,
        dispatch_queue_handoff=_fake_dispatch,
        mark_phase2_admission_attempt_failed=_unexpected_attempt_cleanup,
        update_agent_run_status=_unexpected_status_update,
    )

    assert result == {"agent_run_id": "run-1", "status": "queued"}
    assert client.inserted_rows["agent_runs"][0]["status"] == "queued"
    assert client.inserted_rows["agent_runs"][0]["thread_id"] == "thread-1"
    assert client.inserted_rows["agent_runs"][0]["agent_id"] == "agent-1"
    assert client.inserted_rows["agent_runs"][0]["agent_version_id"] == "ver-1"
    assert _read_inserted_metadata(client) == {
        "model_name": "openai/gpt-4.1",
        "regular_execution_mode": "phase2_supervisor",
    }
    assert created_attempts == ["run-1"]
    assert dispatched_modes == ["phase2_supervisor"]


@pytest.mark.asyncio
async def test_admit_queued_regular_run_rolls_back_when_dispatch_fails():
    module = _load_module()
    client = _FakeClient()
    cleaned_attempts: list[tuple[str, str]] = []
    updated_statuses: list[tuple[str, str, str | None]] = []

    async def _fake_ensure_attempt_capacity(_client):
        return None

    async def _fake_create_initial_attempt(_client, *, agent_run_id: str):
        assert agent_run_id == "run-1"

    def _boom_dispatch(*, execution_mode: str):
        raise RuntimeError(f"dispatch failed for {execution_mode}")

    async def _fake_mark_attempt_failed(_client, *, agent_run_id: str, error_message: str):
        cleaned_attempts.append((agent_run_id, error_message))

    async def _fake_update_status(_client, agent_run_id: str, status: str, error: str | None = None):
        updated_statuses.append((agent_run_id, status, error))

    with pytest.raises(RuntimeError, match="dispatch failed for phase2_supervisor"):
        await module.admit_queued_regular_run(
            client=client,
            thread_id="thread-1",
            project_id="project-1",
            agent_run_id="run-1",
            agent_config_snapshot=None,
            agent_run_metadata={"model_name": "openai/gpt-4.1"},
            execution_mode="phase2_supervisor",
            ensure_phase2_attempt_queue_capacity=_fake_ensure_attempt_capacity,
            create_initial_attempt=_fake_create_initial_attempt,
            dispatch_queue_handoff=_boom_dispatch,
            mark_phase2_admission_attempt_failed=_fake_mark_attempt_failed,
            update_agent_run_status=_fake_update_status,
        )

    assert cleaned_attempts == [
        ("run-1", "Failed to dispatch agent run: dispatch failed for phase2_supervisor")
    ]
    assert updated_statuses == [
        (
            "run-1",
            "failed",
            "Failed to dispatch agent run: dispatch failed for phase2_supervisor",
        )
    ]
