from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import inspect
import json
from math import ceil
import os
from pathlib import Path
import signal
import sys
import time
import uuid
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND_PATH = Path(__file__).resolve().parents[1] / "backend"
_RUNTIME_META_PATH_ENV = "REGULAR_LOAD_HARNESS_RUNTIME_META_PATH"
_RUNTIME_META_ENV_FIELDS: tuple[tuple[str, str], ...] = (
    ("model_to_use", "MODEL_TO_USE"),
    ("runtime_token", "WORKTREE_RUNTIME_TOKEN"),
    ("dramatiq_run_agent_queue", "DRAMATIQ_RUN_AGENT_QUEUE"),
    ("dramatiq_sandbox_cleanup_queue", "DRAMATIQ_SANDBOX_CLEANUP_QUEUE"),
    ("shadow_clone_subagent_queue", "SHADOW_CLONE_SUBAGENT_QUEUE"),
    ("regular_supervisor_queue", "REGULAR_SUPERVISOR_QUEUE"),
    ("agentscope_server_concurrency_budget", "AGENTSCOPE_SERVER_CONCURRENCY_BUDGET"),
    (
        "agentscope_server_regular_admission_budget",
        "AGENTSCOPE_SERVER_REGULAR_ADMISSION_BUDGET",
    ),
    ("regular_queue_max_depth", "REGULAR_QUEUE_MAX_DEPTH"),
    ("regular_supervisor_queue_max_depth", "REGULAR_SUPERVISOR_QUEUE_MAX_DEPTH"),
    (
        "sandbox_shared_max_concurrent_global",
        "SANDBOX_SHARED_MAX_CONCURRENT_GLOBAL",
    ),
    (
        "sandbox_shared_max_concurrent_per_sandbox",
        "SANDBOX_SHARED_MAX_CONCURRENT_PER_SANDBOX",
    ),
)


def _resolve_runtime_meta_path() -> Path:
    explicit_path = str(os.getenv(_RUNTIME_META_PATH_ENV) or "").strip()
    if explicit_path:
        return Path(explicit_path).expanduser()
    return REPO_ROOT / ".worktree-runtime" / "backend.meta"


def _read_runtime_meta(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}

    payload: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if "=" not in raw_line:
            continue
        key, value = raw_line.split("=", 1)
        normalized_key = key.strip()
        if not normalized_key:
            continue
        payload[normalized_key] = value.strip()

    worktree_root = str(payload.get("worktree_root") or "").strip()
    if worktree_root and Path(worktree_root).expanduser().resolve() != REPO_ROOT:
        return {}
    return payload


def _bootstrap_worktree_runtime_environment(
    runtime_meta: dict[str, str],
) -> dict[str, str]:
    if not runtime_meta:
        return {}

    applied: dict[str, str] = {}
    resolved_redis_db = str(runtime_meta.get("redis_db") or "").strip() or "15"
    if not str(os.getenv("REDIS_DB") or "").strip():
        os.environ["REDIS_DB"] = resolved_redis_db
        applied["REDIS_DB"] = resolved_redis_db

    for meta_field, env_name in _RUNTIME_META_ENV_FIELDS:
        expected_value = str(runtime_meta.get(meta_field) or "").strip()
        if not expected_value:
            continue
        if str(os.getenv(env_name) or "").strip():
            continue
        os.environ[env_name] = expected_value
        applied[env_name] = expected_value

    return applied


_WORKTREE_RUNTIME_META = _read_runtime_meta(_resolve_runtime_meta_path())
_BOOTSTRAPPED_RUNTIME_ENV = _bootstrap_worktree_runtime_environment(
    _WORKTREE_RUNTIME_META
)

if str(BACKEND_PATH) not in sys.path:
    sys.path.insert(0, str(BACKEND_PATH))

from agent import api as agent_api
from agent import regular_run_admission
from services import redis as redis_service
from services import regular_supervisor_metrics
from services.postgresql import DBConnection
from sandbox.sandbox import delete_sandbox
from utils.config import config
from utils.logger import logger

db = DBConnection()
agent_api.initialize(db, _instance_id="phase3_regular_backend_load_harness")
_TERMINAL_STATUSES = {"completed", "failed", "stopped"}
_DEFAULT_VALIDATION_PROFILE = "stop_sampled"
_DEFAULT_EXECUTION_PROFILE = "stub_backend"
_DEFAULT_SANDBOX_DISTRIBUTION_PROFILE = "shared_project_single_sandbox"
_DEFAULT_REGULAR_MODEL = "openrouter/minimax/minimax-m2.5"
_LEGACY_QWEN_DEFAULT_MODEL = "dashscope/qwen3.5-397b-a17b"


def validate_worktree_runtime_binding() -> None:
    if not _WORKTREE_RUNTIME_META:
        return

    mismatches: list[str] = []
    expected_redis_db = (
        str(_WORKTREE_RUNTIME_META.get("redis_db") or "").strip() or "15"
    )
    current_redis_db = str(os.getenv("REDIS_DB") or "").strip()
    if current_redis_db != expected_redis_db:
        mismatches.append(
            f"REDIS_DB={current_redis_db or '<unset>'} expected {expected_redis_db}"
        )

    for meta_field, env_name in _RUNTIME_META_ENV_FIELDS:
        expected_value = str(_WORKTREE_RUNTIME_META.get(meta_field) or "").strip()
        if not expected_value:
            continue
        current_value = str(os.getenv(env_name) or "").strip()
        if current_value != expected_value:
            mismatches.append(
                f"{env_name}={current_value or '<unset>'} expected {expected_value}"
            )

    if mismatches:
        raise RuntimeError(
            "Harness runtime environment does not match worktree runtime meta: "
            + "; ".join(mismatches)
        )


def _p95(values: list[float]) -> float | None:
    if not values:
        return None
    sorted_values = sorted(values)
    index = max(0, ceil(len(sorted_values) * 0.95) - 1)
    return round(sorted_values[index], 3)


async def _resolve_maybe_awaitable(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


async def _get_client():
    await redis_service.initialize_async()
    return await db.client


async def resolve_account_id(*, client: Any, explicit_account_id: str | None) -> str:
    if explicit_account_id:
        return explicit_account_id

    basejump_error: Exception | None = None
    try:
        result = (
            await client.schema("basejump")
            .table("accounts")
            .select("id")
            .limit(1)
            .execute()
        )
        rows = list(getattr(result, "data", None) or [])
        if rows:
            return str(rows[0]["id"])
    except Exception as exc:
        basejump_error = exc
        if "basejump.accounts" not in str(exc):
            raise

    for table_name in ("projects", "threads"):
        result = await client.table(table_name).select("account_id").limit(1).execute()
        rows = list(getattr(result, "data", None) or [])
        if rows and rows[0].get("account_id"):
            return str(rows[0]["account_id"])

    if basejump_error is not None:
        raise RuntimeError(
            "No account_id is available for the load harness; "
            "basejump.accounts lookup failed and no public project/thread account_id was found"
        ) from basejump_error
    raise RuntimeError("No account_id is available for the load harness")


def build_harness_metadata(
    *,
    batch_id: str,
    target_tier: int,
    active_duration_seconds: float,
    emit_stub_response: bool,
    artifact_path: str | None = None,
    execution_profile: str = _DEFAULT_EXECUTION_PROFILE,
    validation_profile: str = _DEFAULT_VALIDATION_PROFILE,
    sandbox_distribution_profile: str = _DEFAULT_SANDBOX_DISTRIBUTION_PROFILE,
    user_message_override: str | None = None,
) -> dict[str, Any]:
    payload = {
        "model_name": _resolve_harness_model_name(),
        "regular_execution_mode": "phase2_supervisor",
        "regular_load_harness": {
            "enabled": True,
            "batch_id": batch_id,
            "target_tier": target_tier,
            "execution_profile": execution_profile,
            "validation_profile": validation_profile,
            "sandbox_distribution_profile": sandbox_distribution_profile,
            "user_message_override": user_message_override,
            "active_duration_seconds": active_duration_seconds,
            "chunk_interval_seconds": 0.25,
            "emit_stub_response": emit_stub_response,
            "artifact_path": artifact_path,
            "artifact_content": "ok" if artifact_path else None,
        },
    }
    return payload


def resolve_validation_profile(validation_profile: str) -> dict[str, Any]:
    normalized_profile = str(validation_profile or "").strip().lower()
    if not normalized_profile:
        normalized_profile = _DEFAULT_VALIDATION_PROFILE
    if normalized_profile == "baseline_no_stop":
        return {
            "validation_profile": "baseline_no_stop",
            "queued_stop_count": 0,
            "running_stop_count": 0,
            "disturb_one_shard": False,
        }
    if normalized_profile == "disturbance_recovery":
        return {
            "validation_profile": "disturbance_recovery",
            "queued_stop_count": 2,
            "running_stop_count": 3,
            "disturb_one_shard": True,
        }
    if normalized_profile == _DEFAULT_VALIDATION_PROFILE:
        return {
            "validation_profile": _DEFAULT_VALIDATION_PROFILE,
            "queued_stop_count": 2,
            "running_stop_count": 3,
            "disturb_one_shard": False,
        }
    raise ValueError(f"Unknown validation_profile: {validation_profile!r}")


def _resolve_harness_model_name() -> str:
    explicit_model_name = str(
        os.getenv("REGULAR_LOAD_HARNESS_MODEL_NAME") or ""
    ).strip()
    if explicit_model_name:
        return explicit_model_name
    configured_model_name = str(config.MODEL_TO_USE or "").strip()
    if (
        configured_model_name
        and configured_model_name != _LEGACY_QWEN_DEFAULT_MODEL
    ):
        return configured_model_name
    return _DEFAULT_REGULAR_MODEL


def _default_user_message_override(
    *,
    execution_profile: str,
    artifact_path: str | None,
) -> str | None:
    normalized_profile = str(execution_profile or "").strip().lower()
    if normalized_profile == "real_provider_text":
        return "Reply with the exact text LOAD-HARNESS-OK and nothing else."
    if normalized_profile == "real_sandbox_write_once" and artifact_path:
        return (
            "You must do exactly these steps:\n"
            f"1. Use the sandbox to create a file at this exact path: `{artifact_path}`\n"
            "2. Write exactly this text into the file: `ok`\n"
            "3. Reply with exactly `DONE`\n"
            "Do not change the path. Do not rename the file. Do not add extra text."
        )
    return None


def build_artifact_path(*, batch_id: str, run_index: int) -> str:
    del batch_id
    return f"/workspace/load-harness/artifact-{int(run_index) + 1}.txt"


async def _insert_thread(
    client: Any,
    *,
    account_id: str,
    project_id: str,
) -> str:
    thread_id = str(uuid.uuid4())
    result = (
        await client.schema("public")
        .table("threads")
        .insert(
            {
                "thread_id": thread_id,
                "account_id": account_id,
                "project_id": project_id,
                "created_at": datetime.now(),
            }
        )
    )
    if not getattr(result, "data", None):
        raise RuntimeError(f"Failed to create thread for project {project_id}")
    return thread_id


def _resolve_sandbox_distribution_profile(sandbox_distribution_profile: str) -> str:
    normalized_profile = str(sandbox_distribution_profile or "").strip().lower()
    if not normalized_profile:
        return _DEFAULT_SANDBOX_DISTRIBUTION_PROFILE
    if normalized_profile == "per_run_project_multi_sandbox":
        return "per_run_project_multi_sandbox"
    if normalized_profile == "shared_project_single_sandbox":
        return "shared_project_single_sandbox"
    raise ValueError(
        "Unknown sandbox_distribution_profile: " f"{sandbox_distribution_profile!r}"
    )


def _resolve_effective_artifact_run_count(
    *,
    target_active_runs: int,
    artifact_run_count: int | None,
    execution_profile: str,
) -> int:
    if artifact_run_count is not None:
        return max(0, int(artifact_run_count))
    normalized_profile = str(execution_profile or "").strip().lower()
    if normalized_profile == "real_sandbox_write_once":
        return max(1, int(target_active_runs))
    if normalized_profile == "real_provider_text":
        return 0
    return 2


def _resolve_admission_wave_size(target_active_runs: int) -> int:
    normalized_target = max(1, int(target_active_runs))
    if normalized_target <= 16:
        return 4
    if normalized_target <= 25:
        return 5
    if normalized_target <= 32:
        return 8
    if normalized_target <= 50:
        return 10
    return max(10, min(16, normalized_target // 5))


def _resolve_admission_wave_delay_seconds() -> float:
    return 0.25


def _fixture_projects(fixtures: dict[str, Any]) -> list[dict[str, str]]:
    explicit_projects = [
        {
            "project_id": str(project.get("project_id")),
            "project_name": str(project.get("project_name") or ""),
        }
        for project in list(fixtures.get("projects") or [])
        if str(project.get("project_id") or "").strip()
    ]
    if explicit_projects:
        return explicit_projects

    fallback_project_id = str(fixtures.get("project_id") or "").strip()
    if fallback_project_id:
        return [
            {
                "project_id": fallback_project_id,
                "project_name": str(fixtures.get("project_name") or ""),
            }
        ]
    return []


async def _create_project_fixture(
    client: Any,
    *,
    batch_id: str,
    account_id: str,
    run_index: int | None = None,
) -> dict[str, str]:
    project_id = str(uuid.uuid4())
    project_name = f"phase3-task5b-{batch_id}"
    if run_index is not None:
        project_name = f"{project_name}-run-{run_index + 1}"
    result = (
        await client.schema("public")
        .table("projects")
        .insert(
            {
                "project_id": project_id,
                "account_id": account_id,
                "name": project_name,
                "description": f"Task 5b load harness batch {batch_id}",
                "created_at": datetime.now(),
            }
        )
    )
    if not getattr(result, "data", None):
        raise RuntimeError(f"Failed to create project for batch {batch_id}")
    return {
        "project_id": project_id,
        "project_name": project_name,
    }


async def _delete_project_fixture(client: Any, *, project_id: str) -> None:
    await client.table("projects").eq("project_id", project_id).delete()


async def _create_fixtures(
    *,
    batch_id: str,
    account_id: str,
    target_active_runs: int,
    sandbox_distribution_profile: str = _DEFAULT_SANDBOX_DISTRIBUTION_PROFILE,
) -> dict[str, Any]:
    client = await _get_client()
    resolved_distribution_profile = _resolve_sandbox_distribution_profile(
        sandbox_distribution_profile
    )
    requested_project_count = (
        max(1, int(target_active_runs))
        if resolved_distribution_profile == "per_run_project_multi_sandbox"
        else 1
    )
    projects: list[dict[str, str]] = []
    try:
        for run_index in range(requested_project_count):
            projects.append(
                await _create_project_fixture(
                    client,
                    batch_id=batch_id,
                    account_id=account_id,
                    run_index=(
                        run_index
                        if resolved_distribution_profile
                        == "per_run_project_multi_sandbox"
                        else None
                    ),
                )
            )
    except Exception:
        for project in projects:
            await _delete_project_fixture(
                client,
                project_id=str(project["project_id"]),
            )
        raise

    fixtures = {
        "batch_id": batch_id,
        "account_id": account_id,
        "projects": projects,
    }
    if len(projects) == 1:
        fixtures["project_id"] = str(projects[0]["project_id"])
        fixtures["project_name"] = str(projects[0]["project_name"])
    return {
        **fixtures,
    }


async def _fetch_attempt_rows(
    client: Any,
    *,
    agent_run_ids: list[str],
) -> list[dict[str, Any]]:
    if not agent_run_ids:
        return []
    result = (
        await client.table("regular_run_attempts")
        .select("*")
        .in_("agent_run_id", agent_run_ids)
        .execute()
    )
    return [dict(row) for row in (getattr(result, "data", None) or [])]


async def _fetch_run_rows(
    client: Any,
    *,
    agent_run_ids: list[str],
) -> list[dict[str, Any]]:
    if not agent_run_ids:
        return []
    result = (
        await client.table("agent_runs")
        .select("*")
        .in_("agent_run_id", agent_run_ids)
        .execute()
    )
    return [dict(row) for row in (getattr(result, "data", None) or [])]


async def _fetch_thread_rows_for_projects(
    client: Any,
    *,
    project_ids: list[str],
) -> list[dict[str, Any]]:
    if not project_ids:
        return []
    result = (
        await client.table("threads")
        .select("thread_id,project_id")
        .in_("project_id", project_ids)
        .execute()
    )
    return [dict(row) for row in (getattr(result, "data", None) or [])]


def _parse_project_sandbox_info(raw_value: Any) -> dict[str, Any]:
    if isinstance(raw_value, dict):
        return dict(raw_value)
    if isinstance(raw_value, str):
        try:
            parsed = json.loads(raw_value)
        except json.JSONDecodeError:
            return {}
        if isinstance(parsed, dict):
            return dict(parsed)
    return {}


async def _fetch_project_sandbox_ids_for_cleanup(
    client: Any,
    *,
    project_ids: list[str],
) -> list[str]:
    if not project_ids:
        return []

    result = (
        await client.table("projects")
        .select("project_id,sandbox")
        .in_("project_id", project_ids)
        .execute()
    )
    sandbox_ids: list[str] = []
    for row in getattr(result, "data", None) or []:
        sandbox_info = _parse_project_sandbox_info(
            dict(row).get("sandbox"),
        )
        sandbox_id = str(sandbox_info.get("id") or "").strip()
        if sandbox_id:
            sandbox_ids.append(sandbox_id)
    return list(dict.fromkeys(sandbox_ids))


async def _fetch_run_rows_for_threads(
    client: Any,
    *,
    thread_ids: list[str],
) -> list[dict[str, Any]]:
    if not thread_ids:
        return []
    result = (
        await client.table("agent_runs")
        .select("agent_run_id,thread_id")
        .in_("thread_id", thread_ids)
        .execute()
    )
    return [dict(row) for row in (getattr(result, "data", None) or [])]


async def _admit_runs(
    *,
    fixtures: dict[str, Any],
    batch_id: str,
    target_active_runs: int,
    metadata_target_tier: int | None = None,
    active_duration_seconds: float,
    artifact_run_count: int,
    execution_profile: str = _DEFAULT_EXECUTION_PROFILE,
    validation_profile: str = _DEFAULT_VALIDATION_PROFILE,
    sandbox_distribution_profile: str = _DEFAULT_SANDBOX_DISTRIBUTION_PROFILE,
    user_message_override: str | None = None,
    tracked_runs: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    client = await _get_client()
    admitted_runs = tracked_runs if tracked_runs is not None else []
    result_slots: list[dict[str, Any] | None] = [None] * target_active_runs
    project_fixtures = _fixture_projects(fixtures)
    if not project_fixtures:
        raise RuntimeError("No project fixtures are available for admission")
    resolved_distribution_profile = _resolve_sandbox_distribution_profile(
        sandbox_distribution_profile
    )
    resolved_metadata_target_tier = int(
        metadata_target_tier if metadata_target_tier is not None else target_active_runs
    )
    wave_size = _resolve_admission_wave_size(resolved_metadata_target_tier)
    wave_delay_seconds = _resolve_admission_wave_delay_seconds()

    async def _admit_single(index: int) -> dict[str, Any]:
        project_fixture = (
            project_fixtures[index]
            if resolved_distribution_profile == "per_run_project_multi_sandbox"
            else project_fixtures[0]
        )
        project_id = str(project_fixture["project_id"])
        thread_id = await _insert_thread(
            client,
            account_id=str(fixtures["account_id"]),
            project_id=project_id,
        )
        artifact_path = None
        if index < artifact_run_count:
            artifact_path = build_artifact_path(
                batch_id=batch_id,
                run_index=index,
            )
        metadata = build_harness_metadata(
            batch_id=batch_id,
            target_tier=resolved_metadata_target_tier,
            active_duration_seconds=active_duration_seconds,
            emit_stub_response=str(execution_profile or "").strip().lower()
            == _DEFAULT_EXECUTION_PROFILE,
            artifact_path=artifact_path,
            execution_profile=execution_profile,
            validation_profile=validation_profile,
            sandbox_distribution_profile=resolved_distribution_profile,
            user_message_override=(
                user_message_override
                or _default_user_message_override(
                    execution_profile=execution_profile,
                    artifact_path=artifact_path,
                )
            ),
        )
        tracked_run = {
            "thread_id": thread_id,
            "agent_run_id": None,
            "project_id": project_id,
            "artifact_path": artifact_path,
        }
        result_slots[index] = tracked_run
        queued_run = await regular_run_admission.admit_queued_regular_run(
            client=client,
            thread_id=thread_id,
            project_id=project_id,
            agent_run_id=str(uuid.uuid4()),
            agent_config_snapshot=None,
            agent_run_metadata=metadata,
            execution_mode="phase2_supervisor",
            ensure_phase2_attempt_queue_capacity=agent_api._ensure_phase2_attempt_queue_capacity_or_raise,
            create_initial_attempt=agent_api.regular_run_attempts.create_initial_attempt,
            dispatch_queue_handoff=agent_api._dispatch_regular_queue_handoff,
            mark_phase2_admission_attempt_failed=agent_api._mark_phase2_admission_attempt_failed_best_effort,
            update_agent_run_status=agent_api.update_agent_run_status,
        )
        tracked_run["agent_run_id"] = queued_run["agent_run_id"]
        return tracked_run

    first_error: Exception | None = None
    for wave_start in range(0, target_active_runs, wave_size):
        wave_end = min(target_active_runs, wave_start + wave_size)
        tasks = [
            asyncio.create_task(_admit_single(index))
            for index in range(wave_start, wave_end)
        ]
        results = await asyncio.gather(*tasks, return_exceptions=True)
        admitted_runs[:] = [
            tracked_run for tracked_run in result_slots if tracked_run is not None
        ]
        first_error = next(
            (result for result in results if isinstance(result, Exception)),
            None,
        )
        if first_error is not None:
            break
        if wave_end < target_active_runs:
            await asyncio.sleep(wave_delay_seconds)

    if first_error is not None:
        raise first_error

    return admitted_runs


async def _wait_for_target_active(
    *,
    agent_run_ids: list[str],
    target_active_runs: int,
    admission_started_at: float | None = None,
    poll_interval_seconds: float = 0.25,
    timeout_seconds: float = 120.0,
) -> dict[str, Any]:
    client = await _get_client()
    started_at = (
        float(admission_started_at)
        if admission_started_at is not None
        else time.monotonic()
    )
    peak_active_slots = 0
    p95_start_delay_ms: float | None = None
    while time.monotonic() - started_at < timeout_seconds:
        attempt_rows = await _fetch_attempt_rows(client, agent_run_ids=agent_run_ids)
        running_rows = [
            row
            for row in attempt_rows
            if str(row.get("status") or "").strip().lower() == "running"
        ]
        peak_active_slots = max(peak_active_slots, len(running_rows))
        running_count = len(running_rows)
        if running_count >= target_active_runs:
            start_delays = []
            for row in running_rows:
                queued_at = row.get("queued_at")
                running_at = row.get("running_at")
                if isinstance(queued_at, datetime) and isinstance(running_at, datetime):
                    start_delays.append((running_at - queued_at).total_seconds() * 1000)
            p95_start_delay_ms = _p95(start_delays)
            return {
                "time_to_target_active_ms": round(
                    (time.monotonic() - started_at) * 1000,
                    3,
                ),
                "peak_active_slots": peak_active_slots,
                "p95_run_start_delay_ms": p95_start_delay_ms,
            }
        await asyncio.sleep(poll_interval_seconds)
    raise TimeoutError(
        f"Timed out waiting for {target_active_runs} active runs; peak={peak_active_slots}"
    )


async def _wait_for_parent_status(
    *,
    agent_run_id: str,
    expected_status: str,
    timeout_seconds: float = 60.0,
) -> None:
    client = await _get_client()
    started_at = time.monotonic()
    while time.monotonic() - started_at < timeout_seconds:
        rows = await _fetch_run_rows(client, agent_run_ids=[agent_run_id])
        if rows and str(rows[0].get("status") or "").strip().lower() == expected_status:
            return
        await asyncio.sleep(0.25)
    raise TimeoutError(
        f"Timed out waiting for run {agent_run_id} to reach {expected_status}"
    )


async def _select_stop_candidates(
    *,
    batch_runs: list[dict[str, Any]],
    queued_limit: int,
    running_limit: int,
) -> list[str]:
    client = await _get_client()
    agent_run_ids = [str(run["agent_run_id"]) for run in batch_runs]
    attempt_rows = await _fetch_attempt_rows(client, agent_run_ids=agent_run_ids)
    queued_ids = [
        str(row["agent_run_id"])
        for row in attempt_rows
        if str(row.get("status") or "").strip().lower() == "queued"
    ][:queued_limit]
    running_ids = [
        str(row["agent_run_id"])
        for row in attempt_rows
        if str(row.get("status") or "").strip().lower() == "running"
    ][:running_limit]
    return queued_ids + running_ids


async def _issue_stop_sample(
    *,
    batch_runs: list[dict[str, Any]],
    queued_stop_count: int,
    running_stop_count: int,
) -> dict[str, Any]:
    candidates = await _select_stop_candidates(
        batch_runs=batch_runs,
        queued_limit=queued_stop_count,
        running_limit=running_stop_count,
    )
    latencies_ms: list[float] = []
    for agent_run_id in candidates:
        started_at = time.monotonic()
        await agent_api.stop_agent_run(agent_run_id)
        await _wait_for_parent_status(
            agent_run_id=agent_run_id,
            expected_status="stopped",
        )
        latencies_ms.append(round((time.monotonic() - started_at) * 1000, 3))
    return {
        "p95_stop_latency_ms": _p95(latencies_ms),
        "stopped_runs": len(candidates),
        "stopped_agent_run_ids": list(candidates),
        "latencies_ms": latencies_ms,
    }


async def _wait_for_terminal_convergence(
    *,
    agent_run_ids: list[str],
    poll_interval_seconds: float = 0.25,
    timeout_seconds: float = 180.0,
) -> dict[str, Any]:
    client = await _get_client()
    started_at = time.monotonic()
    expected_run_ids = [str(agent_run_id) for agent_run_id in agent_run_ids]
    expected_run_count = len(expected_run_ids)
    terminal_seen_ms: dict[str, float] = {}
    while True:
        elapsed_seconds = time.monotonic() - started_at
        if elapsed_seconds >= timeout_seconds:
            break
        run_rows = await _fetch_run_rows(client, agent_run_ids=agent_run_ids)
        rows_by_run_id = {
            str(row.get("agent_run_id")): row
            for row in run_rows
            if str(row.get("agent_run_id") or "").strip()
        }
        normalized_statuses = [
            str(rows_by_run_id.get(agent_run_id, {}).get("status") or "")
            .strip()
            .lower()
            for agent_run_id in expected_run_ids
        ]
        elapsed_ms = round(elapsed_seconds * 1000, 3)
        for agent_run_id, status in zip(expected_run_ids, normalized_statuses):
            if status in _TERMINAL_STATUSES and agent_run_id not in terminal_seen_ms:
                terminal_seen_ms[agent_run_id] = elapsed_ms
        if (
            len(rows_by_run_id) == expected_run_count
            and run_rows
            and all(status in _TERMINAL_STATUSES for status in normalized_statuses)
        ):
            stopped_count = sum(
                1 for status in normalized_statuses if status == "stopped"
            )
            completed_count = sum(
                1 for status in normalized_statuses if status == "completed"
            )
            completion_denominator = max(1, len(normalized_statuses) - stopped_count)
            return {
                "successful_completion_ratio": round(
                    completed_count / completion_denominator,
                    3,
                ),
                "p95_terminal_convergence_ms": _p95(
                    [
                        terminal_seen_ms[agent_run_id]
                        for agent_run_id in expected_run_ids
                    ]
                ),
            }
        await asyncio.sleep(poll_interval_seconds)
    raise TimeoutError("Timed out waiting for terminal convergence")


async def _verify_artifact_visibility(
    *,
    batch_runs: list[dict[str, Any]],
    expected_artifact_runs: int,
    excluded_agent_run_ids: set[str] | None = None,
) -> dict[str, Any]:
    if expected_artifact_runs <= 0:
        return {"artifact_visibility_ratio": 1.0}

    client = await _get_client()
    excluded_run_ids = {
        str(agent_run_id).strip()
        for agent_run_id in list(excluded_agent_run_ids or set())
        if str(agent_run_id).strip()
    }
    candidate_runs = [
        run
        for run in batch_runs[:expected_artifact_runs]
        if str(run.get("agent_run_id") or "").strip() not in excluded_run_ids
    ]
    if not candidate_runs:
        return {"artifact_visibility_ratio": 1.0}
    remaining_runs = [
        run for run in candidate_runs if str(run.get("artifact_path") or "").strip()
    ]
    visible_count = len(candidate_runs) - len(remaining_runs)
    max_attempts = 5
    for attempt_index in range(max_attempts):
        if not remaining_runs:
            break
        still_pending: list[dict[str, Any]] = []
        for run in remaining_runs:
            artifact_path = str(run.get("artifact_path") or "").strip()
            result = (
                await client.table("workspace_artifacts")
                .select("artifact_id")
                .eq("agent_run_id", str(run["agent_run_id"]))
                .eq("path", artifact_path)
                .execute()
            )
            if getattr(result, "data", None):
                visible_count += 1
            else:
                still_pending.append(run)
        remaining_runs = still_pending
        if remaining_runs and attempt_index + 1 < max_attempts:
            await asyncio.sleep(0.25)
    return {
        "artifact_visibility_ratio": round(
            visible_count / max(1, len(candidate_runs)),
            3,
        )
    }


async def _load_supervisor_counter_snapshots() -> dict[str, dict[str, Any]]:
    return await regular_supervisor_metrics.load_metrics_snapshots(
        redis_client=redis_service,
        runtime_token=regular_supervisor_metrics.get_runtime_token(),
    )


async def _load_supervisor_counter_deltas(
    baseline_snapshots: dict[str, dict[str, Any]],
) -> dict[str, dict[str, int]]:
    current_snapshots = await _load_supervisor_counter_snapshots()
    counter_fields = (
        "claim_success_count",
        "claim_empty_count",
        "claim_error_count",
        "reconcile_pass_count",
        "reconcile_recovered_count",
    )
    deltas: dict[str, dict[str, int]] = {}
    for supervisor_id, current_snapshot in current_snapshots.items():
        baseline_snapshot = baseline_snapshots.get(supervisor_id, {})
        deltas[supervisor_id] = {
            field: int(current_snapshot.get(field) or 0)
            - int(baseline_snapshot.get(field) or 0)
            for field in counter_fields
        }
    return deltas


async def _maybe_disturb_one_supervisor(
    *,
    enabled: bool,
    baseline_metrics: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    if not enabled:
        return {"enabled": False}

    supervisor_id = next(iter(sorted(baseline_metrics.keys())), None)
    if not supervisor_id:
        return {"enabled": True, "disturbed": False, "reason": "no_supervisors_visible"}

    try:
        pid = int(str(supervisor_id).rsplit(":", 1)[-1])
    except (TypeError, ValueError):
        return {
            "enabled": True,
            "disturbed": False,
            "reason": f"unparseable_supervisor_id:{supervisor_id}",
        }

    os.kill(pid, signal.SIGTERM)
    return {
        "enabled": True,
        "disturbed": True,
        "supervisor_id": supervisor_id,
        "pid": pid,
    }


async def _cleanup_batch(
    *,
    fixtures: dict[str, Any],
    admitted_runs: list[dict[str, Any]],
) -> None:
    client = await _get_client()
    tracked_agent_run_ids = [
        str(run["agent_run_id"])
        for run in admitted_runs
        if str(run.get("agent_run_id") or "").strip()
    ]
    project_ids = [
        str(project["project_id"])
        for project in _fixture_projects(fixtures)
        if str(project.get("project_id") or "").strip()
    ]
    should_discover_project_scoped_rows = any(
        not str(run.get("agent_run_id") or "").strip() for run in admitted_runs
    )
    discovered_thread_rows: list[dict[str, Any]] = []
    discovered_run_rows: list[dict[str, Any]] = []
    if should_discover_project_scoped_rows and project_ids:
        discovered_thread_rows = await _fetch_thread_rows_for_projects(
            client,
            project_ids=project_ids,
        )
        discovered_run_rows = await _fetch_run_rows_for_threads(
            client,
            thread_ids=[
                str(row["thread_id"])
                for row in discovered_thread_rows
                if str(row.get("thread_id") or "").strip()
            ],
        )
    agent_run_ids = list(
        dict.fromkeys(
            tracked_agent_run_ids
            + [
                str(row["agent_run_id"])
                for row in discovered_run_rows
                if str(row.get("agent_run_id") or "").strip()
            ]
        )
    )
    thread_ids = list(
        dict.fromkeys(
            [
                str(run["thread_id"])
                for run in admitted_runs
                if str(run.get("thread_id") or "").strip()
            ]
            + [
                str(row["thread_id"])
                for row in discovered_thread_rows
                if str(row.get("thread_id") or "").strip()
            ]
        )
    )
    sandbox_ids = await _fetch_project_sandbox_ids_for_cleanup(
        client,
        project_ids=project_ids,
    )

    for sandbox_id in sandbox_ids:
        try:
            await delete_sandbox(sandbox_id)
        except Exception as sandbox_cleanup_error:
            logger.warning(
                "Phase 3 harness cleanup failed to delete sandbox %s: %s",
                sandbox_id,
                sandbox_cleanup_error,
            )

    for agent_run_id in agent_run_ids:
        await client.table("regular_run_attempts").eq(
            "agent_run_id", agent_run_id
        ).delete()
        await client.table("agent_runs").eq("agent_run_id", agent_run_id).delete()
        await client.table("workspace_artifacts").eq(
            "agent_run_id", agent_run_id
        ).delete()

    for thread_id in thread_ids:
        await client.table("threads").eq("thread_id", thread_id).delete()

    for project in _fixture_projects(fixtures):
        await client.table("projects").eq(
            "project_id",
            str(project["project_id"]),
        ).delete()


async def run_tier(
    *,
    target_active_runs: int,
    account_id: str | None = None,
    active_duration_seconds: float = 20.0,
    queued_stop_count: int | None = None,
    running_stop_count: int | None = None,
    artifact_run_count: int | None = None,
    disturb_one_shard: bool | None = None,
    execution_profile: str = _DEFAULT_EXECUTION_PROFILE,
    validation_profile: str = _DEFAULT_VALIDATION_PROFILE,
    sandbox_distribution_profile: str = _DEFAULT_SANDBOX_DISTRIBUTION_PROFILE,
    user_message_override: str | None = None,
) -> dict[str, Any]:
    client = await _get_client()
    validation_profile_config = resolve_validation_profile(validation_profile)
    resolved_validation_profile = str(validation_profile_config["validation_profile"])
    resolved_queued_stop_count = (
        validation_profile_config["queued_stop_count"]
        if queued_stop_count is None
        else int(queued_stop_count)
    )
    resolved_running_stop_count = (
        validation_profile_config["running_stop_count"]
        if running_stop_count is None
        else int(running_stop_count)
    )
    resolved_disturb_one_shard = (
        bool(validation_profile_config["disturb_one_shard"])
        if disturb_one_shard is None
        else bool(disturb_one_shard)
    )
    queued_stop_budget = max(0, resolved_queued_stop_count)
    admission_target_runs = target_active_runs + queued_stop_budget
    resolved_distribution_profile = _resolve_sandbox_distribution_profile(
        sandbox_distribution_profile
    )
    resolved_artifact_run_count = _resolve_effective_artifact_run_count(
        target_active_runs=target_active_runs,
        artifact_run_count=artifact_run_count,
        execution_profile=execution_profile,
    )
    resolved_account_id = await _resolve_maybe_awaitable(
        resolve_account_id(
            client=client,
            explicit_account_id=account_id,
        )
    )
    batch_id = f"phase3-task5b-{target_active_runs}-{uuid.uuid4()}"
    fixtures = await _create_fixtures(
        batch_id=batch_id,
        account_id=str(resolved_account_id),
        target_active_runs=admission_target_runs,
        sandbox_distribution_profile=resolved_distribution_profile,
    )
    admitted_runs: list[dict[str, Any]] = []
    try:
        baseline_metrics = await _resolve_maybe_awaitable(
            _load_supervisor_counter_snapshots()
        )
        admission_started_at = time.monotonic()
        admitted_runs = await _resolve_maybe_awaitable(
            _admit_runs(
                fixtures=fixtures,
                batch_id=batch_id,
                target_active_runs=admission_target_runs,
                metadata_target_tier=target_active_runs,
                active_duration_seconds=active_duration_seconds,
                artifact_run_count=resolved_artifact_run_count,
                execution_profile=execution_profile,
                validation_profile=resolved_validation_profile,
                sandbox_distribution_profile=resolved_distribution_profile,
                user_message_override=user_message_override,
                tracked_runs=admitted_runs,
            )
        )
        queued_stop_result = await _resolve_maybe_awaitable(
            _issue_stop_sample(
                batch_runs=admitted_runs,
                queued_stop_count=resolved_queued_stop_count,
                running_stop_count=0,
            )
        )
        active_result = await _resolve_maybe_awaitable(
            _wait_for_target_active(
                agent_run_ids=[str(run["agent_run_id"]) for run in admitted_runs],
                target_active_runs=target_active_runs,
                admission_started_at=admission_started_at,
            )
        )
        disturbance_result = await _resolve_maybe_awaitable(
            _maybe_disturb_one_supervisor(
                enabled=resolved_disturb_one_shard,
                baseline_metrics=baseline_metrics,
            )
        )
        running_stop_result = await _resolve_maybe_awaitable(
            _issue_stop_sample(
                batch_runs=admitted_runs,
                queued_stop_count=0,
                running_stop_count=resolved_running_stop_count,
            )
        )
        stop_latency_samples = list(queued_stop_result.get("latencies_ms") or [])
        stop_latency_samples.extend(list(running_stop_result.get("latencies_ms") or []))
        if not stop_latency_samples:
            for result in (queued_stop_result, running_stop_result):
                p95_latency = result.get("p95_stop_latency_ms")
                if p95_latency is not None:
                    stop_latency_samples.append(float(p95_latency))
        stopped_agent_run_ids = {
            str(agent_run_id)
            for result in (queued_stop_result, running_stop_result)
            for agent_run_id in list(result.get("stopped_agent_run_ids") or [])
            if str(agent_run_id).strip()
        }
        stop_result = {
            "p95_stop_latency_ms": _p95(stop_latency_samples),
            "stopped_runs": len(stopped_agent_run_ids),
            "stopped_agent_run_ids": sorted(stopped_agent_run_ids),
        }
        terminal_result = await _resolve_maybe_awaitable(
            _wait_for_terminal_convergence(
                agent_run_ids=[str(run["agent_run_id"]) for run in admitted_runs],
            )
        )
        artifact_result = await _resolve_maybe_awaitable(
            _verify_artifact_visibility(
                batch_runs=admitted_runs,
                expected_artifact_runs=resolved_artifact_run_count,
                excluded_agent_run_ids=stopped_agent_run_ids,
            )
        )
        shard_counter_deltas = await _resolve_maybe_awaitable(
            _load_supervisor_counter_deltas(baseline_metrics)
        )
        return {
            "target_active_runs": target_active_runs,
            "execution_profile": execution_profile,
            "validation_profile": resolved_validation_profile,
            "sandbox_distribution_profile": resolved_distribution_profile,
            "fixture_project_count": len(_fixture_projects(fixtures)),
            "expected_sandbox_count": len(_fixture_projects(fixtures)),
            "comparison_key": "|".join(
                [
                    execution_profile,
                    resolved_validation_profile,
                    resolved_distribution_profile,
                ]
            ),
            "successful_completion_ratio": terminal_result[
                "successful_completion_ratio"
            ],
            "artifact_visibility_ratio": artifact_result["artifact_visibility_ratio"],
            "time_to_target_active_ms": active_result["time_to_target_active_ms"],
            "peak_active_slots": active_result["peak_active_slots"],
            "p95_run_start_delay_ms": active_result["p95_run_start_delay_ms"],
            "p95_terminal_convergence_ms": terminal_result[
                "p95_terminal_convergence_ms"
            ],
            "p95_stop_latency_ms": stop_result["p95_stop_latency_ms"],
            "shard_counter_deltas": shard_counter_deltas,
            "disturbance": disturbance_result,
        }
    finally:
        await _resolve_maybe_awaitable(
            _cleanup_batch(fixtures=fixtures, admitted_runs=admitted_runs)
        )


async def run_matrix(
    *,
    targets: tuple[int, ...] = (25, 50, 100),
    account_id: str | None = None,
    active_duration_seconds: float = 20.0,
    queued_stop_count: int | None = None,
    running_stop_count: int | None = None,
    artifact_run_count: int | None = None,
    disturb_one_shard: bool | None = None,
    execution_profile: str = _DEFAULT_EXECUTION_PROFILE,
    validation_profile: str = _DEFAULT_VALIDATION_PROFILE,
    sandbox_distribution_profile: str = _DEFAULT_SANDBOX_DISTRIBUTION_PROFILE,
    user_message_override: str | None = None,
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for target in targets:
        results.append(
            await run_tier(
                target_active_runs=target,
                account_id=account_id,
                active_duration_seconds=active_duration_seconds,
                queued_stop_count=queued_stop_count,
                running_stop_count=running_stop_count,
                artifact_run_count=artifact_run_count,
                disturb_one_shard=disturb_one_shard,
                execution_profile=execution_profile,
                validation_profile=validation_profile,
                sandbox_distribution_profile=sandbox_distribution_profile,
                user_message_override=user_message_override,
            )
        )
    return results


def _parse_targets(raw_targets: str) -> tuple[int, ...]:
    parsed_targets = tuple(
        max(1, int(segment.strip()))
        for segment in str(raw_targets or "").split(",")
        if segment.strip()
    )
    return parsed_targets or (25, 50, 100)


async def _run_from_args(args: argparse.Namespace) -> int:
    validate_worktree_runtime_binding()
    results = await run_matrix(
        targets=_parse_targets(args.targets),
        account_id=args.account_id,
        active_duration_seconds=args.active_duration_seconds,
        queued_stop_count=args.queued_stop_count,
        running_stop_count=args.running_stop_count,
        artifact_run_count=args.artifact_run_count,
        disturb_one_shard=args.disturb_one_shard,
        execution_profile=args.execution_profile,
        validation_profile=args.validation_profile,
        sandbox_distribution_profile=args.sandbox_distribution_profile,
        user_message_override=args.user_message_override,
    )
    for result in results:
        print(json.dumps(result, sort_keys=True))
    return 0


def _build_argument_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Production-faithful Phase 3 Task 5b regular load harness",
    )
    parser.add_argument("--targets", default="25,50,100")
    parser.add_argument("--account-id", default=None)
    parser.add_argument("--active-duration-seconds", type=float, default=20.0)
    parser.add_argument("--queued-stop-count", type=int, default=None)
    parser.add_argument("--running-stop-count", type=int, default=None)
    parser.add_argument("--artifact-run-count", type=int, default=None)
    parser.add_argument("--disturb-one-shard", action="store_true", default=None)
    parser.add_argument("--execution-profile", default=_DEFAULT_EXECUTION_PROFILE)
    parser.add_argument("--validation-profile", default=_DEFAULT_VALIDATION_PROFILE)
    parser.add_argument(
        "--sandbox-distribution-profile",
        default=_DEFAULT_SANDBOX_DISTRIBUTION_PROFILE,
    )
    parser.add_argument("--user-message-override", default=None)
    return parser


def main() -> int:
    parser = _build_argument_parser()
    args = parser.parse_args()
    return asyncio.run(_run_from_args(args))


if __name__ == "__main__":
    raise SystemExit(main())
