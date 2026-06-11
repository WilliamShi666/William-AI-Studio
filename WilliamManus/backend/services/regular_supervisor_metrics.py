from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from typing import Any, Awaitable, Callable


_TRUE_ENV_VALUES = frozenset({"1", "true", "yes", "on"})


def metrics_enabled() -> bool:
    return (
        str(os.getenv("REGULAR_LOAD_HARNESS_METRICS_ENABLED", "false"))
        .strip()
        .lower()
        in _TRUE_ENV_VALUES
    )


def get_runtime_token() -> str:
    runtime_token = str(os.getenv("WORKTREE_RUNTIME_TOKEN") or "").strip()
    return runtime_token or "default"


def _build_metrics_key(*, runtime_token: str, supervisor_id: str) -> str:
    return f"regular_supervisor_metrics:{runtime_token}:{supervisor_id}"


def build_redis_metrics_sink(
    *,
    redis_client: Any,
    supervisor_id: str,
    runtime_token: str,
) -> Callable[[dict[str, Any]], Awaitable[None]]:
    metrics_key = _build_metrics_key(
        runtime_token=runtime_token,
        supervisor_id=supervisor_id,
    )

    async def _sink(snapshot: dict[str, Any]) -> None:
        payload = dict(snapshot)
        payload["supervisor_id"] = supervisor_id
        payload["runtime_token"] = runtime_token
        payload["published_at"] = datetime.now(timezone.utc).isoformat()
        await redis_client.set(metrics_key, json.dumps(payload), ex=600)

    return _sink


async def load_metrics_snapshots(
    *,
    redis_client: Any,
    runtime_token: str,
) -> dict[str, dict[str, Any]]:
    snapshots: dict[str, dict[str, Any]] = {}
    keys = await redis_client.keys(
        f"regular_supervisor_metrics:{runtime_token}:*"
    )
    for key in keys:
        raw_payload = await redis_client.get(key)
        if not raw_payload:
            continue
        if isinstance(raw_payload, bytes):
            raw_payload = raw_payload.decode("utf-8")
        payload = json.loads(raw_payload)
        snapshots[str(payload["supervisor_id"])] = payload
    return snapshots
