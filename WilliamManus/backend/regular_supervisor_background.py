from __future__ import annotations

import importlib
import json
from pathlib import Path
import os
import uuid

import dramatiq  # type: ignore
import dotenv
import structlog
from dramatiq.brokers.redis import RedisBroker  # type: ignore

from services import redis
from services import regular_supervisor_runtime
from services import regular_supervisor_metrics
from services.postgresql import DBConnection
from utils.retry import retry
from utils.dramatiq_queue_names import REGULAR_SUPERVISOR_QUEUE
from utils.config import config
from utils.logger import logger

_ENV_PATH = Path(__file__).resolve().parent / ".env"
dotenv.load_dotenv(dotenv_path=_ENV_PATH, override=False)


_BaseDramatiqMiddleware = getattr(dramatiq.middleware, "Middleware", object)
_TRUE_ENV_VALUES = frozenset({"1", "true", "yes", "on"})
PERSISTENT_SUPERVISOR_WAKEUP_SUPERVISOR_ID = "__persistent_supervisor_wakeup__"


def build_supervisor_id() -> str:
    shard_prefix = str(
        os.getenv("REGULAR_SUPERVISOR_SHARD_PREFIX") or "regular-supervisor"
    ).strip()
    if not shard_prefix:
        shard_prefix = "regular-supervisor"
    return f"{shard_prefix}:{os.getpid()}"


def resolve_supervisor_id(supervisor_id: str) -> str:
    normalized_supervisor_id = str(supervisor_id or "").strip()
    if normalized_supervisor_id == PERSISTENT_SUPERVISOR_WAKEUP_SUPERVISOR_ID:
        return build_supervisor_id()
    return normalized_supervisor_id


class _RegularSupervisorBootstrapMiddleware(_BaseDramatiqMiddleware):
    @property
    def actor_options(self):
        return set()

    @property
    def forks(self):
        return []

    def after_worker_boot(self, broker, worker):
        try:
            regular_supervisor_background.send(
                supervisor_id=build_supervisor_id()
            )
        except Exception as dispatch_error:
            logger.warning(
                "Failed to enqueue bootstrap regular supervisor actor: %s",
                dispatch_error,
            )


redis_host = os.getenv("REDIS_HOST", "redis")
redis_port = int(os.getenv("REDIS_PORT", 6379))
redis_password = os.getenv("REDIS_PASSWORD", "")
redis_db = int(os.getenv("REDIS_DB", 0))

dramatiq_middlewares = [
    dramatiq.middleware.AsyncIO(),
    dramatiq.middleware.Retries(),
    dramatiq.middleware.TimeLimit(),
    _RegularSupervisorBootstrapMiddleware(),
]

if redis_password:
    redis_broker = RedisBroker(
        host=redis_host,
        port=redis_port,
        password=redis_password,
        db=redis_db,
        middleware=dramatiq_middlewares,
    )
else:
    redis_broker = RedisBroker(
        host=redis_host,
        port=redis_port,
        db=redis_db,
        middleware=dramatiq_middlewares,
    )

dramatiq.set_broker(redis_broker)

_initialized = False
db = DBConnection()


async def initialize() -> None:
    global _initialized

    if _initialized:
        return

    await retry(lambda: redis.initialize_async())
    await db.initialize()
    _initialized = True
    logger.info("Initialized regular supervisor runtime")


def get_regular_supervisor_max_runs_per_process() -> int:
    raw_value = str(os.getenv("REGULAR_SUPERVISOR_MAX_RUNS_PER_PROCESS") or "").strip()
    try:
        parsed_value = int(raw_value)
    except (TypeError, ValueError):
        return 4
    return max(1, parsed_value)


def get_regular_supervisor_actor_time_limit_ms() -> int:
    if str(os.getenv("REGULAR_SUPERVISOR_PERSISTENT", "false")).strip().lower() in _TRUE_ENV_VALUES:
        return float("inf")
    raw_value = str(os.getenv("REGULAR_SUPERVISOR_TIME_LIMIT_MS") or "").strip()
    try:
        parsed_value = int(raw_value)
    except (TypeError, ValueError):
        return 86_400_000
    return max(1_000, parsed_value)


def get_regular_supervisor_actor_max_retries() -> int | None:
    if str(os.getenv("REGULAR_SUPERVISOR_PERSISTENT", "false")).strip().lower() in _TRUE_ENV_VALUES:
        return None
    raw_value = str(os.getenv("REGULAR_SUPERVISOR_MAX_RETRIES") or "").strip()
    try:
        parsed_value = int(raw_value)
    except (TypeError, ValueError):
        return 20
    return max(0, parsed_value)


def _coerce_regular_run_metadata(value: object) -> dict[str, object]:
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


async def _load_agent_run_row(
    client,
    *,
    agent_run_id: str,
) -> dict[str, object] | None:
    normalized_agent_run_id = str(agent_run_id or "").strip()
    if not normalized_agent_run_id:
        raise ValueError("agent_run_id is required")

    result = (
        await client.table("agent_runs")
        .select("agent_run_id, thread_id, metadata")
        .eq("agent_run_id", normalized_agent_run_id)
        .execute()
    )
    rows = list(getattr(result, "data", None) or [])
    if not rows:
        return None
    agent_run_row = dict(rows[0])
    thread_id = str(agent_run_row.get("thread_id") or "").strip()
    if not thread_id:
        return agent_run_row

    thread_result = (
        await client.table("threads")
        .select("project_id")
        .eq("thread_id", thread_id)
        .execute()
    )
    thread_rows = list(getattr(thread_result, "data", None) or [])
    if thread_rows:
        agent_run_row["project_id"] = thread_rows[0].get("project_id")
    return agent_run_row


def _load_regular_run_kernel():
    run_agent_background_module = importlib.import_module("run_agent_background")
    return (
        run_agent_background_module.execute_regular_run_kernel,
        str(getattr(run_agent_background_module, "instance_id", "single")),
    )


async def _execute_claimed_attempt(
    attempt_row,
    owner_token: str,
    *,
    control_plane_slot=None,
) -> None:
    client = await db.client
    agent_run_id = str(attempt_row.get("agent_run_id") or "").strip()
    attempt_id = str(attempt_row.get("attempt_id") or "").strip()
    execution_epoch = int(attempt_row.get("execution_epoch") or 0)
    if not agent_run_id:
        raise ValueError("attempt_row.agent_run_id is required")
    if not attempt_id:
        raise ValueError("attempt_row.attempt_id is required")

    agent_run_row = await _load_agent_run_row(
        client,
        agent_run_id=agent_run_id,
    )
    if agent_run_row is None:
        raise RuntimeError(f"agent run {agent_run_id} is missing")

    metadata = _coerce_regular_run_metadata(agent_run_row.get("metadata"))
    resolved_model = str(metadata.get("model_name") or config.MODEL_TO_USE).strip()
    if not resolved_model:
        resolved_model = config.MODEL_TO_USE
    agent_config_snapshot = metadata.get("agent_config_snapshot")
    if not isinstance(agent_config_snapshot, dict):
        agent_config_snapshot = None
    shadow_clone_mode = str(metadata.get("shadow_clone_mode") or "off").strip().lower()
    if shadow_clone_mode in {"", "false", "none", "disabled"}:
        shadow_clone_mode = "off"
    shadow_clone_main_model = metadata.get("shadow_clone_main_model")
    shadow_clone_subagent_model = metadata.get("shadow_clone_subagent_model")
    run_capacity_mode = shadow_clone_mode if shadow_clone_mode != "off" else "off"

    execute_regular_run_kernel, current_instance_id = _load_regular_run_kernel()
    kernel_kwargs = {
        "client": client,
        "agent_run_id": agent_run_id,
        "thread_id": str(agent_run_row.get("thread_id") or ""),
        "instance_id": current_instance_id,
        "project_id": str(agent_run_row.get("project_id") or ""),
        "model_name": resolved_model,
        "enable_thinking": metadata.get("enable_thinking"),
        "reasoning_effort": metadata.get("reasoning_effort"),
        "stream": True,
        "enable_context_manager": bool(metadata.get("enable_context_manager", True)),
        "agent_config": agent_config_snapshot,
        "is_agent_builder": False,
        "target_agent_id": None,
        "request_id": metadata.get("request_id"),
        "resume_strategy": str(metadata.get("resume_strategy") or "auto"),
        "resume_window_minutes": int(metadata.get("resume_window_minutes") or 1440),
        "shadow_clone_mode": shadow_clone_mode,
        "shadow_clone_main_model": shadow_clone_main_model,
        "shadow_clone_subagent_model": shadow_clone_subagent_model,
        "owner_token": owner_token,
        "run_capacity_mode": run_capacity_mode,
        "attempt_id": attempt_id,
        "execution_epoch": execution_epoch,
        "run_metadata": metadata,
    }
    if control_plane_slot is not None:
        kernel_kwargs["control_plane_slot"] = control_plane_slot

    await execute_regular_run_kernel(
        **kernel_kwargs,
    )


@dramatiq.actor(
    queue_name=REGULAR_SUPERVISOR_QUEUE,
    time_limit=get_regular_supervisor_actor_time_limit_ms(),
    max_retries=get_regular_supervisor_actor_max_retries(),
)
async def regular_supervisor_background(supervisor_id: str) -> None:
    structlog.contextvars.clear_contextvars()

    normalized_supervisor_id = resolve_supervisor_id(supervisor_id)
    if not normalized_supervisor_id:
        raise ValueError("supervisor_id is required")

    structlog.contextvars.bind_contextvars(supervisor_id=normalized_supervisor_id)

    await initialize()
    client = await db.client
    owner_token = f"{normalized_supervisor_id}:{uuid.uuid4()}"
    metrics_sink = None
    if regular_supervisor_metrics.metrics_enabled():
        metrics_sink = regular_supervisor_metrics.build_redis_metrics_sink(
            redis_client=redis,
            supervisor_id=normalized_supervisor_id,
            runtime_token=regular_supervisor_metrics.get_runtime_token(),
        )
    completed_attempt_ids = await regular_supervisor_runtime.run_supervisor_loop(
        client=client,
        owner_token=owner_token,
        supervisor_id=normalized_supervisor_id,
        executor=_execute_claimed_attempt,
        max_concurrency=get_regular_supervisor_max_runs_per_process(),
        metrics_sink=metrics_sink,
    )
    logger.info(
        "Regular supervisor loop completed supervisor_id=%s completed_attempts=%s",
        normalized_supervisor_id,
        len(completed_attempt_ids),
    )
