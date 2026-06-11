from __future__ import annotations

import importlib

import pytest


def _load_module():
    try:
        return importlib.import_module("services.regular_load_harness_stub")
    except ModuleNotFoundError as exc:
        pytest.fail(f"regular_load_harness_stub module is missing: {exc}")


def test_resolve_stub_config_requires_runtime_flag_and_metadata(monkeypatch):
    module = _load_module()
    metadata = {
        "regular_load_harness": {
            "enabled": True,
            "batch_id": "batch-1",
            "target_tier": 25,
            "active_duration_seconds": 2.0,
            "chunk_interval_seconds": 0.25,
            "emit_stub_response": True,
        }
    }

    monkeypatch.delenv("REGULAR_LOAD_HARNESS_STUB_MODE", raising=False)
    assert module.resolve_stub_config(metadata) is None

    monkeypatch.setenv("REGULAR_LOAD_HARNESS_STUB_MODE", "true")
    assert module.resolve_stub_config({}) is None

    resolved = module.resolve_stub_config(metadata)
    assert resolved is not None
    assert resolved.batch_id == "batch-1"
    assert resolved.target_tier == 25
    assert resolved.active_duration_seconds == 2.0


@pytest.mark.asyncio
async def test_iter_stub_responses_persists_artifact_and_finishes_completed():
    module = _load_module()
    persisted_artifacts: list[tuple[str, str, str]] = []

    async def _fake_persist_artifact(**kwargs):
        persisted_artifacts.append(
            (
                kwargs["agent_run_id"],
                kwargs["project_id"],
                kwargs["path"],
            )
        )
        return object()

    config = module.RegularLoadHarnessStubConfig(
        batch_id="batch-1",
        target_tier=25,
        active_duration_seconds=0.03,
        chunk_interval_seconds=0.01,
        emit_stub_response=True,
        artifact_path="/workspace/load-harness/result.txt",
        artifact_content="ok",
    )

    responses = []
    async for response in module.iter_stub_responses(
        config=config,
        project_id="project-1",
        thread_id="thread-1",
        agent_run_id="run-1",
        client=object(),
        persist_artifact=_fake_persist_artifact,
    ):
        responses.append(response)

    assert persisted_artifacts == [
        ("run-1", "project-1", "/workspace/load-harness/result.txt")
    ]
    assert responses[-1]["type"] == "status"
    assert responses[-1]["status"] == "completed"
