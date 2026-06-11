from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from eval.deepseek_capability_api_runner import (
    DeepSeekCapabilityApiRunner,
    build_langfuse_trace_url,
    extract_tool_calls_from_messages,
)
from eval.deepseek_capability_harness import CapabilityTask, default_capability_tasks
from eval.outcome_verifier import (
    CommandExecutionResult,
    OutcomeVerificationRequest,
    verify_outcome,
)


class _FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200) -> None:
        self._payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}: {self.text}")


class _FakeAsyncClient:
    def __init__(
        self,
        *,
        file_payloads: dict[str, bytes] | None = None,
        terminal_status: str = "completed",
    ) -> None:
        self.posts: list[tuple[str, dict | None, dict | None]] = []
        self.gets: list[str] = []
        self._poll_count = 0
        self._file_payloads = dict(file_payloads or {})
        self._terminal_status = str(terminal_status)

    async def post(self, url: str, *, headers=None, data=None, json=None):
        self.posts.append((url, data, json))
        return _FakeResponse(
            {
                "thread_id": "thread-1",
                "agent_run_id": "run-1",
            }
        )

    async def get(self, url: str, *, headers=None):
        self.gets.append(url)
        if url.endswith("/agent-runs"):
            self._poll_count += 1
            status = self._terminal_status if self._poll_count >= 2 else "running"
            return _FakeResponse(
                {
                    "agent_runs": [
                        {
                            "agent_run_id": "run-1",
                            "status": status,
                            "completed_at": "2026-05-17T00:00:21+00:00",
                            "metadata": {
                                "langfuse_trace_id": "0123456789abcdef0123456789abcdef",
                            },
                        }
                    ]
                }
            )
        if url.endswith("/threads/thread-1"):
            return _FakeResponse(
                {
                    "thread_id": "thread-1",
                    "project_id": "project-1",
                    "project": {
                        "project_id": "project-1",
                        "sandbox": {"id": "sandbox-1"},
                    },
                }
            )
        if url.endswith("/messages?order=asc"):
            return _FakeResponse(
                {
                    "messages": [
                        {
                            "type": "assistant",
                            "content": json_module_dumps(
                                {
                                    "role": "assistant",
                                    "content": "",
                                    "tool_calls": [
                                        {
                                            "type": "function",
                                            "function": {
                                                "name": "write_file",
                                                "arguments": json_module_dumps(
                                                    {
                                                        "path": "ds_smoke.txt",
                                                        "content": "ok",
                                                    }
                                                ),
                                            },
                                        }
                                    ],
                                }
                            ),
                        }
                    ]
                }
            )
        if "/sandboxes/sandbox-1/files/content?path=" in url:
            path = url.rsplit("path=", 1)[-1]
            if path in self._file_payloads:
                return _BytesResponse(content=self._file_payloads[path])
            return _BytesResponse(
                content=b'{"detail":"File not found"}',
                payload={"detail": "File not found"},
                status_code=404,
            )
        if "/sandboxes/sandbox-1/files?path=" in url:
            return _FakeResponse({"files": []})
        raise AssertionError(f"Unexpected GET {url}")


class _BytesResponse:
    def __init__(
        self,
        *,
        content: bytes,
        payload: dict | None = None,
        status_code: int = 200,
    ) -> None:
        self.content = content
        self._payload = dict(payload or {})
        self.status_code = status_code
        self.text = content.decode("utf-8", errors="replace")

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}: {self.text}")


@dataclass(frozen=True)
class _CommandKey:
    argv: tuple[str, ...]
    cwd: str | None


class _FakeVerifierIO:
    def __init__(
        self,
        *,
        files: dict[str, bytes] | None = None,
        directories: set[str] | None = None,
        commands: dict[_CommandKey, CommandExecutionResult] | None = None,
    ) -> None:
        self.files = dict(files or {})
        self.directories = set(directories or set())
        self.commands = dict(commands or {})

    async def read_bytes(self, path: str) -> bytes | None:
        return self.files.get(path)

    async def list_dir(self, path: str) -> list[dict]:
        normalized = path.rstrip("/") or "/"
        if normalized not in self.directories:
            raise FileNotFoundError(path)
        return []

    async def run_command(
        self,
        argv: list[str],
        *,
        cwd: str | None = None,
        timeout_seconds: int | None = None,
    ) -> CommandExecutionResult:
        del timeout_seconds
        key = _CommandKey(tuple(argv), cwd)
        if key not in self.commands:
            raise AssertionError(f"Unexpected command: argv={argv!r}, cwd={cwd!r}")
        return self.commands[key]


def json_module_dumps(payload: dict) -> str:
    return json.dumps(payload)




def test_build_langfuse_trace_url_handles_common_hosts() -> None:
    assert (
        build_langfuse_trace_url(
            host="http://localhost:3000",
            trace_id="0123456789abcdef0123456789abcdef",
        )
        == "http://localhost:3000/project/default/traces/0123456789abcdef0123456789abcdef"
    )
    assert (
        build_langfuse_trace_url(
            host="https://cloud.langfuse.com/",
            trace_id="abc",
        )
        == "https://cloud.langfuse.com/project/default/traces/abc"
    )
    assert build_langfuse_trace_url(host="", trace_id="abc") is None
    assert build_langfuse_trace_url(host="http://localhost:3000", trace_id="") is None


def test_extract_tool_calls_from_messages_parses_serialized_assistant_tool_calls() -> (
    None
):
    messages = [
        {
            "type": "assistant",
            "content": json.dumps(
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "type": "function",
                            "function": {
                                "name": "edit_file",
                                "arguments": '{"path":"target.py","old_text":"a","new_text":"b"}',
                            },
                        }
                    ],
                }
            ),
        }
    ]

    tool_calls = extract_tool_calls_from_messages(messages)

    assert len(tool_calls) == 1
    assert tool_calls[0].name == "edit_file"
    assert tool_calls[0].arguments["path"] == "target.py"


@pytest.mark.asyncio
async def test_api_runner_initiates_polls_and_returns_observation_without_sleep() -> (
    None
):
    client = _FakeAsyncClient(
        file_payloads={
            "%2Fworkspace%2Fds_smoke.txt": b"DEEPSEEK_ROYS_ALPHA_SMOKE_OK",
        }
    )
    task = default_capability_tasks()[0]
    runner = DeepSeekCapabilityApiRunner(
        client=client,
        base_url="http://example.test/api",
        auth_token="token",
        model_name="deepseek-v4-pro-high",
        poll_interval_seconds=0,
        max_poll_attempts=3,
    )

    observation = await runner.run_task(task=task, attempt=1)

    assert observation.task_id == task.task_id
    assert observation.attempt == 1
    assert observation.status == "completed"
    assert observation.artifact_verified is True
    assert observation.tool_calls[0].name == "write_file"
    assert client.posts[0][0] == "http://example.test/api/agent/initiate"
    assert client.posts[0][1]["model_name"] == "deepseek-v4-pro-high"
    assert client.posts[0][1]["shadow_clone_mode"] == "off"
    assert "http://example.test/api/threads/thread-1" in client.gets
    assert (
        "http://example.test/api/sandboxes/sandbox-1/files/content?"
        "path=%2Fworkspace%2Fds_smoke.txt"
    ) in client.gets


@pytest.mark.asyncio
async def test_api_runner_marks_completed_run_unverified_when_artifact_missing() -> (
    None
):
    client = _FakeAsyncClient()
    task = default_capability_tasks()[0]
    runner = DeepSeekCapabilityApiRunner(
        client=client,
        base_url="http://example.test/api",
        auth_token="token",
        model_name="deepseek-v4-pro-high",
        poll_interval_seconds=0,
        max_poll_attempts=3,
    )

    observation = await runner.run_task(task=task, attempt=1)

    assert observation.status == "completed"
    assert observation.artifact_verified is False
    assert observation.error is not None
    assert "artifact verification failed" in observation.error


@pytest.mark.asyncio
async def test_api_runner_verifies_artifact_even_when_run_stopped() -> None:
    """A stopped run can still have produced the requested artifact.

    Live model runs may continue past success and later be stopped by a signal
    or cleanup path.  The eval should still inspect the sandbox so a generated
    artifact is not reported as "verification skipped" purely because the final
    run status is stopped.
    """
    client = _FakeAsyncClient(
        terminal_status="stopped",
        file_payloads={
            "%2Fworkspace%2Fds_smoke.txt": b"DEEPSEEK_ROYS_ALPHA_SMOKE_OK",
        },
    )
    task = default_capability_tasks()[0]
    runner = DeepSeekCapabilityApiRunner(
        client=client,
        base_url="http://example.test/api",
        auth_token="token",
        model_name="deepseek-v4-pro-high",
        poll_interval_seconds=0,
        max_poll_attempts=3,
    )

    observation = await runner.run_task(task=task, attempt=1)

    assert observation.status == "stopped"
    assert observation.artifact_verified is True
    assert observation.error is None
    assert observation.metadata["agent_run_id"] == "run-1"
    assert observation.metadata["thread_id"] == "thread-1"
    assert observation.metadata["langfuse_trace_id"] == "0123456789abcdef0123456789abcdef"
    assert observation.metadata["langfuse_trace_url"].endswith(
        "/project/default/traces/0123456789abcdef0123456789abcdef"
    )
    assert (
        "http://example.test/api/sandboxes/sandbox-1/files/content?"
        "path=%2Fworkspace%2Fds_smoke.txt"
    ) in client.gets


@pytest.mark.asyncio
async def test_api_runner_supports_injected_outcome_verifier_io() -> None:
    client = _FakeAsyncClient()
    task = default_capability_tasks()[0]
    seen_requests: list[OutcomeVerificationRequest] = []

    def build_io(request: OutcomeVerificationRequest) -> _FakeVerifierIO:
        seen_requests.append(request)
        return _FakeVerifierIO(
            files={
                "/workspace/ds_smoke.txt": b"DEEPSEEK_ROYS_ALPHA_SMOKE_OK",
            }
        )

    runner = DeepSeekCapabilityApiRunner(
        client=client,
        base_url="http://example.test/api",
        auth_token="token",
        model_name="deepseek-v4-pro-high",
        poll_interval_seconds=0,
        max_poll_attempts=3,
        verifier_io_factory=build_io,
        outcome_verifier=verify_outcome,
    )

    observation = await runner.run_task(task=task, attempt=1)

    assert observation.artifact_verified is True
    assert seen_requests[0].task_id == "direct_file_write_smoke"
    assert seen_requests[0].project_id == "project-1"
    assert seen_requests[0].thread_id == "thread-1"
    assert seen_requests[0].agent_run_id == "run-1"
    assert seen_requests[0].sandbox_id == "sandbox-1"


@pytest.mark.asyncio
async def test_api_runner_builds_report_for_multiple_tasks() -> None:
    client = _FakeAsyncClient(
        file_payloads={
            "%2Fworkspace%2Fds_smoke.txt": b"DEEPSEEK_ROYS_ALPHA_SMOKE_OK",
        }
    )
    tasks = default_capability_tasks()[:1]
    runner = DeepSeekCapabilityApiRunner(
        client=client,
        base_url="http://example.test/api",
        auth_token="token",
        model_name="deepseek-v4-pro-high",
        poll_interval_seconds=0,
        max_poll_attempts=3,
    )

    report = await runner.run_tasks(
        tasks=tasks,
        attempts=2,
        generated_at="2026-05-17T00:00:00Z",
    )

    assert report["schema_version"] == 1
    assert report["summary"]["task_count"] == 1
    assert report["summary"]["attempt_count"] == 2
    assert report["summary"]["pass@1"] == 1.0
    assert report["grades"][0]["passed"] is True


@pytest.mark.asyncio
async def test_api_runner_uses_task_duration_budget_for_poll_attempts() -> None:
    client = _FakeAsyncClient()
    task = CapabilityTask(
        task_id="long-real-artifact",
        category="real_artifact",
        prompt="Create a real artifact.",
        expected_tools=("write_file",),
        max_duration_seconds=905,
    )
    runner = DeepSeekCapabilityApiRunner(
        client=client,
        base_url="http://example.test/api",
        auth_token="token",
        model_name="deepseek-v4-pro-high",
        poll_interval_seconds=10,
        max_poll_attempts=3,
    )

    observation = await runner.run_task(task=task, attempt=1)

    assert observation.status == "completed"
    agent_run_poll_count = sum(url.endswith("/agent-runs") for url in client.gets)
    assert agent_run_poll_count == 2
    assert runner._max_poll_attempts_for_task(task) == 91




def test_start_payload_uses_expected_langfuse_trace_id() -> None:
    task = CapabilityTask(
        task_id="real-artifact",
        category="real_artifact",
        prompt="Create a real artifact.",
        expected_tools=("write_file",),
        max_duration_seconds=1200,
    )
    runner = DeepSeekCapabilityApiRunner(
        client=_FakeAsyncClient(),
        base_url="http://example.test/api",
        auth_token="token",
        model_name="deepseek-v4-pro-high",
        poll_interval_seconds=0,
        max_poll_attempts=3,
    )

    payload = runner._build_start_payload(task, attempt=2)

    assert payload["eval_task_id"] == "real-artifact"
    assert payload["eval_attempt_index"] == "2"
    assert int(payload["eval_max_generated_responses"]) == 200_000
    assert payload["expected_langfuse_trace_id"] == ""

    payload = runner._build_start_payload(
        task,
        attempt=2,
        agent_run_id="11111111-1111-1111-1111-111111111111",
    )

    assert payload["agent_run_id"] == "11111111-1111-1111-1111-111111111111"
    assert (
        payload["expected_langfuse_trace_id"]
        == "38c6cbd28bf165070d070980dd1fb595"
    )


@pytest.mark.asyncio
async def test_api_runner_sends_eval_metadata_for_real_artifact_tasks() -> None:
    client = _FakeAsyncClient()
    task = CapabilityTask(
        task_id="real-artifact",
        category="real_artifact",
        prompt="Create a real artifact.",
        expected_tools=("write_file",),
        max_duration_seconds=1200,
    )
    runner = DeepSeekCapabilityApiRunner(
        client=client,
        base_url="http://example.test/api",
        auth_token="token",
        model_name="deepseek-v4-pro-high",
        poll_interval_seconds=0,
        max_poll_attempts=3,
    )

    await runner.run_task(task=task, attempt=2)

    initiate_payload = client.posts[0][1]
    assert initiate_payload["eval_task_id"] == "real-artifact"
    assert initiate_payload["eval_attempt_index"] == "2"
    assert int(initiate_payload["eval_max_generated_responses"]) == 200_000
    assert len(initiate_payload["expected_langfuse_trace_id"]) == 32
    assert initiate_payload["agent_run_id"]
