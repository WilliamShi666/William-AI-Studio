from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

SCRIPT_PATH = (
    Path(__file__).resolve().parents[1] / "scripts" / "deepseek_capability_harness.py"
)


def _load_cli_module():
    spec = importlib.util.spec_from_file_location(
        "deepseek_capability_harness_cli_test",
        SCRIPT_PATH,
    )
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_cli_list_tasks_outputs_stable_json(capsys) -> None:
    module = _load_cli_module()

    exit_code = module.main(["list-tasks"])

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert output["schema_version"] == 1
    assert output["tasks"][0]["task_id"] == "direct_file_write_smoke"
    assert "prompt" in output["tasks"][0]
    assert output["tasks"][0]["success_criteria"][0]["path"] == (
        "/workspace/ds_smoke.txt"
    )


def test_cli_list_real_tasks_outputs_real_artifact_tasks(capsys) -> None:
    module = _load_cli_module()

    exit_code = module.main(["list-tasks", "--task-set", "real-artifacts"])

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    task_ids = [task["task_id"] for task in output["tasks"]]
    assert task_ids == ["real_manim_mp4_render", "real_pptx_deck_generation"]
    assert output["tasks"][0]["success_criteria"][1]["type"] == "file_size_at_least"


def test_cli_grade_observations_file_outputs_report(tmp_path, capsys) -> None:
    module = _load_cli_module()
    observations_path = tmp_path / "observations.json"
    observations_path.write_text(
        json.dumps(
            [
                {
                    "task_id": "direct_file_write_smoke",
                    "attempt": 1,
                    "status": "completed",
                    "duration_seconds": 11.0,
                    "artifact_verified": True,
                    "tool_calls": [
                        {
                            "name": "write_file",
                            "arguments": {
                                "path": "ds_smoke.txt",
                                "content": "ok",
                            },
                        }
                    ],
                }
            ]
        ),
        encoding="utf-8",
    )

    exit_code = module.main(
        [
            "grade-observations",
            "--observations",
            str(observations_path),
            "--model-name",
            "deepseek-v4-pro-high",
            "--generated-at",
            "2026-05-17T00:00:00Z",
        ]
    )

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert output["model_name"] == "deepseek-v4-pro-high"
    assert output["summary"]["pass@1"] == 0.2
    assert output["grades"][0]["passed"] is True


def test_cli_run_uses_api_runner_and_outputs_report(monkeypatch, capsys) -> None:
    module = _load_cli_module()
    calls = []

    class _FakeRunner:
        def __init__(self, **kwargs):
            calls.append(kwargs)

        async def run_tasks(self, *, tasks, attempts, generated_at):
            return {
                "schema_version": 1,
                "generated_at": generated_at,
                "model_name": calls[0]["model_name"],
                "summary": {
                    "task_count": len(tasks),
                    "attempt_count": attempts * len(tasks),
                    "pass@1": 1.0,
                },
                "tasks": [],
                "grades": [],
            }

    class _FakeHttpx:
        class AsyncClient:
            def __init__(self, timeout):
                self.timeout = timeout

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

    monkeypatch.setattr(module, "DeepSeekCapabilityApiRunner", _FakeRunner)
    monkeypatch.setattr(module, "httpx", _FakeHttpx)

    exit_code = module.main(
        [
            "run",
            "--base-url",
            "http://127.0.0.1:8002/api",
            "--auth-token",
            "test-token",
            "--tasks",
            "direct_file_write_smoke",
            "--attempts",
            "2",
            "--generated-at",
            "2026-05-17T00:00:00Z",
        ]
    )

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert calls[0]["base_url"] == "http://127.0.0.1:8002/api"
    assert calls[0]["auth_token"] == "test-token"
    assert output["summary"]["task_count"] == 1
    assert output["summary"]["attempt_count"] == 2


def test_cli_run_can_select_real_artifact_task_set(monkeypatch, capsys) -> None:
    module = _load_cli_module()
    calls = []

    class _FakeRunner:
        def __init__(self, **kwargs):
            calls.append(kwargs)

        async def run_tasks(self, *, tasks, attempts, generated_at):
            return {
                "schema_version": 1,
                "generated_at": generated_at,
                "model_name": calls[0]["model_name"],
                "summary": {
                    "task_count": len(tasks),
                    "attempt_count": attempts * len(tasks),
                    "pass@1": 0.0,
                },
                "tasks": [task.task_id for task in tasks],
                "grades": [],
            }

    class _FakeHttpx:
        class AsyncClient:
            def __init__(self, timeout):
                self.timeout = timeout

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

    monkeypatch.setattr(module, "DeepSeekCapabilityApiRunner", _FakeRunner)
    monkeypatch.setattr(module, "httpx", _FakeHttpx)

    exit_code = module.main(
        [
            "run",
            "--task-set",
            "real-artifacts",
            "--auth-token",
            "test-token",
            "--tasks",
            "real_manim_mp4_render",
            "--generated-at",
            "2026-05-17T00:00:00Z",
        ]
    )

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert output["summary"]["task_count"] == 1
    assert output["tasks"] == ["real_manim_mp4_render"]


def test_cli_run_requires_auth_token(monkeypatch) -> None:
    module = _load_cli_module()
    monkeypatch.delenv("DEEPSEEK_HARNESS_AUTH_TOKEN", raising=False)

    try:
        module.main(["run", "--tasks", "direct_file_write_smoke"])
    except ValueError as exc:
        assert "--auth-token" in str(exc)
    else:
        raise AssertionError("run command should require an auth token")
