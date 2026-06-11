from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
import os
import time
from typing import Any, AsyncIterator, Awaitable, Callable


_TRUE_ENV_VALUES = frozenset({"1", "true", "yes", "on"})


@dataclass(frozen=True)
class RegularLoadHarnessStubConfig:
    batch_id: str
    target_tier: int
    active_duration_seconds: float
    chunk_interval_seconds: float
    emit_stub_response: bool
    artifact_path: str | None
    artifact_content: str | None


def _coerce_metadata(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        normalized_value = value.strip()
        if not normalized_value:
            return {}
        try:
            parsed_value = json.loads(normalized_value)
        except json.JSONDecodeError:
            return {}
        if isinstance(parsed_value, dict):
            return dict(parsed_value)
    return {}


def resolve_stub_config(metadata: Any) -> RegularLoadHarnessStubConfig | None:
    if (
        str(os.getenv("REGULAR_LOAD_HARNESS_STUB_MODE", "false")).strip().lower()
        not in _TRUE_ENV_VALUES
    ):
        return None

    metadata_payload = _coerce_metadata(metadata)
    harness_payload = _coerce_metadata(metadata_payload.get("regular_load_harness"))
    if not harness_payload or not bool(harness_payload.get("enabled")):
        return None

    return RegularLoadHarnessStubConfig(
        batch_id=str(harness_payload.get("batch_id") or "").strip(),
        target_tier=int(harness_payload.get("target_tier") or 0),
        active_duration_seconds=max(
            0.01,
            float(harness_payload.get("active_duration_seconds") or 0.01),
        ),
        chunk_interval_seconds=max(
            0.01,
            float(harness_payload.get("chunk_interval_seconds") or 0.25),
        ),
        emit_stub_response=bool(harness_payload.get("emit_stub_response", True)),
        artifact_path=str(harness_payload.get("artifact_path") or "").strip() or None,
        artifact_content=str(harness_payload.get("artifact_content") or "").strip()
        or None,
    )


async def iter_stub_responses(
    *,
    config: RegularLoadHarnessStubConfig,
    project_id: str,
    thread_id: str,
    agent_run_id: str,
    client: Any,
    persist_artifact: Callable[..., Awaitable[Any]],
) -> AsyncIterator[dict[str, Any]]:
    if config.artifact_path:
        await persist_artifact(
            project_id=project_id,
            path=config.artifact_path,
            content=config.artifact_content or "ok",
            source="regular_load_harness_stub",
            client=client,
            thread_id=thread_id,
            agent_run_id=agent_run_id,
            metadata={
                "batch_id": config.batch_id,
                "target_tier": config.target_tier,
            },
        )

    started_at = time.monotonic()
    chunk_index = 0
    while True:
        elapsed_seconds = time.monotonic() - started_at
        if elapsed_seconds >= config.active_duration_seconds:
            break
        await asyncio.sleep(config.chunk_interval_seconds)
        chunk_index += 1
        content = (
            f"load-harness:{config.batch_id}:{config.target_tier}:{chunk_index}"
            if config.emit_stub_response
            else ""
        )
        yield {
            "type": "assistant",
            "content": content,
        }

    yield {
        "type": "status",
        "status": "completed",
        "message": "Regular load harness stub completed",
    }
