from __future__ import annotations

import asyncio
import hashlib
import os
from datetime import datetime, timezone
import json
import time
import uuid
from typing import Any, Awaitable, Callable, Protocol
from urllib.parse import quote

from eval.deepseek_capability_harness import (
    CapabilityTask,
    RunObservation,
    ToolCallRecord,
    build_capability_report,
)
from eval.outcome_verifier import (
    CommandExecutionResult,
    OutcomeVerificationRequest,
    OutcomeVerificationResult,
    OutcomeVerifierIO,
    verify_outcome,
)

_TERMINAL_STATUSES = {"completed", "failed", "error", "stopped"}


class AsyncApiClient(Protocol):
    async def post(
        self, url: str, *, headers: dict[str, str] | None = None, data=None, json=None
    ): ...

    async def get(self, url: str, *, headers: dict[str, str] | None = None): ...


OutcomeVerifier = Callable[
    [OutcomeVerificationRequest, OutcomeVerifierIO],
    Awaitable[OutcomeVerificationResult],
]
VerifierIOFactory = Callable[[OutcomeVerificationRequest], OutcomeVerifierIO]


class ApiOutcomeVerifierIO:
    def __init__(
        self,
        *,
        client: AsyncApiClient,
        base_url: str,
        headers: dict[str, str],
        sandbox_id: str | None,
    ) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._headers = dict(headers)
        self._sandbox_id = str(sandbox_id or "").strip()

    async def read_bytes(self, path: str) -> bytes | None:
        if not self._sandbox_id:
            return None
        response = await self._client.get(
            (
                f"{self._base_url}/sandboxes/{quote(self._sandbox_id, safe='')}"
                f"/files/content?path={quote(str(path), safe='')}"
            ),
            headers=self._headers,
        )
        if int(getattr(response, "status_code", 200)) == 404:
            return None
        response.raise_for_status()
        content = getattr(response, "content", None)
        if isinstance(content, (bytes, bytearray)):
            return bytes(content)
        text = getattr(response, "text", "")
        return str(text).encode("utf-8")

    async def list_dir(self, path: str) -> list[dict[str, Any]]:
        if not self._sandbox_id:
            raise FileNotFoundError(path)
        response = await self._client.get(
            (
                f"{self._base_url}/sandboxes/{quote(self._sandbox_id, safe='')}"
                f"/files?path={quote(str(path), safe='')}"
            ),
            headers=self._headers,
        )
        if int(getattr(response, "status_code", 200)) == 404:
            raise FileNotFoundError(path)
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict):
            files = payload.get("files")
            return files if isinstance(files, list) else []
        return payload if isinstance(payload, list) else []

    async def run_command(
        self,
        argv: list[str],
        *,
        cwd: str | None = None,
        timeout_seconds: int | None = None,
    ) -> CommandExecutionResult:
        del argv, cwd, timeout_seconds
        return CommandExecutionResult(
            exit_code=127,
            stderr="command verification is not supported by the API verifier IO",
        )




def build_langfuse_trace_url(*, host: str | None, trace_id: str | None) -> str | None:
    normalized_host = str(host or "").strip().rstrip("/")
    normalized_trace_id = str(trace_id or "").strip()
    if not normalized_host or not normalized_trace_id:
        return None
    return f"{normalized_host}/project/default/traces/{quote(normalized_trace_id, safe='')}"


def _extract_agent_run_metadata(final_run: dict[str, Any]) -> dict[str, Any]:
    run_metadata = final_run.get("metadata")
    if not isinstance(run_metadata, dict):
        run_metadata = {}
    trace_id = str(
        final_run.get("langfuse_trace_id")
        or final_run.get("trace_id")
        or run_metadata.get("langfuse_trace_id")
        or run_metadata.get("trace_id")
        or ""
    ).strip()
    metadata: dict[str, Any] = {}
    if trace_id:
        metadata["langfuse_trace_id"] = trace_id
        trace_url = build_langfuse_trace_url(
            host=os.getenv("LANGFUSE_HOST"),
            trace_id=trace_id,
        )
        if trace_url:
            metadata["langfuse_trace_url"] = trace_url
    return metadata

def _loads_json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return {}
    stripped = value.strip()
    if not stripped:
        return {}
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _json_or_dict(value: Any) -> dict[str, Any]:
    return _loads_json_object(value)


def _extract_project_id(thread_payload: dict[str, Any]) -> str:
    project_payload = thread_payload.get("project")
    if not isinstance(project_payload, dict):
        project_payload = {}
    return str(
        thread_payload.get("project_id") or project_payload.get("project_id") or ""
    ).strip()


def _extract_sandbox_id(
    *,
    thread_payload: dict[str, Any],
    final_run: dict[str, Any],
) -> str | None:
    file_delivery_source = final_run.get("file_delivery_source")
    if isinstance(file_delivery_source, dict):
        sandbox_id = str(
            file_delivery_source.get("archive_sandbox_id")
            or file_delivery_source.get("browse_sandbox_id")
            or ""
        ).strip()
        if sandbox_id:
            return sandbox_id

    project_payload = thread_payload.get("project")
    if not isinstance(project_payload, dict):
        project_payload = {}
    sandbox_payload = _json_or_dict(project_payload.get("sandbox"))
    sandbox_id = str(sandbox_payload.get("id") or "").strip()
    return sandbox_id or None


def _criteria_to_list(task: CapabilityTask) -> list[dict[str, Any]]:
    return [dict(criterion) for criterion in task.success_criteria]


def _merge_error(existing: Any, new_error: str | None) -> str | None:
    existing_text = str(existing or "").strip()
    new_text = str(new_error or "").strip()
    if existing_text and new_text:
        return f"{existing_text}; {new_text}"
    return existing_text or new_text or None


def _extract_function_arguments(payload: dict[str, Any]) -> dict[str, Any]:
    function_payload = payload.get("function")
    if not isinstance(function_payload, dict):
        return {}
    return _loads_json_object(function_payload.get("arguments"))


def extract_tool_calls_from_messages(
    messages: list[dict[str, Any]],
) -> tuple[ToolCallRecord, ...]:
    records: list[ToolCallRecord] = []
    for message in messages:
        if str(message.get("type") or message.get("role") or "") != "assistant":
            continue
        content_payload = _loads_json_object(message.get("content"))
        tool_calls = content_payload.get("tool_calls")
        if not isinstance(tool_calls, list):
            continue
        for tool_call in tool_calls:
            if not isinstance(tool_call, dict):
                continue
            function_payload = tool_call.get("function")
            if not isinstance(function_payload, dict):
                continue
            tool_name = str(function_payload.get("name") or "").strip()
            if not tool_name:
                continue
            records.append(
                ToolCallRecord(
                    name=tool_name,
                    arguments=_extract_function_arguments(tool_call),
                )
            )
    return tuple(records)


class DeepSeekCapabilityApiRunner:
    def __init__(
        self,
        *,
        client: AsyncApiClient,
        base_url: str,
        auth_token: str,
        model_name: str,
        poll_interval_seconds: float = 5.0,
        max_poll_attempts: int = 72,
        verifier_io_factory: VerifierIOFactory | None = None,
        outcome_verifier: OutcomeVerifier = verify_outcome,
    ) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._headers = {"Authorization": f"Bearer {auth_token}"}
        self._model_name = str(model_name)
        self._poll_interval_seconds = max(0.0, float(poll_interval_seconds))
        self._max_poll_attempts = max(1, int(max_poll_attempts))
        self._verifier_io_factory = verifier_io_factory
        self._outcome_verifier = outcome_verifier

    async def run_task(self, *, task: CapabilityTask, attempt: int) -> RunObservation:
        started_at = time.monotonic()
        try:
            start_payload = await self._start_agent_run(task, attempt=attempt)
            thread_id = str(start_payload["thread_id"])
            agent_run_id = str(start_payload.get("agent_run_id") or start_payload.get("id") or "")
            final_run = await self._poll_agent_run(
                thread_id=thread_id,
                agent_run_id=agent_run_id,
                max_poll_attempts=self._max_poll_attempts_for_task(task),
            )
            messages = await self._fetch_messages(thread_id)
            artifact_verified, verification_error = await self._verify_task_artifacts(
                task=task,
                thread_id=thread_id,
                agent_run_id=agent_run_id,
                final_run=final_run,
            )
            agent_run_metadata = _extract_agent_run_metadata(final_run)
            if "langfuse_trace_id" not in agent_run_metadata:
                agent_run_metadata = {
                    **agent_run_metadata,
                    "langfuse_trace_id": hashlib.md5(
                        agent_run_id.encode("utf-8"),
                        usedforsecurity=False,
                    ).hexdigest(),
                }
            if "langfuse_trace_url" not in agent_run_metadata:
                fallback_trace_url = build_langfuse_trace_url(
                    host=os.getenv("LANGFUSE_HOST"),
                    trace_id=str(agent_run_metadata.get("langfuse_trace_id") or ""),
                )
                if fallback_trace_url:
                    agent_run_metadata = {
                        **agent_run_metadata,
                        "langfuse_trace_url": fallback_trace_url,
                    }
            return RunObservation(
                task_id=task.task_id,
                attempt=attempt,
                status=str(final_run.get("status") or ""),
                duration_seconds=round(time.monotonic() - started_at, 6),
                artifact_verified=artifact_verified,
                tool_calls=extract_tool_calls_from_messages(messages),
                error=_merge_error(final_run.get("error"), verification_error),
                metadata={
                    "thread_id": thread_id,
                    "agent_run_id": agent_run_id,
                    **agent_run_metadata,
                },
            )
        except Exception as exc:
            return RunObservation(
                task_id=task.task_id,
                attempt=attempt,
                status="error",
                duration_seconds=round(time.monotonic() - started_at, 6),
                artifact_verified=(
                    False if task.requires_artifact_verification else None
                ),
                tool_calls=(),
                error=str(exc),
            )

    async def _verify_task_artifacts(
        self,
        *,
        task: CapabilityTask,
        thread_id: str,
        agent_run_id: str,
        final_run: dict[str, Any],
    ) -> tuple[bool | None, str | None]:
        if not task.requires_artifact_verification:
            return None, None

        criteria = _criteria_to_list(task)
        if not criteria:
            return False, "artifact verification failed: task has no success criteria"

        try:
            thread_payload = await self._fetch_thread(thread_id)
            project_id = _extract_project_id(thread_payload)
            sandbox_id = _extract_sandbox_id(
                thread_payload=thread_payload,
                final_run=final_run,
            )
            request = OutcomeVerificationRequest(
                task_id=task.task_id,
                project_id=project_id,
                success_criteria=criteria,
                thread_id=thread_id,
                agent_run_id=agent_run_id,
                sandbox_id=sandbox_id,
                artifact_root="/workspace",
            )
            io_adapter = (
                self._verifier_io_factory(request)
                if self._verifier_io_factory is not None
                else ApiOutcomeVerifierIO(
                    client=self._client,
                    base_url=self._base_url,
                    headers=self._headers,
                    sandbox_id=sandbox_id,
                )
            )
            result = await self._outcome_verifier(request, io_adapter)
        except Exception as exc:
            return False, f"artifact verification failed: {exc}"

        if result.passed:
            return True, None
        detail = "; ".join(result.errors) or result.summary
        return False, f"artifact verification failed: {result.summary}; {detail}"

    async def run_tasks(
        self,
        *,
        tasks: tuple[CapabilityTask, ...],
        attempts: int = 1,
        generated_at: str | None = None,
    ) -> dict[str, Any]:
        observations: list[RunObservation] = []
        for task in tasks:
            for attempt in range(1, max(1, int(attempts)) + 1):
                observations.append(await self.run_task(task=task, attempt=attempt))
        return build_capability_report(
            tasks=tasks,
            observations=tuple(observations),
            model_name=self._model_name,
            generated_at=generated_at or datetime.now(timezone.utc).isoformat(),
        )


    def _build_start_payload(
        self,
        task: CapabilityTask,
        *,
        attempt: int,
        agent_run_id: str | None = None,
    ) -> dict[str, str]:
        data = {
            "prompt": task.prompt,
            "model_name": self._model_name,
            "enable_thinking": "true",
            "reasoning_effort": "high",
            "stream": "false",
            "shadow_clone_mode": "off",
        }
        if task.category != "real_artifact":
            return data

        normalized_agent_run_id = str(agent_run_id or "").strip()
        eval_payload = {
            **data,
            "eval_task_id": task.task_id,
            "eval_attempt_index": str(attempt),
            "eval_max_generated_responses": "200000",
            "expected_langfuse_trace_id": (
                hashlib.md5(
                    normalized_agent_run_id.encode("utf-8"),
                    usedforsecurity=False,
                ).hexdigest()
                if normalized_agent_run_id
                else ""
            ),
        }
        if normalized_agent_run_id:
            eval_payload["agent_run_id"] = normalized_agent_run_id
        return eval_payload

    async def _start_agent_run(
        self,
        task: CapabilityTask,
        *,
        attempt: int,
    ) -> dict[str, Any]:
        agent_run_id = (
            str(uuid.uuid4()) if task.category == "real_artifact" else None
        )
        data = self._build_start_payload(
            task,
            attempt=attempt,
            agent_run_id=agent_run_id,
        )

        response = await self._client.post(
            f"{self._base_url}/agent/initiate",
            headers=self._headers,
            data=data,
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("agent initiate response must be a JSON object")
        return payload

    async def _poll_agent_run(
        self,
        *,
        thread_id: str,
        agent_run_id: str,
        max_poll_attempts: int | None = None,
    ) -> dict[str, Any]:
        for _ in range(max(1, int(max_poll_attempts or self._max_poll_attempts))):
            response = await self._client.get(
                f"{self._base_url}/thread/{thread_id}/agent-runs",
                headers=self._headers,
            )
            response.raise_for_status()
            runs = response.json().get("agent_runs", [])
            if isinstance(runs, list):
                for run in runs:
                    if not isinstance(run, dict):
                        continue
                    run_id = str(run.get("agent_run_id") or run.get("id") or "")
                    if run_id != agent_run_id:
                        continue
                    if str(run.get("status") or "") in _TERMINAL_STATUSES:
                        return run
            if self._poll_interval_seconds:
                await asyncio.sleep(self._poll_interval_seconds)
        raise TimeoutError(f"agent run {agent_run_id} did not finish")

    def _max_poll_attempts_for_task(self, task: CapabilityTask) -> int:
        duration_seconds = task.max_duration_seconds
        if duration_seconds is None or duration_seconds <= 0:
            return self._max_poll_attempts
        interval = max(self._poll_interval_seconds, 1.0)
        task_budget_attempts = int((float(duration_seconds) + interval - 1) // interval)
        return max(self._max_poll_attempts, task_budget_attempts)

    async def _fetch_messages(self, thread_id: str) -> list[dict[str, Any]]:
        response = await self._client.get(
            f"{self._base_url}/threads/{thread_id}/messages?order=asc",
            headers=self._headers,
        )
        response.raise_for_status()
        messages = response.json().get("messages", [])
        return messages if isinstance(messages, list) else []

    async def _fetch_thread(self, thread_id: str) -> dict[str, Any]:
        response = await self._client.get(
            f"{self._base_url}/threads/{thread_id}",
            headers=self._headers,
        )
        response.raise_for_status()
        payload = response.json()
        return payload if isinstance(payload, dict) else {}
