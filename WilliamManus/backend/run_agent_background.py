import dotenv
from pathlib import Path

_ENV_PATH = Path(__file__).resolve().parent / ".env"
dotenv.load_dotenv(dotenv_path=_ENV_PATH, override=False)


import sentry
import asyncio
import hashlib
import inspect
import json
import traceback
from datetime import datetime, timezone
import time
from typing import Optional
from services import redis
from services import regular_load_harness_stub
from services.workspace_artifacts import workspace_artifacts
from agent.run import run_agent
from utils.logger import logger, structlog
import dramatiq  # type: ignore
import uuid
from services.postgresql import DBConnection
from services import redis
from services import regular_async_pool
from services.run_capacity import (
    build_run_capacity_limit_detail,
    get_run_capacity_cost,
    is_server_concurrency_budget_enabled,
    refresh_run_capacity_lease,
    release_run_capacity_lease,
    try_acquire_run_capacity_lease,
)
from dramatiq.brokers.redis import RedisBroker  # type: ignore
import os
from services.langfuse import (
    filter_registered_scores,
    generate_trace_id,
    get_trace_url,
    langfuse,
    score_many_numeric,
    start_root_span,
)
from services.eval_runtime_registry import (
    pop_eval_run,
    start_eval_run,
    update_eval_run_metadata,
)
from services.metrics_collector import compute_phase1_score_bundle
from utils.config import config
from utils.retry import retry
from utils.agent_run_context import (
    clear_agent_run_context,
    get_agent_model_context,
    set_agent_run_context,
)
from utils.dramatiq_queue_names import (
    RUN_AGENT_BACKGROUND_QUEUE,
    SANDBOX_CLEANUP_QUEUE,
    SHADOW_CLONE_SUBAGENT_QUEUE,
)
from agentscope_integration.utils.tool_arg_merge import (
    extract_partial_write_file_input,
    is_plausible_write_file_path,
    sanitize_write_file_input,
)
from agentscope_integration.memory.post_run_review import schedule_post_run_review
from agentscope_integration.shadow_clone.constants import (
    ShadowCloneMode,
    resolve_shadow_clone_mode,
)

import sentry_sdk  # type: ignore
from typing import Dict, Any

# 使用与 services/redis.py 相同的配置
redis_host = os.getenv("REDIS_HOST", "redis")
redis_port = int(os.getenv("REDIS_PORT", 6379))
redis_password = os.getenv("REDIS_PASSWORD", "")
redis_db = int(os.getenv("REDIS_DB", 0))

# Log broker configuration for debugging (without secrets)
logger.info(
    "Dramatiq Redis broker config: host=%s port=%s db=%s password_set=%s env_file=%s",
    redis_host,
    redis_port,
    redis_db,
    bool(redis_password),
    str(_ENV_PATH),
)
logger.info(
    "Dramatiq queue config: run_agent=%s sandbox_cleanup=%s shadow_clone_subagents=%s",
    RUN_AGENT_BACKGROUND_QUEUE,
    SANDBOX_CLEANUP_QUEUE,
    SHADOW_CLONE_SUBAGENT_QUEUE,
)

# 创建Redis broker，使用与 services/redis.py 相同的配置
dramatiq_middlewares = [
    dramatiq.middleware.AsyncIO(),
    dramatiq.middleware.Retries(),
    dramatiq.middleware.TimeLimit(),
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
instance_id = "single"


def _read_int_env(name: str, default: int) -> int:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default

    try:
        return int(raw_value)
    except (TypeError, ValueError):
        return default


SANDBOX_CLEANUP_LOCK_TTL_SECONDS = _read_int_env(
    "SANDBOX_CLEANUP_LOCK_TTL_SECONDS",
    1800,
)
SANDBOX_CLEANUP_ACTOR_TIME_LIMIT_SECONDS = _read_int_env(
    "SANDBOX_CLEANUP_ACTOR_TIME_LIMIT_SECONDS",
    120,
)
RUN_LOCK_TTL_SECONDS = max(
    60,
    _read_int_env("AGENTSCOPE_RUN_LOCK_TTL_SECONDS", 300),
)


def _normalize_optional_attempt_id(attempt_id: Optional[str]) -> Optional[str]:
    normalized_attempt_id = str(attempt_id or "").strip()
    return normalized_attempt_id or None


def _normalize_optional_execution_epoch(
    execution_epoch: Optional[int],
) -> Optional[int]:
    if execution_epoch is None:
        return None
    normalized_execution_epoch = max(0, int(execution_epoch))
    return normalized_execution_epoch or None


def _build_response_list_key(
    agent_run_id: str,
    *,
    execution_epoch: Optional[int] = None,
) -> str:
    normalized_agent_run_id = str(agent_run_id or "").strip()
    if not normalized_agent_run_id:
        raise ValueError("agent_run_id is required")
    normalized_execution_epoch = _normalize_optional_execution_epoch(execution_epoch)
    if normalized_execution_epoch is None:
        return f"agent_run:{normalized_agent_run_id}:responses"
    return f"agent_run:{normalized_agent_run_id}:epoch:{normalized_execution_epoch}:responses"


def _build_run_capacity_lease_key(
    agent_run_id: str,
    *,
    attempt_id: Optional[str] = None,
) -> str:
    return _normalize_optional_attempt_id(attempt_id) or str(agent_run_id or "").strip()


def _resolve_execution_origin(agent_config: Optional[dict]) -> str:
    execution_origin = "interactive"
    if not isinstance(agent_config, dict):
        return execution_origin

    raw_origin = str(agent_config.get("execution_origin") or "").strip().lower()
    if raw_origin:
        return raw_origin
    if agent_config.get("trigger_execution"):
        return "trigger"
    if agent_config.get("workflow_execution"):
        return "workflow"
    return execution_origin


def _resolve_effective_shadow_clone_mode(
    *,
    shadow_clone_mode: Optional[str],
    agent_config: Optional[dict],
):
    execution_origin = _resolve_execution_origin(agent_config)
    return (
        resolve_shadow_clone_mode(
            shadow_clone_mode=shadow_clone_mode,
            execution_origin=execution_origin,
        ),
        execution_origin,
    )


def _extract_tool_status_payload(payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if payload.get("type") != "status":
        return None

    content = payload.get("content")
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except Exception:
            return None
    if not isinstance(content, dict):
        return None

    status_type = str(content.get("status_type") or "").strip()
    if status_type not in {"tool_started", "tool_completed", "tool_failed"}:
        return None
    return content


def _is_terminal_compensation_tool_status(content_payload: Dict[str, Any]) -> bool:
    function_name = str(content_payload.get("function_name") or "").strip().lower()
    if function_name != "write_file":
        return False
    message = str(content_payload.get("message") or "")
    return message.startswith("Run ended with status '")


def _build_phase1_tool_events_from_responses(
    responses: list[Any],
) -> list[dict[str, Any]]:
    tool_events: list[dict[str, Any]] = []

    for raw in responses:
        try:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            payload = json.loads(raw) if isinstance(raw, str) else raw
        except Exception:
            continue

        if not isinstance(payload, dict):
            continue

        content_payload = _extract_tool_status_payload(payload)
        if not content_payload or _is_terminal_compensation_tool_status(
            content_payload
        ):
            continue

        status_type = str(content_payload.get("status_type") or "").strip()
        if status_type not in {"tool_completed", "tool_failed"}:
            continue

        tool_name = str(content_payload.get("function_name") or "").strip()
        if not tool_name:
            continue

        tool_events.append(
            {
                "tool_name": tool_name,
                "success": status_type == "tool_completed",
            }
        )

    return tool_events


_REFRESH_RUN_LOCK_LUA = """
local key = KEYS[1]
local expected_owner = ARGV[1]
local ttl = tonumber(ARGV[2])

local current_owner = redis.call('GET', key)
if not current_owner then
  return 0
end

if tostring(current_owner) ~= expected_owner then
  return -1
end

redis.call('EXPIRE', key, ttl)
return 1
"""

_RELEASE_RUN_LOCK_LUA = """
local key = KEYS[1]
local expected_owner = ARGV[1]

local current_owner = redis.call('GET', key)
if not current_owner then
  return 0
end

if tostring(current_owner) ~= expected_owner then
  return -1
end

return redis.call('DEL', key)
"""

_TAKE_OVER_RUN_LOCK_LUA = """
local key = KEYS[1]
local expected_owner = ARGV[1]
local new_owner = ARGV[2]
local ttl = tonumber(ARGV[3])

local current_owner = redis.call('GET', key)
if not current_owner then
  return 0
end

if tostring(current_owner) ~= expected_owner then
  return -1
end

redis.call('SET', key, new_owner, 'EX', ttl)
return 1
"""

_PUSH_RESPONSE_WITH_NOTIFY_LUA = """
local response_list_key = KEYS[1]
local response_channel = KEYS[2]
local payload_json = ARGV[1]
local initial_ttl_seconds = tonumber(ARGV[2]) or 0
local publish_message = ARGV[3]

local new_length = redis.call('RPUSH', response_list_key, payload_json)
if initial_ttl_seconds > 0 and new_length == 1 then
  redis.call('EXPIRE', response_list_key, initial_ttl_seconds)
end
if publish_message and publish_message ~= '' then
  redis.call('PUBLISH', response_channel, publish_message)
end
return new_length
"""


async def initialize():
    """Initialize the agent API with resources from the main API."""
    global db, instance_id, _initialized

    if not instance_id:
        instance_id = str(uuid.uuid4())[:8]
    await retry(lambda: redis.initialize_async())
    await db.initialize()

    _warmup_langfuse_prompts()

    _initialized = True
    logger.info(f"Initialized agent API with instance ID: {instance_id}")


def _warmup_langfuse_prompts() -> None:
    """Pre-fetch Langfuse prompts so the first agent_run doesn't pay cold-start latency.

    Failures are non-fatal — the prompt modules all have hardcoded fallbacks."""
    try:
        from services.langfuse import enabled, langfuse
    except Exception as e:
        logger.warning("[warmup] services.langfuse import failed: %s", e)
        return
    if not enabled:
        return
    for name in ("skill-awareness", "orchestrator-system", "worker-system"):
        try:
            langfuse.get_prompt(name)
            logger.info("[warmup] Pre-fetched prompt: %s", name)
        except Exception as e:
            logger.warning("[warmup] Failed to pre-fetch %s: %s", name, e)


@dramatiq.actor
async def check_health(key: str):
    """Run the agent in the background using Redis for state."""
    structlog.contextvars.clear_contextvars()
    await redis.set(key, "healthy", ex=redis.REDIS_KEY_TTL)


@dramatiq.actor(
    queue_name=SANDBOX_CLEANUP_QUEUE,
    time_limit=SANDBOX_CLEANUP_ACTOR_TIME_LIMIT_SECONDS * 1000,
)
async def delete_sandbox_background(
    sandbox_id: str,
    thread_id: Optional[str] = None,
    project_id: Optional[str] = None,
):
    """Delete sandbox in background so thread delete API can return quickly."""
    structlog.contextvars.clear_contextvars()
    structlog.contextvars.bind_contextvars(
        sandbox_id=sandbox_id,
        thread_id=thread_id,
        project_id=project_id,
    )

    await initialize()

    cleanup_lock_key = f"sandbox_cleanup_lock:{sandbox_id}"
    cleanup_owner = thread_id or str(uuid.uuid4())
    lock_acquired = await redis.set(
        cleanup_lock_key,
        cleanup_owner,
        nx=True,
        ex=SANDBOX_CLEANUP_LOCK_TTL_SECONDS,
    )

    if not lock_acquired:
        logger.info(
            "Sandbox cleanup already in progress, skipping duplicate request "
            "(sandbox_id=%s, thread_id=%s)",
            sandbox_id,
            thread_id,
        )
        return

    try:
        from sandbox.sandbox import delete_sandbox

        await delete_sandbox(sandbox_id)
        logger.info(
            "Sandbox cleanup completed (sandbox_id=%s, thread_id=%s)",
            sandbox_id,
            thread_id,
        )
    finally:
        try:
            await redis.delete(cleanup_lock_key)
        except Exception as cleanup_error:
            logger.warning(
                "Failed to clear sandbox cleanup lock %s: %s",
                cleanup_lock_key,
                cleanup_error,
            )


"""
Redis:内存数据库，做键值存储 + 发布订阅，项目中的角色定位是：数据存储 + 通信枢纽
Dramatiq:任务队列，做异步任务调度，项目中的角色定位是：后台任务管理器

@dramatiq.actor：将普通函数转换为可调度的后台任务， 自动添加 .send() 方法，让函数可以被Dramatiq worker执行
"""


async def execute_regular_run_kernel(
    agent_run_id: str,
    thread_id: str,
    instance_id: str,
    project_id: str,
    model_name: str,
    enable_thinking: Optional[bool],
    reasoning_effort: Optional[str],
    stream: bool,
    enable_context_manager: bool,
    agent_config: Optional[dict] = None,
    is_agent_builder: Optional[bool] = False,
    target_agent_id: Optional[str] = None,
    request_id: Optional[str] = None,
    resume_strategy: str = "auto",
    resume_window_minutes: int = 1440,
    shadow_clone_mode: Optional[str] = None,
    shadow_clone_main_model: Optional[str] = None,
    shadow_clone_subagent_model: Optional[str] = None,
    owner_token: Optional[str] = None,
    run_capacity_mode: Optional[Any] = None,
    client: Optional[Any] = None,
    attempt_id: Optional[str] = None,
    execution_epoch: Optional[int] = None,
    control_plane_slot: Optional[Any] = None,
    run_metadata: Optional[Dict[str, Any]] = None,
    user_message_override: Optional[str] = None,
):
    """Run the agent in the background using Redis for state."""
    # 并发场景下：管理结构化日志的上下文变量，确保每个Agent运行任务有独立、干净的日志上下文
    # 先清除所有上下文变量
    structlog.contextvars.clear_contextvars()
    # 再绑定新的上下文变量（当前运行）
    # 为当前任务绑定新的上下文变量
    # 效果: 后续所有日志都会自动包含这些字段
    structlog.contextvars.bind_contextvars(
        agent_run_id=agent_run_id,
        thread_id=thread_id,
        request_id=request_id,
    )
    normalized_attempt_id = _normalize_optional_attempt_id(attempt_id)
    normalized_execution_epoch = _normalize_optional_execution_epoch(execution_epoch)
    set_agent_run_context(
        agent_run_id,
        thread_id,
        model_name=model_name,
        current_execution_epoch=normalized_execution_epoch,
    )
    try:
        # 初始化 Redis 和 Postgresql 连接实例
        await initialize()
        logger.info(f"Initialized Redis and Postgresql connection successfully")
    except Exception as e:
        logger.error(f"Failed to initialize Redis connection: {e}")
        raise e

    # 锁机制确保只有一个实例处理同一个agent_run_id
    # 避免重复执行和资源浪费
    run_lock_key = f"agent_run_lock:{agent_run_id}"
    run_lock_owner = str(owner_token or f"{instance_id}:{uuid.uuid4()}")

    # 获取运行锁
    # 多个后端实例同时运行
    # Instance-A: 尝试处理 agent_run_123
    # Instance-B: 也尝试处理 agent_run_123  拒绝：被锁阻止
    # Instance-C: 也尝试处理 agent_run_123  拒绝：被锁阻止
    # 只有Instance-A成功获取锁，继续执行
    # 其他实例被阻塞，直到锁释放
    # 锁释放后，其他实例可以重新尝试获取锁
    try:
        # TTL = 过期时间：设置锁时同时设置过期时间
        # # 一般设置
        # REDIS_KEY_TTL = 3600  # 1小时
        # REDIS_KEY_TTL = 1800  # 30分钟
        # REDIS_KEY_TTL = 600   # 10分钟 (短任务)

        # 平衡考虑
        # 太短: 长任务还没完成锁就过期了 → 重复执行
        # 太长: 崩溃后等待时间太久 → 影响恢复速度
        lock_acquired = await redis.set(
            run_lock_key,
            run_lock_owner,
            nx=True,
            ex=RUN_LOCK_TTL_SECONDS,
        )
        logger.info(f"Redis SET command completed: {lock_acquired}")
    except Exception as redis_error:
        logger.error(
            f"Redis lock operation failed, Error details: {traceback.format_exc()}"
        )
        raise redis_error

    # Redis分布式锁机制
    #   ↓
    # 第一次尝试获取锁 (nx=True)
    #   ↓
    # ├─成功 → 继续执行Agent
    # ├─失败 → 检查现有实例
    #   ↓
    #   ├─有实例值 → 退出 (避免重复)
    #   ├─无实例值 → 第二次尝试获取
    #           ↓
    #           ├─成功 → 继续执行Agent
    #           ├─失败 → 退出 (其他实例抢到了)
    if not lock_acquired:
        # 检查是否已有其他实例在处理
        try:
            existing_instance = await redis.get(run_lock_key)
            logger.info(f"Existing instance: {existing_instance}")
        except Exception as redis_error:
            logger.error(
                f"Redis GET operation failed, Error details: {traceback.format_exc()}"
            )
            raise redis_error
        if existing_instance:
            # 已有实例在处理，把 existing_instance 转换为字符串，做一个日志记录
            existing_instance_str = (
                existing_instance.decode()
                if isinstance(existing_instance, bytes)
                else existing_instance
            )
            takeover_result = "owner_mismatch"
            if await _should_take_over_stale_run_lock(
                client,
                agent_run_id,
                attempt_id=normalized_attempt_id,
                execution_epoch=normalized_execution_epoch,
            ):
                takeover_result = await _take_over_redis_run_lock(
                    agent_run_id,
                    expected_owner_token=existing_instance_str,
                    new_owner_token=run_lock_owner,
                )
                if takeover_result == "taken_over":
                    logger.info(
                        "Recovered attempt took over stale run lock for %s old_owner=%s new_owner=%s attempt_id=%s execution_epoch=%s",
                        agent_run_id,
                        existing_instance_str,
                        run_lock_owner,
                        normalized_attempt_id,
                        normalized_execution_epoch,
                    )
                    lock_acquired = True
                elif takeover_result == "missing":
                    try:
                        lock_acquired = await redis.set(
                            run_lock_key,
                            run_lock_owner,
                            nx=True,
                            ex=RUN_LOCK_TTL_SECONDS,
                        )
                    except Exception as redis_error:
                        logger.error(
                            "Redis post-takeover-missing lock operation failed, Error details: %s",
                            traceback.format_exc(),
                        )
                        raise redis_error
                    if lock_acquired:
                        logger.info(
                            "Recovered attempt acquired run lock after stale owner expired for %s new_owner=%s attempt_id=%s execution_epoch=%s",
                            agent_run_id,
                            run_lock_owner,
                            normalized_attempt_id,
                            normalized_execution_epoch,
                        )
            if not lock_acquired:
                logger.warning(
                    "Agent run %s is already being processed by instance %s. Skipping duplicate execution. takeover_result=%s attempt_id=%s execution_epoch=%s",
                    agent_run_id,
                    existing_instance_str,
                    takeover_result,
                    normalized_attempt_id,
                    normalized_execution_epoch,
                )
                return
        else:
            # 锁存在但无值，再次尝试获取
            # 第二次获取锁的意义 - 处理竞态条件
            # 时间线分析：
            # 1. Instance-A的锁即将过期 (TTL=1秒后过期)
            # 2. Instance-B尝试获取锁
            # 3. redis.set(nx=True) → 失败  (因为key还存在)
            # 4. Redis自动清理过期的key
            # 5. redis.get() → None (key已被清理)
            # 6. 第二次尝试 redis.set(nx=True) → 成功
            try:
                lock_acquired = await redis.set(
                    run_lock_key,
                    run_lock_owner,
                    nx=True,
                    ex=RUN_LOCK_TTL_SECONDS,
                )
            except Exception as redis_error:
                logger.error(
                    f"Redis second lock operation failed, Error details: {traceback.format_exc()}"
                )
                raise redis_error
            if not lock_acquired:
                logger.warning(
                    f"Agent run {agent_run_id} is already being processed by another instance. Skipping duplicate execution."
                )
                return
    else:
        logger.info(f"Successfully acquired run lock")

    try:
        # Sentry 是一个 应用监控和错误追踪平台，主要用于帮助开发者 实时发现、定位和分析应用中的错误和性能问题
        # Docs：https://docs.sentry.io/platforms/python/
        sentry.sentry.set_tag("thread_id", thread_id)
        logger.info(f"Sentry tag setting completed")
    except Exception as sentry_error:
        logger.error(f"Sentry tag setting failed, Error details: {sentry_error}")

    # 使用已解析的模型名
    effective_model = model_name  # 现在传入的已经是解析后的最终模型名
    effective_shadow_clone_mode, execution_origin = (
        _resolve_effective_shadow_clone_mode(
            shadow_clone_mode=shadow_clone_mode,
            agent_config=agent_config,
        )
    )
    logger.info(
        "Shadow clone mode resolved for run %s: requested=%s origin=%s effective=%s",
        agent_run_id,
        shadow_clone_mode,
        execution_origin,
        effective_shadow_clone_mode.value,
    )
    shadow_clone_runtime_model = (
        shadow_clone_main_model
        if effective_shadow_clone_mode != ShadowCloneMode.OFF
        and shadow_clone_main_model
        else effective_model
    )
    qwen_runtime_guards_enabled = _is_qwen_35_model(shadow_clone_runtime_model)
    if qwen_runtime_guards_enabled:
        logger.info(
            "Qwen runtime guards enabled from actor model name: %s",
            shadow_clone_runtime_model,
        )
    resolved_run_capacity_mode = run_capacity_mode or effective_shadow_clone_mode
    run_capacity_cost = get_run_capacity_cost(resolved_run_capacity_mode)
    run_capacity_lease_key = _build_run_capacity_lease_key(
        agent_run_id,
        attempt_id=normalized_attempt_id,
    )

    if client is None:
        try:
            client = await db.client
            logger.info(f"Database client acquisition successful")
        except Exception as db_error:
            logger.error(
                f"Database client acquisition failed, Error details: {db_error}"
            )
            raise db_error

    # 初始化时间、响应计数、Pub/Sub、停止信号检查器、待处理Redis操作
    start_time = datetime.now(timezone.utc)
    total_responses = 0
    pubsub = None
    stop_checker = None
    stop_signal_received = False
    external_stop_requested = False
    control_plane_stop_error: Optional[RuntimeError] = None
    last_ttl_refresh_at = 0.0
    last_run_lock_refresh_success_at = 0.0
    last_capacity_lease_refresh_success_at = 0.0
    run_capacity_lease_ttl_seconds = 0
    all_responses_json: list[Any] = []
    final_status = "running"

    # 定义 Redis keys 和 channels
    # 两层控制架构
    public_response_list_key = _build_response_list_key(agent_run_id)
    response_list_key = _build_response_list_key(
        agent_run_id,
        execution_epoch=normalized_execution_epoch,
    )
    compatibility_mirror_response_list_key = (
        None
        if response_list_key == public_response_list_key
        else public_response_list_key
    )
    response_channel = f"agent_run:{agent_run_id}:new_response"
    instance_control_channel = (
        f"agent_run:{agent_run_id}:control:{instance_id}"  # 精确控制
    )
    global_control_channel = f"agent_run:{agent_run_id}:control"  #  广播控制
    instance_active_key = f"active_run:{instance_id}:{agent_run_id}"
    project_active_key = _project_active_run_key(project_id)
    timeout_lease_task: Optional[asyncio.Task] = None
    timeout_lease_stop_event = asyncio.Event()
    run_capacity_lease_acquired = False
    terminal_write_fence_armed = False

    async def _activate_terminal_write_fence(
        *,
        reason: str,
    ) -> None:
        nonlocal terminal_write_fence_armed
        if terminal_write_fence_armed:
            return
        terminal_write_fence_armed = True
        await _cleanup_redis_instance_key(agent_run_id)
        clear_agent_run_context()
        logger.info(
            "Activated terminal write fence for run %s (reason=%s)",
            agent_run_id,
            reason,
        )

    async def _mirror_public_response_payload(payload_json: str) -> None:
        if compatibility_mirror_response_list_key is None:
            return
        try:
            await _mirror_response_without_notify_with_retry(
                compatibility_mirror_response_list_key,
                payload_json,
                agent_run_id=agent_run_id,
            )
        except RuntimeError as mirror_error:
            logger.warning(
                "Failed to mirror response payload for run %s to compatibility stream: %s",
                agent_run_id,
                mirror_error,
            )

    def _sync_external_control_plane_state() -> None:
        nonlocal stop_signal_received, external_stop_requested
        nonlocal control_plane_stop_error

        if control_plane_slot is None:
            return

        slot_error = getattr(control_plane_slot, "control_plane_error", None)
        if slot_error is not None and control_plane_stop_error is None:
            control_plane_stop_error = (
                slot_error
                if isinstance(slot_error, RuntimeError)
                else RuntimeError(str(slot_error))
            )
            stop_signal_received = True
            return

        if bool(getattr(control_plane_slot, "stop_requested", False)):
            external_stop_requested = True
            stop_signal_received = True

    async def _refresh_control_plane_state(now: float) -> None:
        nonlocal control_plane_stop_error, stop_signal_received
        nonlocal last_run_lock_refresh_success_at
        nonlocal last_capacity_lease_refresh_success_at
        nonlocal run_capacity_lease_ttl_seconds

        lock_refresh_result = await _refresh_redis_run_lock(
            agent_run_id,
            run_lock_owner,
        )
        if lock_refresh_result == "refreshed":
            last_run_lock_refresh_success_at = now
        elif lock_refresh_result == "error":
            if _control_plane_refresh_error_is_fatal(
                last_success_at=last_run_lock_refresh_success_at,
                now_monotonic=now,
                ttl_seconds=RUN_LOCK_TTL_SECONDS,
                refresh_interval_seconds=ACTIVE_RUN_KEY_REFRESH_INTERVAL_SECONDS,
            ):
                if external_stop_requested:
                    logger.warning(
                        "Ignoring fatal run lock refresh error after external STOP for %s owner=%s",
                        agent_run_id,
                        run_lock_owner,
                    )
                    return
                control_plane_stop_error = RuntimeError(
                    f"Run lock refresh exhausted TTL headroom for {agent_run_id}"
                )
                logger.error(
                    "Run lock refresh errors exhausted TTL headroom for %s "
                    "(ttl=%ss refresh_interval=%ss last_success_age=%.2fs)",
                    agent_run_id,
                    RUN_LOCK_TTL_SECONDS,
                    ACTIVE_RUN_KEY_REFRESH_INTERVAL_SECONDS,
                    max(0.0, now - last_run_lock_refresh_success_at),
                )
                stop_signal_received = True
                return
            logger.warning(
                "Temporarily tolerating run lock refresh error for %s within TTL headroom "
                "(ttl=%ss refresh_interval=%ss last_success_age=%.2fs)",
                agent_run_id,
                RUN_LOCK_TTL_SECONDS,
                ACTIVE_RUN_KEY_REFRESH_INTERVAL_SECONDS,
                max(0.0, now - last_run_lock_refresh_success_at),
            )
        else:
            if external_stop_requested:
                logger.warning(
                    "Ignoring run lock refresh failure after external STOP for %s owner=%s reason=%s",
                    agent_run_id,
                    run_lock_owner,
                    lock_refresh_result,
                )
                return
            control_plane_stop_error = RuntimeError(
                f"Lost Redis run lock for {agent_run_id}: {lock_refresh_result}"
            )
            logger.error(
                "Run lock refresh failed for %s owner=%s reason=%s",
                agent_run_id,
                run_lock_owner,
                lock_refresh_result,
            )
            stop_signal_received = True
            return

        try:
            await redis.expire(instance_active_key, redis.REDIS_KEY_TTL)
            await _set_project_active_run_key(
                project_id=project_id,
                agent_run_id=agent_run_id,
                instance_id=instance_id,
                status="running",
            )
            if run_capacity_lease_acquired:
                try:
                    capacity_refresh = await refresh_run_capacity_lease(
                        agent_run_id=run_capacity_lease_key,
                        mode=resolved_run_capacity_mode,
                        owner_token=run_lock_owner,
                    )
                    resolved_capacity_lease_ttl = int(
                        capacity_refresh.get("lease_ttl_seconds")
                        or run_capacity_lease_ttl_seconds
                        or 0
                    )
                    if not bool(capacity_refresh.get("acquired")):
                        lease_loss_reason = (
                            "ownership"
                            if bool(capacity_refresh.get("owner_conflict"))
                            else "admission"
                        )
                        if external_stop_requested:
                            logger.warning(
                                "Ignoring run capacity lease %s loss after external STOP for %s "
                                "(budget=%s in_use=%s requested_cost=%s)",
                                lease_loss_reason,
                                agent_run_id,
                                capacity_refresh.get("budget"),
                                capacity_refresh.get("in_use"),
                                capacity_refresh.get("requested_cost"),
                            )
                            return
                        control_plane_stop_error = RuntimeError(
                            f"Lost run capacity lease {lease_loss_reason} for "
                            f"{agent_run_id}: budget={capacity_refresh.get('budget')} "
                            f"in_use={capacity_refresh.get('in_use')} "
                            f"requested_cost={capacity_refresh.get('requested_cost')}"
                        )
                        logger.warning(
                            "Run capacity lease refresh lost %s for run %s "
                            "(budget=%s in_use=%s requested_cost=%s)",
                            lease_loss_reason,
                            agent_run_id,
                            capacity_refresh.get("budget"),
                            capacity_refresh.get("in_use"),
                            capacity_refresh.get("requested_cost"),
                        )
                        stop_signal_received = True
                        return
                    if resolved_capacity_lease_ttl > 0:
                        run_capacity_lease_ttl_seconds = resolved_capacity_lease_ttl
                    last_capacity_lease_refresh_success_at = now
                except Exception as capacity_refresh_error:
                    if external_stop_requested:
                        logger.warning(
                            "Ignoring run capacity lease refresh failure after external STOP for %s: %s",
                            agent_run_id,
                            capacity_refresh_error,
                        )
                        return
                    if _control_plane_refresh_error_is_fatal(
                        last_success_at=last_capacity_lease_refresh_success_at,
                        now_monotonic=now,
                        ttl_seconds=run_capacity_lease_ttl_seconds,
                        refresh_interval_seconds=ACTIVE_RUN_KEY_REFRESH_INTERVAL_SECONDS,
                    ):
                        control_plane_stop_error = RuntimeError(
                            "Run capacity lease refresh exhausted TTL headroom for "
                            f"{agent_run_id}: {capacity_refresh_error}"
                        )
                        logger.warning(
                            "Run capacity lease refresh errors exhausted TTL headroom for %s "
                            "(ttl=%ss refresh_interval=%ss last_success_age=%.2fs): %s",
                            agent_run_id,
                            run_capacity_lease_ttl_seconds,
                            ACTIVE_RUN_KEY_REFRESH_INTERVAL_SECONDS,
                            max(0.0, now - last_capacity_lease_refresh_success_at),
                            capacity_refresh_error,
                        )
                        stop_signal_received = True
                        return
                    logger.warning(
                        "Temporarily tolerating run capacity lease refresh error for %s "
                        "within TTL headroom (ttl=%ss refresh_interval=%ss last_success_age=%.2fs): %s",
                        agent_run_id,
                        run_capacity_lease_ttl_seconds,
                        ACTIVE_RUN_KEY_REFRESH_INTERVAL_SECONDS,
                        max(0.0, now - last_capacity_lease_refresh_success_at),
                        capacity_refresh_error,
                    )
        except Exception as ttl_err:
            logger.warning(
                "Failed to refresh active run keys for %s/%s: %s",
                instance_active_key,
                project_active_key,
                ttl_err,
            )

    # 用户在前端点击"停止"按钮
    # 前端 → 后端API → Redis发布 "STOP" 消息
    # → instance_control_channel 或 global_control_channel
    # → 正在运行的Agent接收到信号 → 立即停止执行

    async def check_for_stop_signal():
        """
        实时监听Redis中的STOP信号
        """
        nonlocal stop_signal_received, external_stop_requested
        nonlocal last_ttl_refresh_at, control_plane_stop_error
        nonlocal last_run_lock_refresh_success_at
        nonlocal last_capacity_lease_refresh_success_at
        nonlocal run_capacity_lease_ttl_seconds

        if not pubsub:
            logger.warning(f"PubSub not initialized, exiting checker")
            return
        try:
            while True:
                message = await pubsub.get_message(
                    ignore_subscribe_messages=True, timeout=0.5
                )
                if message and message.get("type") == "message":
                    data = message.get("data")
                    if isinstance(data, bytes):
                        data = data.decode("utf-8")
                    if data == "STOP":
                        logger.info(
                            f"Received STOP signal for agent run {agent_run_id} (Instance: {instance_id})"
                        )
                        external_stop_requested = True
                        stop_signal_received = True
                # 按时间窗口刷新活跃运行键，避免响应稀疏场景下的频繁请求。
                now = time.monotonic()
                if now - last_ttl_refresh_at >= ACTIVE_RUN_KEY_REFRESH_INTERVAL_SECONDS:
                    last_ttl_refresh_at = now
                    await _refresh_control_plane_state(now)
                    if stop_signal_received:
                        break
                await asyncio.sleep(0.1)  # Short sleep to prevent tight loop
        except asyncio.CancelledError:
            logger.info(
                f"Stop signal checker cancelled for {agent_run_id} (Instance: {instance_id})"
            )
        except Exception as e:
            logger.error(
                f"Error in stop signal checker for {agent_run_id}: {e}", exc_info=True
            )
            if external_stop_requested:
                return
            control_plane_stop_error = RuntimeError(
                f"Stop signal checker failed for {agent_run_id}: {e}"
            )
            stop_signal_received = True  # Stop the run if the checker fails

    # 创建 Langfuse 跟踪
    trace_metadata = {
        "project_id": project_id,
        "instance_id": instance_id,
        "thread_id": thread_id,
        "model_name": effective_model,
        "resume_strategy": resume_strategy,
        "shadow_clone_mode": effective_shadow_clone_mode.value,
        "execution_origin": execution_origin,
        "regular_mode": True,
    }
    if normalized_attempt_id:
        trace_metadata["eval_attempt_id"] = normalized_attempt_id
    if normalized_execution_epoch is not None:
        trace_metadata["execution_epoch"] = normalized_execution_epoch
    _eval_task_id_for_tags: str = ""
    if isinstance(run_metadata, dict):
        eval_task_id = str(run_metadata.get("eval_task_id") or "").strip()
        if eval_task_id:
            trace_metadata["eval_task_id"] = eval_task_id
            _eval_task_id_for_tags = eval_task_id
        deliverable_quality = run_metadata.get("deliverable_quality")
        if deliverable_quality is not None:
            trace_metadata["deliverable_quality"] = deliverable_quality
    _model_family = (
        effective_model.split("/")[0] if "/" in effective_model else effective_model
    )
    trace_tags = [
        f"origin:{execution_origin}",
        f"shadow_clone:{effective_shadow_clone_mode.value}",
        f"model:{_model_family}",
    ]
    if _eval_task_id_for_tags:
        trace_tags.append("is_eval_run")
        trace_tags.append(f"eval_task:{_eval_task_id_for_tags}")
    try:
        lf_trace_id = generate_trace_id(agent_run_id)
    except Exception as trace_id_error:
        logger.warning(
            "Langfuse trace id generation failed for run %s: %s",
            agent_run_id,
            trace_id_error,
        )
        lf_trace_id = hashlib.md5(
            str(agent_run_id).encode("utf-8"), usedforsecurity=False
        ).hexdigest()

    trace = None
    try:
        trace = start_root_span(name="agent_run", trace_id=lf_trace_id)
        trace.update_trace(
            session_id=thread_id,
            metadata=trace_metadata,
            tags=trace_tags,
        )
    except Exception as trace_bootstrap_error:
        logger.warning(
            "Langfuse tracing bootstrap failed for run %s: %s",
            agent_run_id,
            trace_bootstrap_error,
        )
        trace = None
    else:
        logger.info(f"Langfuse trace created successfully")
    start_eval_run(run_id=agent_run_id, trace_id=lf_trace_id, metadata=trace_metadata)
    update_eval_run_metadata(
        agent_run_id,
        model_name=effective_model,
        execution_origin=execution_origin,
        shadow_clone_mode=effective_shadow_clone_mode.value,
        resume_strategy=resume_strategy,
        attempt_id=normalized_attempt_id,
        execution_epoch=normalized_execution_epoch,
        eval_task_id=(
            str((run_metadata or {}).get("eval_task_id") or "").strip() or None
        ),
        eval_attempt_index=(run_metadata or {}).get("eval_attempt_index"),
    )
    max_generated_responses = _resolve_max_generated_responses(run_metadata)
    agent_gen: Optional[Any] = None

    try:
        if is_server_concurrency_budget_enabled():
            try:
                capacity_acquire_result = await try_acquire_run_capacity_lease(
                    agent_run_id=run_capacity_lease_key,
                    mode=resolved_run_capacity_mode,
                    owner_token=run_lock_owner,
                    allow_takeover=True,
                )
            except Exception as capacity_error:
                raise RuntimeError(
                    f"Run capacity guard unavailable for {agent_run_id}: {capacity_error}"
                ) from capacity_error
            else:
                run_capacity_lease_acquired = bool(
                    capacity_acquire_result.get("acquired")
                )
                if not run_capacity_lease_acquired:
                    capacity_detail = build_run_capacity_limit_detail(
                        mode=resolved_run_capacity_mode,
                        snapshot=capacity_acquire_result,
                    )
                    raise RuntimeError(
                        f"{capacity_detail['message']} "
                        f"(capacity_kind={capacity_detail['capacity_kind']} "
                        f"budget={capacity_detail['budget']} "
                        f"in_use={capacity_detail['in_use']} "
                        f"requested_cost={capacity_detail['requested_cost']})"
                    )
                if bool(capacity_acquire_result.get("taken_over")):
                    logger.warning(
                        "Run %s safely took over an existing capacity lease after acquiring the run lock",
                        agent_run_id,
                    )
                run_capacity_lease_ttl_seconds = int(
                    capacity_acquire_result.get("lease_ttl_seconds") or 0
                )
                last_capacity_lease_refresh_success_at = time.monotonic()

        try:
            current_run_result = (
                await client.table("agent_runs")
                .select("status,error")
                .eq(
                    "agent_run_id",
                    agent_run_id,
                )
                .execute()
            )
        except Exception as run_state_error:
            logger.warning(
                "Failed to read current run state before execution start for %s: %s",
                agent_run_id,
                run_state_error,
            )
        else:
            current_run_row = (
                current_run_result.data[0] if current_run_result.data else {}
            )
            persisted_status = str(current_run_row.get("status") or "").strip().lower()
            if persisted_status in {"completed", "failed", "stopped", "error"}:
                resolved_terminal_status = (
                    "failed" if persisted_status == "error" else persisted_status
                )
                final_status = _merge_terminal_status(
                    final_status,
                    resolved_terminal_status,
                    error_present=bool(current_run_row.get("error")),
                )
                external_stop_requested = resolved_terminal_status == "stopped"
                stop_signal_received = external_stop_requested
                logger.info(
                    "Skipping agent execution because run %s is already terminal before start (status=%s)",
                    agent_run_id,
                    persisted_status,
                )
                return

        # 处理前端终止操作
        # 前端用户: 点击"停止Agent"按钮
        #          ↓
        # 后端API: 接收停止请求
        #          ↓
        # Redis: 发布"STOP"信号到control_channel
        #          ↓
        # 后端Agent: 订阅并接收到"STOP"信号
        #          ↓
        # Agent: 优雅停止执行，清理资源

        if control_plane_slot is None:
            # 创建 Pub/Sub 连接
            pubsub = await redis.create_pubsub()
            logger.info(f"PubSub connection created successfully")
            try:
                # 订阅控制频道
                await retry(
                    lambda: pubsub.subscribe(
                        instance_control_channel, global_control_channel
                    )
                )
                logger.info(f"Control channels subscribed successfully")
            except Exception as e:
                logger.error(
                    f"Redis failed to subscribe to control channels: {e}", exc_info=True
                )
                raise e

            logger.debug(
                f"Subscribed to control channels: {instance_control_channel}, {global_control_channel}"
            )
            stop_checker = asyncio.create_task(check_for_stop_signal())
            logger.info(f"Stop signal checker started successfully")
        else:
            logger.info(
                "Using external regular supervisor control plane for run %s",
                agent_run_id,
            )
        # 确保活跃运行键存在并设置TTL
        await redis.set(instance_active_key, "running", ex=redis.REDIS_KEY_TTL)
        await _set_project_active_run_key(
            project_id=project_id,
            agent_run_id=agent_run_id,
            instance_id=instance_id,
            status="running",
        )
        initial_control_plane_heartbeat_at = time.monotonic()
        last_ttl_refresh_at = initial_control_plane_heartbeat_at
        last_run_lock_refresh_success_at = initial_control_plane_heartbeat_at
        if (
            run_capacity_lease_acquired
            and last_capacity_lease_refresh_success_at <= 0.0
        ):
            last_capacity_lease_refresh_success_at = initial_control_plane_heartbeat_at
        logger.info(f"Active run key set successfully")
        if control_plane_slot is not None and hasattr(
            control_plane_slot,
            "set_refresh_callback",
        ):

            async def _external_control_plane_refresh(_slot) -> None:
                nonlocal last_ttl_refresh_at
                _sync_external_control_plane_state()
                if stop_signal_received:
                    return
                last_ttl_refresh_at = time.monotonic()
                await _refresh_control_plane_state(last_ttl_refresh_at)

            control_plane_slot.set_refresh_callback(_external_control_plane_refresh)

        stub_config = regular_load_harness_stub.resolve_stub_config(run_metadata)
        effective_user_message_override = (
            str(
                user_message_override
                or _resolve_regular_harness_user_message_override(run_metadata or {})
                or ""
            ).strip()
            or None
        )
        real_harness_hold_config = _resolve_regular_harness_real_profile_hold_config(
            run_metadata,
            stub_config=stub_config,
        )
        real_harness_started_at = (
            time.monotonic() if real_harness_hold_config is not None else None
        )

        if stub_config is None and SANDBOX_SET_TIMEOUT_ENABLED and project_id:
            timeout_lease_task = asyncio.create_task(
                _sandbox_timeout_lease_loop(
                    project_id=project_id,
                    agent_run_id=agent_run_id,
                    stop_event=timeout_lease_stop_event,
                ),
                name=f"sandbox-timeout-lease:{project_id}:{agent_run_id}",
            )

        def _create_agent_generator(
            *,
            user_message_override: Optional[str] = None,
        ) -> Any:
            return run_agent(
                thread_id=thread_id,
                project_id=project_id,
                stream=stream,
                native_max_auto_continues=0,
                model_name=effective_model,
                enable_thinking=enable_thinking,
                reasoning_effort=reasoning_effort,
                enable_context_manager=enable_context_manager,
                agent_config=agent_config,
                trace=trace,
                is_agent_builder=is_agent_builder,
                target_agent_id=target_agent_id,
                resume_strategy=resume_strategy,
                resume_window_minutes=resume_window_minutes,
                user_message_override=user_message_override,
                agent_run_id=agent_run_id,
                shadow_clone_mode=effective_shadow_clone_mode,
                shadow_clone_main_model=shadow_clone_main_model,
                shadow_clone_subagent_model=shadow_clone_subagent_model,
            )

        # 初始化Agent生成器
        try:
            # 这里开始执行Agent的逻辑。注意：这里仅仅是创建生成器，并不执行
            if stub_config is not None:
                agent_gen = regular_load_harness_stub.iter_stub_responses(
                    config=stub_config,
                    project_id=project_id,
                    thread_id=thread_id,
                    agent_run_id=agent_run_id,
                    client=client,
                    persist_artifact=workspace_artifacts.persist_artifact,
                )
            else:
                agent_gen = _create_agent_generator(
                    user_message_override=effective_user_message_override
                )
            logger.info(f"Agent run {agent_run_id} started successfully")
        except Exception as agent_error:
            logger.error(f"Failed to call run_agent: {agent_error}")
            raise agent_error

        error_message = None
        response_count = 0
        consecutive_sandbox_not_found_failures = 0
        qwen_write_repair_attempts = 0
        qwen_write_arg_guard_state: Dict[str, Any] = {}
        qwen_write_chunk_emit_state: Dict[str, Dict[str, float]] = {}
        pending_response_task: Optional[asyncio.Task] = None
        last_response_monotonic = time.monotonic()
        saw_error_status_message = False
        qwen_timeout_grace_remaining = int(QWEN_TIMEOUT_GRACE_CYCLES)

        def _set_final_status(candidate_status: str) -> None:
            nonlocal final_status
            final_status = _merge_terminal_status(
                final_status,
                candidate_status,
                error_present=bool(error_message),
            )

        async def _cancel_pending_response_task() -> None:
            nonlocal pending_response_task
            if pending_response_task is None:
                return
            if not pending_response_task.done():
                pending_response_task.cancel()
            try:
                await pending_response_task
            except (asyncio.CancelledError, StopAsyncIteration):
                pass
            except Exception as pending_error:
                logger.debug(
                    "Ignoring pending response task cancellation error for run %s: %s",
                    agent_run_id,
                    pending_error,
                )
            finally:
                pending_response_task = None

        async def _hold_real_harness_completion_until_active_window_elapsed() -> None:
            nonlocal final_status, error_message, last_ttl_refresh_at

            if final_status != "completed":
                return
            if real_harness_hold_config is None or real_harness_started_at is None:
                return

            active_duration_seconds = float(
                real_harness_hold_config["active_duration_seconds"]
            )
            chunk_interval_seconds = float(
                real_harness_hold_config["chunk_interval_seconds"]
            )

            while True:
                _sync_external_control_plane_state()
                if stop_signal_received:
                    if control_plane_stop_error is not None:
                        raise control_plane_stop_error
                    if not external_stop_requested:
                        raise RuntimeError(
                            "Regular harness completion hold stopped without an external STOP signal."
                        )
                    _set_final_status("stopped")
                    error_message = None
                    return

                now = time.monotonic()
                remaining_seconds = active_duration_seconds - max(
                    0.0,
                    now - real_harness_started_at,
                )
                if remaining_seconds <= 0:
                    return

                if (
                    control_plane_slot is not None
                    and now - last_ttl_refresh_at
                    >= ACTIVE_RUN_KEY_REFRESH_INTERVAL_SECONDS
                ):
                    last_ttl_refresh_at = now
                    await _refresh_control_plane_state(now)
                    continue

                await asyncio.sleep(min(chunk_interval_seconds, remaining_seconds))

        async def _restart_qwen_write_after_path_only_loop(
            *,
            trigger_reason: str,
            trigger_now_monotonic: float,
        ) -> tuple[bool, Optional[str]]:
            nonlocal agent_gen, qwen_write_repair_attempts, last_response_monotonic

            if not _should_attempt_qwen_write_path_only_retry(
                qwen_write_arg_guard_state,
                now_monotonic=trigger_now_monotonic,
                attempts_made=qwen_write_repair_attempts,
            ):
                return False, None

            path_value = _resolve_qwen_write_retry_path_hint(qwen_write_arg_guard_state)
            path_hint_value = qwen_write_arg_guard_state.get("path_hint")
            if isinstance(path_hint_value, str):
                path_hint_value = path_hint_value.strip() or None
            else:
                path_hint_value = None

            qwen_write_repair_attempts += 1
            repair_instruction = _build_qwen_write_path_only_retry_instruction(
                path=path_value,
                attempt=qwen_write_repair_attempts,
                path_hint=path_hint_value,
            )
            logger.warning(
                "Qwen write_file contentless loop detected for run %s; starting repair retry (%s/%s) path=%s path_hint=%s trigger=%s",
                agent_run_id,
                qwen_write_repair_attempts,
                QWEN_WRITE_AUTO_REPAIR_MAX_RETRIES,
                path_value,
                path_hint_value,
                trigger_reason,
            )

            await _cancel_pending_response_task()
            await _close_agent_generator(agent_gen, agent_run_id=agent_run_id)

            try:
                agent_gen = _create_agent_generator(
                    user_message_override=repair_instruction,
                )
            except Exception as restart_error:
                return False, f"failed to start repair retry generator: {restart_error}"

            _reset_qwen_write_arg_guard_state(qwen_write_arg_guard_state)
            qwen_write_chunk_emit_state.clear()
            last_response_monotonic = time.monotonic()
            return True, None

        # 从这里开始真正执行：runner.run()
        while True:
            _sync_external_control_plane_state()
            qwen_runtime_guards_enabled = _maybe_enable_qwen_runtime_guards(
                qwen_runtime_guards_enabled,
                agent_run_id=agent_run_id,
            )

            # 检查是否收到STOP信号
            if stop_signal_received:
                if control_plane_stop_error is not None:
                    raise control_plane_stop_error
                if not external_stop_requested:
                    raise RuntimeError(
                        f"Run {agent_run_id} stopped without an external STOP signal."
                    )
                logger.info(f"Agent run {agent_run_id} stopped by signal.")
                _set_final_status("stopped")
                try:
                    if trace is not None:
                        trace.create_event(
                            name="agent_run_stopped",
                            status_message="agent_run_stopped",
                            level="WARNING",
                        )
                except Exception as trace_error:
                    logger.warning(
                        f"Failed to record trace for agent run {agent_run_id}: {trace_error}"
                    )
                await _cancel_pending_response_task()
                break

            if qwen_runtime_guards_enabled:
                if pending_response_task is None:
                    pending_response_task = asyncio.create_task(anext(agent_gen))
                try:
                    response = await asyncio.wait_for(
                        asyncio.shield(pending_response_task),
                        timeout=QWEN_STREAM_POLL_SECONDS,
                    )
                    pending_response_task = None
                except asyncio.TimeoutError:
                    idle_reason = _check_qwen_write_idle_guard(
                        qwen_write_arg_guard_state,
                        now_monotonic=time.monotonic(),
                        last_response_monotonic=last_response_monotonic,
                    )
                    if idle_reason:
                        if qwen_timeout_grace_remaining > 0:
                            qwen_timeout_grace_remaining -= 1
                            last_response_monotonic = time.monotonic()
                            logger.warning(
                                "Qwen write_file idle guard grace used for run %s (remaining=%s): %s",
                                agent_run_id,
                                qwen_timeout_grace_remaining,
                                idle_reason,
                            )
                            continue

                        idle_checked_at = time.monotonic()
                        retry_started, retry_error = (
                            await _restart_qwen_write_after_path_only_loop(
                                trigger_reason=idle_reason,
                                trigger_now_monotonic=idle_checked_at,
                            )
                        )
                        if retry_started:
                            continue

                        path_only_loop_active = _is_qwen_write_path_only_loop(
                            qwen_write_arg_guard_state,
                            now_monotonic=idle_checked_at,
                        )
                        contentless_loop_active = _is_qwen_write_contentless_loop(
                            qwen_write_arg_guard_state,
                            now_monotonic=idle_checked_at,
                        )
                        short_args_loop_active = _is_qwen_write_short_args_loop(
                            qwen_write_arg_guard_state,
                            now_monotonic=idle_checked_at,
                        )
                        forced_completion = False
                        forced_error: Optional[str] = None
                        if (
                            not path_only_loop_active
                            and not contentless_loop_active
                            and not short_args_loop_active
                        ):
                            forced_completion, forced_error = (
                                await _force_qwen_write_completion(
                                    project_id=project_id,
                                    state=qwen_write_arg_guard_state,
                                    agent_run_id=agent_run_id,
                                    trigger_reason=idle_reason,
                                    shadow_clone_run_id=(
                                        agent_run_id
                                        if effective_shadow_clone_mode
                                        == ShadowCloneMode.ON
                                        else None
                                    ),
                                    strict_sandbox=effective_shadow_clone_mode
                                    == ShadowCloneMode.ON,
                                )
                            )
                        if forced_completion:
                            _set_final_status("completed")
                            await _cancel_pending_response_task()
                            break
                        error_message = f"{idle_reason} Run aborted to avoid prolonged write_file pre-execution loop."
                        if retry_error:
                            error_message = f"{error_message} Path-only retry failed: {retry_error}."
                        if path_only_loop_active:
                            error_message = f"{error_message} Path-only payload detected during idle timeout."
                        elif short_args_loop_active:
                            error_message = f"{error_message} Unreconstructable short-args write payload loop detected."
                        elif contentless_loop_active:
                            error_message = f"{error_message} Content-less write_file payload detected during idle timeout."
                        if (
                            contentless_loop_active
                            and qwen_write_repair_attempts
                            >= QWEN_WRITE_AUTO_REPAIR_MAX_RETRIES
                        ):
                            error_message = (
                                f"{error_message} Path-only retry exhausted "
                                f"({qwen_write_repair_attempts}/{QWEN_WRITE_AUTO_REPAIR_MAX_RETRIES})."
                            )
                        if forced_error:
                            error_message = f"{error_message} Forced completion failed: {forced_error}"
                        _set_final_status("failed")
                        logger.error(
                            "Qwen write_file idle guard triggered for run %s: %s",
                            agent_run_id,
                            error_message,
                        )
                        await _cancel_pending_response_task()
                        break
                    continue
                except StopAsyncIteration:
                    pending_response_task = None
                    break
                except Exception:
                    pending_response_task = None
                    raise
            else:
                try:
                    if pending_response_task is None:
                        pending_response_task = asyncio.create_task(anext(agent_gen))
                    response = await asyncio.wait_for(
                        asyncio.shield(pending_response_task),
                        timeout=NON_QWEN_RESPONSE_POLL_SECONDS,
                    )
                    pending_response_task = None
                except asyncio.TimeoutError:
                    now = time.monotonic()
                    _sync_external_control_plane_state()
                    if (
                        control_plane_slot is not None
                        and now - last_ttl_refresh_at
                        >= ACTIVE_RUN_KEY_REFRESH_INTERVAL_SECONDS
                    ):
                        last_ttl_refresh_at = now
                        await _refresh_control_plane_state(now)
                    continue
                except StopAsyncIteration:
                    pending_response_task = None
                    break

            response_count += 1
            last_response_monotonic = time.monotonic()

            if max_generated_responses and response_count > max_generated_responses:
                error_message = (
                    "Agent run exceeded max generated response count "
                    f"({max_generated_responses})."
                )
                logger.error(
                    "%s agent_run_id=%s redis_responses=%s",
                    error_message,
                    agent_run_id,
                    total_responses,
                )
                _set_final_status("failed")
                break

            if MAX_REDIS_RESPONSES and total_responses >= MAX_REDIS_RESPONSES:
                error_message = (
                    f"Agent run exceeded max response count ({MAX_REDIS_RESPONSES})."
                )
                logger.error(error_message)
                _set_final_status("failed")
                break

            if qwen_runtime_guards_enabled:
                guard_checked_at = time.monotonic()
                guard_reason = _check_qwen_write_arg_guard(
                    response,
                    qwen_write_arg_guard_state,
                    now_monotonic=guard_checked_at,
                )
                if guard_reason:
                    if qwen_timeout_grace_remaining > 0:
                        qwen_timeout_grace_remaining -= 1
                        last_response_monotonic = time.monotonic()
                        logger.warning(
                            "Qwen write_file pre-execution guard grace used for run %s (remaining=%s): %s",
                            agent_run_id,
                            qwen_timeout_grace_remaining,
                            guard_reason,
                        )
                        continue

                    retry_started, retry_error = (
                        await _restart_qwen_write_after_path_only_loop(
                            trigger_reason=guard_reason,
                            trigger_now_monotonic=guard_checked_at,
                        )
                    )
                    if retry_started:
                        continue

                    path_only_loop_active = _is_qwen_write_path_only_loop(
                        qwen_write_arg_guard_state,
                        now_monotonic=guard_checked_at,
                    )
                    contentless_loop_active = _is_qwen_write_contentless_loop(
                        qwen_write_arg_guard_state,
                        now_monotonic=guard_checked_at,
                    )
                    short_args_loop_active = _is_qwen_write_short_args_loop(
                        qwen_write_arg_guard_state,
                        now_monotonic=guard_checked_at,
                    )
                    forced_completion = False
                    forced_error: Optional[str] = None
                    if (
                        not path_only_loop_active
                        and not contentless_loop_active
                        and not short_args_loop_active
                    ):
                        forced_completion, forced_error = (
                            await _force_qwen_write_completion(
                                project_id=project_id,
                                state=qwen_write_arg_guard_state,
                                agent_run_id=agent_run_id,
                                trigger_reason=guard_reason,
                                shadow_clone_run_id=(
                                    agent_run_id
                                    if effective_shadow_clone_mode == ShadowCloneMode.ON
                                    else None
                                ),
                                strict_sandbox=effective_shadow_clone_mode
                                == ShadowCloneMode.ON,
                            )
                        )
                    if forced_completion:
                        _set_final_status("completed")
                        break

                    error_message = f"{guard_reason} Run aborted to avoid prolonged write_file pre-execution loop."
                    if retry_error:
                        error_message = (
                            f"{error_message} Path-only retry failed: {retry_error}."
                        )
                    if (
                        contentless_loop_active
                        and qwen_write_repair_attempts
                        >= QWEN_WRITE_AUTO_REPAIR_MAX_RETRIES
                    ):
                        error_message = (
                            f"{error_message} Path-only retry exhausted "
                            f"({qwen_write_repair_attempts}/{QWEN_WRITE_AUTO_REPAIR_MAX_RETRIES})."
                        )
                    if path_only_loop_active:
                        error_message = f"{error_message} Path-only payload detected."
                    elif short_args_loop_active:
                        error_message = f"{error_message} Unreconstructable short-args write payload loop detected."
                    elif contentless_loop_active:
                        error_message = (
                            f"{error_message} Content-less write_file payload detected."
                        )
                    if forced_error:
                        error_message = (
                            f"{error_message} Forced completion failed: {forced_error}"
                        )
                    _set_final_status("failed")
                    logger.error(
                        "Qwen write_file pre-execution guard triggered for run %s: %s",
                        agent_run_id,
                        error_message,
                    )
                    break

                if _response_contains_sandbox_not_found(response):
                    consecutive_sandbox_not_found_failures += 1
                    logger.warning(
                        "Qwen sandbox failure streak %s/%s for run %s",
                        consecutive_sandbox_not_found_failures,
                        QWEN_SANDBOX_FAILURE_THRESHOLD,
                        agent_run_id,
                    )
                    if (
                        consecutive_sandbox_not_found_failures
                        >= QWEN_SANDBOX_FAILURE_THRESHOLD
                    ):
                        error_message = (
                            "Sandbox became unavailable repeatedly "
                            f"({QWEN_SANDBOX_FAILURE_THRESHOLD} consecutive failures); "
                            "run aborted to prevent an infinite tool loop."
                        )
                        _set_final_status("failed")
                        logger.error(
                            "Qwen circuit breaker triggered for run %s: %s",
                            agent_run_id,
                            error_message,
                        )
                        break
                elif consecutive_sandbox_not_found_failures:
                    consecutive_sandbox_not_found_failures = 0

            if qwen_runtime_guards_enabled:
                should_emit_response = _should_emit_qwen_write_chunk(
                    response,
                    qwen_write_chunk_emit_state,
                    now_monotonic=time.monotonic(),
                )
                if not should_emit_response:
                    continue

            # 存储响应到Redis列表并发布通知
            response_json = json.dumps(response, ensure_ascii=False)
            response_size = len(response_json.encode("utf-8"))
            if MAX_RESPONSE_BYTES and response_size > MAX_RESPONSE_BYTES:
                error_message = (
                    f"Agent response too large ({response_size} bytes > "
                    f"{MAX_RESPONSE_BYTES} bytes)."
                )
                logger.error(error_message)
                _set_final_status("failed")
                break
            all_responses_json.append(response_json)
            if _is_shadow_clone_v2_user_facing_main_agent_response(
                response,
                effective_shadow_clone_mode=effective_shadow_clone_mode,
            ):
                try:
                    await _persist_shadow_clone_v2_main_agent_assistant_message(
                        client,
                        response,
                    )
                except Exception as persist_error:
                    logger.warning(
                        "Failed to persist Shadow Clone V2 main-agent assistant message "
                        "for run %s: %s",
                        agent_run_id,
                        persist_error,
                    )
            # Redis List 就像一个双端队列
            # [左端/头部] ← 元素1 - 元素2 - 元素3 → [右端/尾部]
            #    ↑                                    ↑
            # lpush                               rpush
            # (左入栈)                            (右入栈)
            try:
                response_pushed = await _push_response_with_retry(
                    response_list_key,
                    response_channel,
                    response_json,
                    agent_run_id=agent_run_id,
                )
            except RuntimeError as push_error:
                logger.error(
                    "Response push failed for run %s but continuing execution: %s",
                    agent_run_id,
                    push_error,
                )
                response_pushed = False
            if not response_pushed:
                logger.warning(
                    "Response push failed but continuing execution for run %s",
                    agent_run_id,
                )
            else:
                await _mirror_public_response_payload(response_json)
            total_responses += 1

            if total_responses % 10 == 1:  # 每10个响应打印一次进度
                logger.info(
                    f"Agent run {agent_run_id} has processed {total_responses} responses"
                )

            # 检查是否收到Agent信号完成或错误
            if response.get("type") == "status":
                status_val = response.get("status")
                if status_val in ["completed", "failed", "stopped", "error"]:
                    normalized_status = _normalize_terminal_status(status_val)
                    logger.info(
                        "Agent run %s finished via status message: raw=%s normalized=%s",
                        agent_run_id,
                        status_val,
                        normalized_status,
                    )
                    if normalized_status in {"failed", "stopped"}:
                        error_message = response.get(
                            "message", f"Run ended with status: {status_val}"
                        )
                    if status_val == "error":
                        saw_error_status_message = True
                    _set_final_status(normalized_status)
                    break

        await _cancel_pending_response_task()
        await _close_agent_generator(agent_gen, agent_run_id=agent_run_id)
        await _hold_real_harness_completion_until_active_window_elapsed()

        candidate_final_status = _finalize_local_terminal_status(
            final_status,
            external_stop_requested=external_stop_requested,
            control_plane_failure=control_plane_stop_error is not None,
            error_message=error_message,
            saw_error_status_message=saw_error_status_message,
        )
        candidate_final_status = await _merge_with_persisted_agent_run_status(
            client,
            agent_run_id,
            candidate_final_status,
            error_present=bool(error_message),
        )

        await _activate_terminal_write_fence(
            reason=f"normal_exit:{candidate_final_status}"
        )
        final_status = await _persist_and_resolve_final_agent_run_status(
            client,
            agent_run_id,
            candidate_final_status,
            error_message=error_message,
            attempt_id=normalized_attempt_id,
            execution_epoch=normalized_execution_epoch,
        )

        if final_status == "completed":
            duration = (datetime.now(timezone.utc) - start_time).total_seconds()
            logger.info(
                f"Agent run {agent_run_id} completed normally (duration: {duration:.2f}s, responses: {total_responses})"
            )
            completion_message = {
                "type": "status",
                "status": "completed",
                "message": "Agent run completed successfully",
            }
            completion_message_json = json.dumps(completion_message, ensure_ascii=False)
            all_responses_json.append(completion_message_json)
            if trace is not None:
                trace.create_event(
                    name="agent_run_completed",
                    status_message="agent_run_completed",
                )
            try:
                completion_pushed = await _push_response_with_retry(
                    response_list_key,
                    response_channel,
                    completion_message_json,
                    agent_run_id=agent_run_id,
                )
            except RuntimeError as push_error:
                logger.error(
                    "Completion response push failed for run %s: %s",
                    agent_run_id,
                    push_error,
                )
                completion_pushed = False
            if not completion_pushed:
                logger.warning(
                    "Completion response push failed for run %s; final status will still be recorded",
                    agent_run_id,
                )
            else:
                await _mirror_public_response_payload(completion_message_json)

        logger.info(
            "Emitting terminal status for run %s (status=%s error_present=%s)",
            agent_run_id,
            final_status,
            bool(error_message),
        )
        write_compensation_json = await _append_missing_write_file_terminal_status(
            response_list_key=response_list_key,
            response_channel=response_channel,
            agent_run_id=agent_run_id,
            thread_id=thread_id,
            final_status=final_status,
            enabled=qwen_runtime_guards_enabled,
        )
        if write_compensation_json is not None:
            all_responses_json.append(write_compensation_json)
            await _mirror_public_response_payload(write_compensation_json)
        terminal_status_json = await _ensure_terminal_status_message(
            response_list_key,
            response_channel,
            final_status,
            error_message,
        )
        if terminal_status_json is not None:
            all_responses_json.append(terminal_status_json)
            await _mirror_public_response_payload(terminal_status_json)
        if compatibility_mirror_response_list_key is not None:
            await _ensure_terminal_status_message(
                compatibility_mirror_response_list_key,
                response_channel,
                final_status,
                error_message,
            )

        # 从Redis获取最终响应用于更新数据库状态
        all_responses_json = await _get_final_responses_best_effort(
            response_list_key,
            agent_run_id=agent_run_id,
            fallback_responses=all_responses_json,
        )

        # 发布最终控制信号 (END_STREAM or ERROR)
        control_signal = (
            "END_STREAM"
            if final_status == "completed"
            else "ERROR" if final_status == "failed" else "STOP"
        )

        try:
            await redis.publish(global_control_channel, control_signal)
            # 不需要发布到实例频道，因为运行正在这个实例上结束
            logger.debug(
                f"Published final control signal '{control_signal}' to {global_control_channel}"
            )
        except Exception as e:
            logger.warning(
                f"Failed to publish final control signal {control_signal}: {str(e)}"
            )

    except Exception as e:
        # 捕获异常
        error_message = str(e)
        traceback_str = traceback.format_exc()
        duration = (datetime.now(timezone.utc) - start_time).total_seconds()

        _trace_url = get_trace_url(trace_id=lf_trace_id)
        logger.error(
            f"Error in agent run {agent_run_id} after {duration:.2f}s: {error_message}\n{traceback_str} (Instance: {instance_id}) — Langfuse trace: {_trace_url}"
        )
        candidate_final_status = "failed"
        candidate_final_status = await _merge_with_persisted_agent_run_status(
            client,
            agent_run_id,
            candidate_final_status,
            error_present=True,
        )
        await _activate_terminal_write_fence(reason="exception")
        try:
            if trace is not None:
                trace.create_event(
                    name="agent_run_failed",
                    status_message=error_message,
                    level="ERROR",
                )
        except Exception as trace_error:
            logger.warning(f"Trace failed: {trace_error}")

        final_status = await _persist_and_resolve_final_agent_run_status(
            client,
            agent_run_id,
            candidate_final_status,
            error_message=f"{error_message}\n{traceback_str}",
            attempt_id=normalized_attempt_id,
            execution_epoch=normalized_execution_epoch,
        )

        user_facing_terminal_error = _resolve_terminal_error_message(
            final_status,
            error_message,
        )
        if _should_emit_exception_error_status(final_status):
            # Only surface an error status to the stream when the final user-facing
            # terminal state is actually failed.
            error_response = {
                "type": "status",
                "status": "error",
                "message": user_facing_terminal_error or error_message,
            }
            error_response_json = json.dumps(error_response, ensure_ascii=False)
            all_responses_json.append(error_response_json)
            try:
                error_pushed = await _push_response_with_retry(
                    response_list_key,
                    response_channel,
                    error_response_json,
                    agent_run_id=agent_run_id,
                )
            except RuntimeError as push_error:
                logger.error(
                    "Failed to push error response to Redis for %s: %s",
                    agent_run_id,
                    push_error,
                )
                error_pushed = False
            if not error_pushed:
                logger.error(
                    "Failed to push error response to Redis for %s; continuing terminal cleanup",
                    agent_run_id,
                )
            else:
                await _mirror_public_response_payload(error_response_json)
        else:
            logger.warning(
                "Suppressing exception error status for run %s because final terminal state is %s",
                agent_run_id,
                final_status,
            )

        logger.info(
            "Emitting terminal error status for run %s (error_present=%s)",
            agent_run_id,
            bool(error_message),
        )
        write_compensation_json = await _append_missing_write_file_terminal_status(
            response_list_key=response_list_key,
            response_channel=response_channel,
            agent_run_id=agent_run_id,
            thread_id=thread_id,
            final_status=final_status,
            enabled=qwen_runtime_guards_enabled,
        )
        if write_compensation_json is not None:
            all_responses_json.append(write_compensation_json)
            await _mirror_public_response_payload(write_compensation_json)
        terminal_status_json = await _ensure_terminal_status_message(
            response_list_key,
            response_channel,
            final_status,
            user_facing_terminal_error,
        )
        if terminal_status_json is not None:
            all_responses_json.append(terminal_status_json)
            await _mirror_public_response_payload(terminal_status_json)
        if compatibility_mirror_response_list_key is not None:
            await _ensure_terminal_status_message(
                compatibility_mirror_response_list_key,
                response_channel,
                final_status,
                user_facing_terminal_error,
            )

        # 从Redis获取最终响应用于更新数据库状态
        all_responses_json = await _get_final_responses_best_effort(
            response_list_key,
            agent_run_id=agent_run_id,
            fallback_responses=all_responses_json,
        )

        # 发布ERROR信号
        try:
            final_control_signal = "ERROR" if final_status == "failed" else "STOP"
            await redis.publish(global_control_channel, final_control_signal)
            logger.debug(
                f"Published {final_control_signal} signal to {global_control_channel}"
            )
        except Exception as e:
            logger.warning(f"Failed to publish exception terminal signal: {str(e)}")
    finally:
        final_duration_seconds = (
            datetime.now(timezone.utc) - start_time
        ).total_seconds()
        eval_record = None
        try:
            eval_record = pop_eval_run(agent_run_id)
        except Exception as eval_registry_error:
            logger.warning(
                "Failed to pop eval runtime record for run %s: %s",
                agent_run_id,
                eval_registry_error,
            )
        # Write agent output to trace for LLM-as-Judge evaluators.
        # Use BOTH update_trace (trace-level) and update (root-span-level)
        # because Langfuse v3 + ClickHouse may read output from either.
        if trace is not None and all_responses_json:
            try:
                final_output = _extract_agent_output_text(all_responses_json)
                if final_output:
                    trace.update_trace(output=final_output)
                    trace.update(output=final_output)
            except Exception:
                logger.debug(
                    "Failed to write agent output to trace for run %s",
                    agent_run_id,
                    exc_info=True,
                )

        if eval_record is not None:
            try:
                deliverable_quality = eval_record.metadata.get("deliverable_quality")
                tool_events = (
                    eval_record.tool_events
                    if eval_record.tool_events
                    else _build_phase1_tool_events_from_responses(all_responses_json)
                )
                if not tool_events and all_responses_json:
                    logger.warning(
                        "[eval] tool_events empty after both registry and fallback: "
                        "run_id=%s registry_tool_events=%s fallback_input_count=%s",
                        agent_run_id,
                        len(eval_record.tool_events),
                        len(all_responses_json),
                    )
                phase1_scores = compute_phase1_score_bundle(
                    generations=eval_record.model_generations,
                    tool_events=tool_events,
                    safety_events=eval_record.safety_events,
                    e2e_duration_seconds=final_duration_seconds,
                    final_status=final_status,
                    ttft_seconds=eval_record.ttft_seconds,
                    deliverable_quality=deliverable_quality,
                )
                filtered_scores = filter_registered_scores(phase1_scores)
                if filtered_scores:
                    score_many_numeric(agent_run_id, filtered_scores)
            except Exception as phase1_score_error:
                logger.warning(
                    "Failed to compute/write Phase 1 eval scores for run %s: %s",
                    agent_run_id,
                    phase1_score_error,
                )
        await _close_agent_generator(agent_gen, agent_run_id=agent_run_id)
        clear_agent_run_context()
        timeout_lease_stop_event.set()
        if timeout_lease_task and not timeout_lease_task.done():
            timeout_lease_task.cancel()
            try:
                await timeout_lease_task
            except asyncio.CancelledError:
                pass
            except Exception as lease_error:
                logger.warning(
                    "Error while stopping sandbox timeout lease loop for run %s: %s",
                    agent_run_id,
                    lease_error,
                )
        # 清理停止检查器任务
        if stop_checker and not stop_checker.done():
            stop_checker.cancel()
            try:
                await stop_checker
            except asyncio.CancelledError:
                pass
            except Exception as e:
                logger.warning(f"Error during stop_checker cancellation: {e}")

        # 关闭pubsub连接
        if pubsub:
            try:
                await pubsub.unsubscribe()
                await pubsub.aclose()  # Use aclose() instead of deprecated close()
                logger.debug(f"Closed pubsub connection for {agent_run_id}")
            except Exception as e:
                logger.warning(f"Error closing pubsub for {agent_run_id}: {str(e)}")
        # 设置Redis响应列表的TTL
        await _cleanup_redis_response_list(
            agent_run_id,
            response_list_key=response_list_key,
        )
        if compatibility_mirror_response_list_key is not None:
            await _cleanup_redis_response_list(
                agent_run_id,
                response_list_key=compatibility_mirror_response_list_key,
            )

        # 清理实例特定的活跃运行键
        await _cleanup_redis_instance_key(agent_run_id)
        await _cleanup_project_active_run_key(
            project_id=project_id,
            agent_run_id=agent_run_id,
        )

        if run_capacity_lease_acquired:
            try:
                await release_run_capacity_lease(
                    agent_run_id=run_capacity_lease_key,
                    owner_token=run_lock_owner,
                )
            except Exception as capacity_release_error:
                logger.warning(
                    "Failed to release run capacity lease for %s: %s",
                    agent_run_id,
                    capacity_release_error,
                )

        # 清理运行锁
        await _cleanup_redis_run_lock(agent_run_id, run_lock_owner)

        # Schedule pause with grace period after agent run completes.
        await _schedule_project_sandbox_auto_pause(project_id)

        schedule_post_run_review(
            responses_json=all_responses_json,
            thread_id=thread_id,
            agent_run_id=agent_run_id,
            final_status=final_status,
            run_model_key=effective_model,
        )

        logger.info(
            f"Agent run background task fully completed for: {agent_run_id} (Instance: {instance_id}) with final status: {final_status}"
        )
        # Close the Langfuse root span and flush buffered observations.
        try:
            if trace is not None:
                trace.end()
        except Exception as trace_end_error:
            logger.warning(
                "Failed to end Langfuse trace for run %s: %s",
                agent_run_id,
                trace_end_error,
            )
        try:
            langfuse.flush()
        except Exception as flush_error:
            logger.warning(
                "Failed to flush Langfuse for run %s: %s",
                agent_run_id,
                flush_error,
            )
        return final_status


def _extract_agent_output_text(
    responses_json: list[Any],
    *,
    max_chars: int = 50_000,
) -> str:
    """Extract concatenated assistant LLM text from response entries.

    Each entry in *responses_json* is a JSON string (or bytes) representing a
    response dict.  The ``content`` field inside that dict is itself a
    JSON-serialised dict whose ``content`` key holds the display text for
    ``type == "assistant"`` messages.
    """
    text_parts: list[str] = []
    for entry in responses_json:
        try:
            if isinstance(entry, bytes):
                entry = entry.decode("utf-8", errors="replace")
            if isinstance(entry, str):
                payload: dict[str, Any] = json.loads(entry)
            elif isinstance(entry, dict):
                payload = entry
            else:
                continue
        except (json.JSONDecodeError, TypeError, ValueError):
            continue

        if payload.get("type") != "assistant":
            continue
        if not payload.get("is_llm_message"):
            continue

        try:
            raw_content = payload.get("content")
            if isinstance(raw_content, str):
                inner = json.loads(raw_content)
            elif isinstance(raw_content, dict):
                inner = raw_content
            else:
                continue
            text = inner.get("content", "") if isinstance(inner, dict) else ""
        except (json.JSONDecodeError, TypeError, ValueError):
            continue

        if isinstance(text, str) and text.strip():
            text_parts.append(text.strip())

    if not text_parts:
        return ""

    full_text = "\n\n".join(text_parts)
    if len(full_text) > max_chars:
        full_text = full_text[:max_chars]
    return full_text


def _coerce_regular_pool_metadata(metadata_value: Any) -> dict[str, Any]:
    if isinstance(metadata_value, dict):
        return dict(metadata_value)
    if isinstance(metadata_value, str):
        normalized_value = metadata_value.strip()
        if not normalized_value:
            return {}
        try:
            parsed_value = json.loads(normalized_value)
        except json.JSONDecodeError:
            return {}
        if isinstance(parsed_value, dict):
            return parsed_value
    return {}


def _resolve_regular_harness_user_message_override(metadata: Any) -> str | None:
    normalized_metadata = _coerce_regular_pool_metadata(metadata)
    harness_metadata = _coerce_regular_pool_metadata(
        normalized_metadata.get("regular_load_harness")
    )
    override = str(
        harness_metadata.get("user_message_override")
        or normalized_metadata.get("user_message_override")
        or ""
    ).strip()
    return override or None


def _resolve_regular_harness_real_profile_hold_config(
    metadata: Any,
    *,
    stub_config: Any,
) -> dict[str, float] | None:
    if stub_config is not None:
        return None

    normalized_metadata = _coerce_regular_pool_metadata(metadata)
    harness_metadata = _coerce_regular_pool_metadata(
        normalized_metadata.get("regular_load_harness")
    )
    if not harness_metadata or not bool(harness_metadata.get("enabled")):
        return None

    execution_profile = (
        str(harness_metadata.get("execution_profile") or "").strip().lower()
    )
    if not execution_profile or execution_profile == "stub_backend":
        return None

    try:
        active_duration_seconds = float(
            harness_metadata.get("active_duration_seconds") or 0.0
        )
    except (TypeError, ValueError):
        return None
    if active_duration_seconds <= 0.0:
        return None

    try:
        chunk_interval_seconds = float(
            harness_metadata.get("chunk_interval_seconds") or 0.25
        )
    except (TypeError, ValueError):
        chunk_interval_seconds = 0.25

    return {
        "active_duration_seconds": max(0.01, active_duration_seconds),
        "chunk_interval_seconds": max(0.01, chunk_interval_seconds),
    }


def _coerce_resume_window_minutes(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 1440


async def _execute_claimed_regular_run_from_pool(
    *,
    client: Any,
    run_row: dict[str, Any],
    owner_token: str,
) -> str:
    metadata = _coerce_regular_pool_metadata(run_row.get("metadata"))
    resolved_model = str(metadata.get("model_name") or config.MODEL_TO_USE).strip()
    if not resolved_model:
        resolved_model = config.MODEL_TO_USE

    return await execute_regular_run_kernel(
        client=client,
        agent_run_id=str(run_row["agent_run_id"]),
        thread_id=str(run_row["thread_id"]),
        instance_id=instance_id,
        project_id=str(run_row["project_id"]),
        model_name=resolved_model,
        enable_thinking=metadata.get("enable_thinking"),
        reasoning_effort=metadata.get("reasoning_effort"),
        stream=True,
        enable_context_manager=bool(metadata.get("enable_context_manager", True)),
        agent_config=None,
        is_agent_builder=False,
        target_agent_id=None,
        request_id=metadata.get("request_id"),
        resume_strategy=str(metadata.get("resume_strategy") or "auto"),
        resume_window_minutes=_coerce_resume_window_minutes(
            metadata.get("resume_window_minutes")
        ),
        shadow_clone_mode="off",
        shadow_clone_main_model=None,
        shadow_clone_subagent_model=None,
        owner_token=owner_token,
        run_capacity_mode="off",
        user_message_override=_resolve_regular_harness_user_message_override(metadata),
    )


async def _reconcile_orphaned_regular_async_pool_runs(client: Any) -> list[str]:
    orphaned_run_ids = (
        await regular_async_pool.find_orphaned_regular_async_pool_run_ids(client)
    )
    if not orphaned_run_ids:
        return []
    return await regular_async_pool.reconcile_expired_regular_run_claims(
        client,
        orphaned_run_ids,
    )


@dramatiq.actor(queue_name=RUN_AGENT_BACKGROUND_QUEUE, time_limit=3_600_000)
async def regular_async_pool_dispatch(trigger: str = "event") -> None:
    await initialize()
    client = await db.client
    normalized_trigger = str(trigger or "event").strip().lower()
    if normalized_trigger == "idle":
        await regular_async_pool.clear_idle_dispatch_wakeup()
    dispatcher_owner = f"regular-pool-dispatcher:{os.getpid()}"
    lock_result = await regular_async_pool.try_acquire_dispatcher_lock(
        owner_token=dispatcher_owner
    )
    if not bool(lock_result.get("acquired")):
        return

    async def _executor(run_row: dict[str, Any], owner_token: str) -> str:
        return await _execute_claimed_regular_run_from_pool(
            client=client,
            run_row=run_row,
            owner_token=owner_token,
        )

    try:
        await _reconcile_orphaned_regular_async_pool_runs(client)
        await regular_async_pool.run_dispatch_loop(
            client=client,
            executor=_executor,
            owner_token=f"regular-pool:{os.getpid()}:{uuid.uuid4()}",
        )
        await _reconcile_orphaned_regular_async_pool_runs(client)
    finally:
        try:
            await regular_async_pool.release_dispatcher_lock(dispatcher_owner)
        finally:
            if regular_async_pool.is_regular_async_pool_enabled("off"):
                await regular_async_pool.schedule_idle_dispatch_wakeup(
                    actor_handle=regular_async_pool_dispatch,
                )


@dramatiq.actor(queue_name=RUN_AGENT_BACKGROUND_QUEUE, time_limit=3_600_000)
async def run_agent_background(
    agent_run_id: str,
    thread_id: str,
    instance_id: str,
    project_id: str,
    model_name: str,
    enable_thinking: Optional[bool],
    reasoning_effort: Optional[str],
    stream: bool,
    enable_context_manager: bool,
    agent_config: Optional[dict] = None,
    is_agent_builder: Optional[bool] = False,
    target_agent_id: Optional[str] = None,
    request_id: Optional[str] = None,
    resume_strategy: str = "auto",
    resume_window_minutes: int = 1440,
    shadow_clone_mode: Optional[str] = None,
    shadow_clone_main_model: Optional[str] = None,
    shadow_clone_subagent_model: Optional[str] = None,
):
    await execute_regular_run_kernel(
        agent_run_id=agent_run_id,
        thread_id=thread_id,
        instance_id=instance_id,
        project_id=project_id,
        model_name=model_name,
        enable_thinking=enable_thinking,
        reasoning_effort=reasoning_effort,
        stream=stream,
        enable_context_manager=enable_context_manager,
        agent_config=agent_config,
        is_agent_builder=is_agent_builder,
        target_agent_id=target_agent_id,
        request_id=request_id,
        resume_strategy=resume_strategy,
        resume_window_minutes=resume_window_minutes,
        shadow_clone_mode=shadow_clone_mode,
        shadow_clone_main_model=shadow_clone_main_model,
        shadow_clone_subagent_model=shadow_clone_subagent_model,
    )


async def _cleanup_redis_instance_key(agent_run_id: str):
    """Clean up the instance-specific Redis key for an agent run."""
    if not instance_id:
        logger.warning("Instance ID not set, cannot clean up instance key.")
        return
    key = f"active_run:{instance_id}:{agent_run_id}"
    logger.debug(f"Cleaning up Redis instance key: {key}")
    try:
        await redis.delete(key)
        logger.debug(f"Successfully cleaned up Redis key: {key}")
    except Exception as e:
        logger.warning(f"Failed to clean up Redis key {key}: {str(e)}")


async def _refresh_redis_run_lock(agent_run_id: str, owner_token: str) -> str:
    """Refresh the run lock only if this supervisor still owns it."""
    run_lock_key = f"agent_run_lock:{agent_run_id}"
    try:
        result = int(
            await redis.eval_script(
                _REFRESH_RUN_LOCK_LUA,
                keys=[run_lock_key],
                args=[str(owner_token or ""), str(RUN_LOCK_TTL_SECONDS)],
            )
            or 0
        )
    except Exception as refresh_error:
        logger.warning(
            "Failed to refresh Redis run lock %s: %s",
            run_lock_key,
            refresh_error,
        )
        return "error"

    if result == 1:
        return "refreshed"
    if result == -1:
        return "owner_mismatch"
    return "missing"


async def _take_over_redis_run_lock(
    agent_run_id: str,
    *,
    expected_owner_token: str,
    new_owner_token: str,
) -> str:
    run_lock_key = f"agent_run_lock:{agent_run_id}"
    try:
        result = int(
            await redis.eval_script(
                _TAKE_OVER_RUN_LOCK_LUA,
                keys=[run_lock_key],
                args=[
                    str(expected_owner_token or ""),
                    str(new_owner_token or ""),
                    str(RUN_LOCK_TTL_SECONDS),
                ],
            )
            or 0
        )
    except Exception as takeover_error:
        logger.warning(
            "Failed to take over Redis run lock %s expected_owner=%s new_owner=%s: %s",
            run_lock_key,
            expected_owner_token,
            new_owner_token,
            takeover_error,
        )
        return "error"

    if result == 1:
        return "taken_over"
    if result == -1:
        return "owner_mismatch"
    return "missing"


async def _cleanup_redis_run_lock(agent_run_id: str, owner_token: str):
    """Release the run lock only if this supervisor still owns it."""
    run_lock_key = f"agent_run_lock:{agent_run_id}"
    logger.debug(f"Cleaning up Redis run lock key: {run_lock_key}")
    try:
        result = int(
            await redis.eval_script(
                _RELEASE_RUN_LOCK_LUA,
                keys=[run_lock_key],
                args=[str(owner_token or "")],
            )
            or 0
        )
        if result == 1:
            logger.debug(f"Successfully cleaned up Redis run lock key: {run_lock_key}")
        elif result == -1:
            logger.warning(
                "Skipped cleanup for Redis run lock key %s because ownership changed",
                run_lock_key,
            )
    except Exception as e:
        logger.warning(
            f"Failed to clean up Redis run lock key {run_lock_key}: {str(e)}"
        )


def _control_plane_refresh_error_is_fatal(
    *,
    last_success_at: float,
    now_monotonic: float,
    ttl_seconds: int,
    refresh_interval_seconds: float,
) -> bool:
    effective_ttl = max(0.0, float(ttl_seconds))
    effective_refresh_interval = max(0.0, float(refresh_interval_seconds))
    if effective_ttl <= 0.0:
        return True

    # A transient refresh error is acceptable only while enough TTL headroom
    # remains for another scheduled refresh attempt before expiry.
    allowed_staleness = effective_ttl - min(effective_ttl, effective_refresh_interval)
    if allowed_staleness <= 0.0:
        return True
    return (now_monotonic - max(0.0, last_success_at)) >= allowed_staleness


def _project_active_run_key(project_id: Optional[str]) -> str:
    if not project_id:
        return ""
    return f"{PROJECT_ACTIVE_RUN_KEY_PREFIX}{project_id}"


def _build_project_active_run_payload(
    *,
    project_id: str,
    agent_run_id: str,
    instance_id: str,
    status: str,
) -> str:
    now_iso = datetime.now(timezone.utc).isoformat()
    payload = {
        "project_id": project_id,
        "agent_run_id": agent_run_id,
        "instance_id": instance_id,
        "status": status,
        "heartbeat_at": now_iso,
    }
    return json.dumps(payload, ensure_ascii=False)


async def _set_project_active_run_key(
    *,
    project_id: Optional[str],
    agent_run_id: str,
    instance_id: str,
    status: str,
) -> None:
    if not PROJECT_ACTIVE_RUN_HEARTBEAT_ENABLED or not project_id:
        return
    try:
        payload = _build_project_active_run_payload(
            project_id=project_id,
            agent_run_id=agent_run_id,
            instance_id=instance_id,
            status=status,
        )
        await redis.set(
            _project_active_run_key(project_id),
            payload,
            ex=PROJECT_ACTIVE_RUN_KEY_TTL_SECONDS,
        )
    except Exception as key_error:
        logger.warning(
            "Failed to update project active run key (project=%s run=%s): %s",
            project_id,
            agent_run_id,
            key_error,
        )


async def _cleanup_project_active_run_key(
    *, project_id: Optional[str], agent_run_id: str
) -> None:
    key = _project_active_run_key(project_id)
    if not key:
        return
    try:
        raw_value = await redis.get(key)
        if not raw_value:
            return

        should_delete = False
        try:
            parsed = json.loads(raw_value) if isinstance(raw_value, str) else {}
            owner_run_id = str(parsed.get("agent_run_id") or "").strip()
            should_delete = owner_run_id == agent_run_id
        except Exception:
            should_delete = False

        if not should_delete:
            return

        redis_client = await redis.get_client()
        await redis_client.eval(
            """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
""",
            1,
            key,
            raw_value,
        )
    except Exception as cleanup_error:
        logger.warning(
            "Failed to cleanup project active run key %s: %s",
            key,
            cleanup_error,
        )


async def _project_has_active_run(project_id: Optional[str]) -> bool:
    key = _project_active_run_key(project_id)
    if not key:
        return False
    try:
        raw_value = await redis.get(key)
    except Exception:
        return False
    if not raw_value:
        return False
    try:
        parsed = json.loads(raw_value) if isinstance(raw_value, str) else {}
    except Exception:
        return True
    return bool(str(parsed.get("agent_run_id") or "").strip())


def _parse_project_sandbox_info(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    if isinstance(raw, dict):
        return raw
    return {}


async def _extend_project_sandbox_timeout_once(
    *,
    project_id: str,
    agent_run_id: str,
) -> bool:
    try:
        client = await db.client
        project = (
            await client.table("projects")
            .select("sandbox")
            .eq("project_id", project_id)
            .execute()
        )
        if not project.data:
            return False
        sandbox_info = _parse_project_sandbox_info(project.data[0].get("sandbox", {}))
        sandbox_id = str(sandbox_info.get("id") or "").strip()
        sandbox_type = str(sandbox_info.get("type") or "desktop").strip() or "desktop"
        if not sandbox_id or sandbox_info.get("state") == "paused":
            return False

        from sandbox.sandbox import get_or_start_sandbox

        sandbox_obj = await asyncio.wait_for(
            get_or_start_sandbox(sandbox_id, sandbox_type),
            timeout=SANDBOX_SET_TIMEOUT_CONNECT_TIMEOUT_SECONDS,
        )
        set_timeout_callable = getattr(sandbox_obj, "set_timeout", None)
        if not callable(set_timeout_callable):
            return False

        set_timeout_result = await asyncio.to_thread(
            set_timeout_callable,
            SANDBOX_SET_TIMEOUT_WINDOW_SECONDS,
        )
        if inspect.isawaitable(set_timeout_result):
            await asyncio.wait_for(
                set_timeout_result,
                timeout=SANDBOX_SET_TIMEOUT_CONNECT_TIMEOUT_SECONDS,
            )

        logger.info(
            "Sandbox timeout heartbeat extended for project %s run %s sandbox %s window=%ss",
            project_id,
            agent_run_id,
            sandbox_id,
            SANDBOX_SET_TIMEOUT_WINDOW_SECONDS,
        )
        return True
    except Exception as timeout_error:
        logger.warning(
            "Sandbox timeout heartbeat failed for project %s run %s: %s",
            project_id,
            agent_run_id,
            timeout_error,
        )
        return False


async def _extend_shadow_clone_sandbox_timeouts_once(
    *,
    project_id: str,
    agent_run_id: str,
) -> Optional[bool]:
    try:
        from agentscope_integration.shadow_clone.sandbox_lease import (
            get_run_sandbox_lease,
            is_attachable_shadow_clone_binding_state,
            mark_run_sandbox_unattachable,
        )
        from sandbox.sandbox import extend_sandbox_timeout
    except Exception as import_error:
        logger.debug(
            "Shadow Clone timeout heartbeat imports unavailable for run %s: %s",
            agent_run_id,
            import_error,
        )
        return None

    try:
        lease = await get_run_sandbox_lease(agent_run_id)
    except Exception as lease_error:
        logger.warning(
            "Shadow Clone timeout heartbeat failed to load lease for run %s: %s",
            agent_run_id,
            lease_error,
        )
        return None

    if not lease or str(lease.get("project_id") or "").strip() != project_id:
        return None

    targets = []
    active_sandbox_id = str(lease.get("sandbox_id") or "").strip()
    active_sandbox_type = (
        str(lease.get("sandbox_type") or "desktop").strip() or "desktop"
    )
    active_state = (
        str((lease.get("sandbox_info") or {}).get("state") or "").strip().lower()
    )
    binding_state = str(lease.get("binding_state") or "").strip().lower()
    if (
        active_sandbox_id
        and active_state != "paused"
        and (
            not binding_state or is_attachable_shadow_clone_binding_state(binding_state)
        )
    ):
        targets.append(("active", active_sandbox_id, active_sandbox_type))

    standby_sandbox_id = str(lease.get("standby_sandbox_id") or "").strip()
    standby_sandbox_type = (
        str(lease.get("standby_sandbox_type") or active_sandbox_type).strip()
        or active_sandbox_type
    )
    standby_state = (
        str((lease.get("standby_sandbox_info") or {}).get("state") or "")
        .strip()
        .lower()
    )
    standby_available = bool(standby_sandbox_id and standby_state != "paused")
    if standby_sandbox_id and standby_state != "paused":
        targets.append(("standby", standby_sandbox_id, standby_sandbox_type))

    if not targets:
        return False

    succeeded = False
    for target_role, sandbox_id, sandbox_type in targets:
        try:
            await extend_sandbox_timeout(
                sandbox_id,
                sandbox_type,
                timeout_window_seconds=SANDBOX_SET_TIMEOUT_WINDOW_SECONDS,
                connect_timeout_seconds=SANDBOX_SET_TIMEOUT_CONNECT_TIMEOUT_SECONDS,
                require_running=True,
                run_id=agent_run_id if target_role == "active" else None,
            )
            logger.info(
                "Shadow Clone timeout heartbeat extended for project %s run %s %s sandbox %s window=%ss",
                project_id,
                agent_run_id,
                target_role,
                sandbox_id,
                SANDBOX_SET_TIMEOUT_WINDOW_SECONDS,
            )
            succeeded = True
        except Exception as timeout_error:
            logger.warning(
                "Shadow Clone timeout heartbeat failed for project %s run %s %s sandbox %s: %s",
                project_id,
                agent_run_id,
                target_role,
                sandbox_id,
                timeout_error,
            )
            if target_role == "active":
                try:
                    lease_update = await mark_run_sandbox_unattachable(
                        agent_run_id,
                        expected_sandbox_id=sandbox_id,
                        last_error=(
                            "Active Shadow Clone sandbox heartbeat failed: "
                            f"{timeout_error}"
                        ),
                        standby_available=standby_available,
                    )
                    if lease_update is not None:
                        logger.warning(
                            "Shadow Clone active sandbox marked non-attachable for run %s "
                            "(binding_state=%s standby_available=%s)",
                            agent_run_id,
                            lease_update.get("binding_state"),
                            standby_available,
                        )
                except Exception as lease_update_error:
                    logger.warning(
                        "Failed to publish Shadow Clone lease degradation for run %s: %s",
                        agent_run_id,
                        lease_update_error,
                    )
    return succeeded


async def _sandbox_timeout_lease_loop(
    *,
    project_id: str,
    agent_run_id: str,
    stop_event: asyncio.Event,
) -> None:
    if not SANDBOX_SET_TIMEOUT_ENABLED:
        return

    while not stop_event.is_set():
        shadow_clone_result = await _extend_shadow_clone_sandbox_timeouts_once(
            project_id=project_id,
            agent_run_id=agent_run_id,
        )
        if shadow_clone_result is None:
            await _extend_project_sandbox_timeout_once(
                project_id=project_id,
                agent_run_id=agent_run_id,
            )
        try:
            await asyncio.wait_for(
                stop_event.wait(),
                timeout=SANDBOX_SET_TIMEOUT_HEARTBEAT_SECONDS,
            )
        except asyncio.TimeoutError:
            continue


_sandbox_pause_monitor_tasks: Dict[str, asyncio.Task] = {}
_sandbox_pause_monitor_lock: Optional[asyncio.Lock] = None


def _is_auto_pause_enabled() -> bool:
    return os.getenv("SANDBOX_AUTO_PAUSE_ON_COMPLETE", "true").lower() == "true"


def _is_auto_pause_delay_enabled() -> bool:
    raw_value = os.getenv("SANDBOX_AUTO_PAUSE_DELAY_ENABLED")
    if raw_value is None:
        return bool(getattr(config, "SANDBOX_AUTO_PAUSE_DELAY_ENABLED", True))
    return raw_value.lower() == "true"


def _get_auto_pause_grace_seconds() -> int:
    raw_value = os.getenv("SANDBOX_AUTO_PAUSE_GRACE_SECONDS", "300")
    try:
        return max(0, int(raw_value))
    except (TypeError, ValueError):
        return 300


def _parse_iso_datetime(value: Any) -> Optional[datetime]:
    if not value or not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _get_pause_monitor_lock() -> asyncio.Lock:
    global _sandbox_pause_monitor_lock
    if _sandbox_pause_monitor_lock is None:
        _sandbox_pause_monitor_lock = asyncio.Lock()
    return _sandbox_pause_monitor_lock


async def _touch_project_sandbox_access(project_id: str, access_time: datetime) -> None:
    """Record last sandbox access to drive inactivity-based auto-pause windows."""
    try:
        client = await db.client
        project = (
            await client.table("projects")
            .select("sandbox")
            .eq("project_id", project_id)
            .execute()
        )
        if not project.data:
            return

        raw = project.data[0].get("sandbox", "{}")
        sandbox_info = json.loads(raw) if isinstance(raw, str) else (raw or {})
        sandbox_id = sandbox_info.get("id")
        if not sandbox_id:
            return

        updated_info = {
            **sandbox_info,
            "last_accessed_at": access_time.isoformat(),
            "pause_grace_seconds": _get_auto_pause_grace_seconds(),
        }
        await client.table("projects").eq("project_id", project_id).update(
            {"sandbox": json.dumps(updated_info)}
        )
    except Exception as touch_error:
        logger.warning(
            f"Failed to update sandbox access timestamp for project {project_id}: {touch_error}"
        )


async def _project_sandbox_auto_pause_monitor(
    project_id: str, reference_time: datetime
) -> None:
    inactivity_reference = reference_time

    while True:
        if not _is_auto_pause_enabled():
            return

        if await _project_has_active_run(project_id):
            inactivity_reference = datetime.now(timezone.utc)
            await asyncio.sleep(AUTO_PAUSE_ACTIVE_RUN_POLL_SECONDS)
            continue

        client = await db.client
        project = (
            await client.table("projects")
            .select("sandbox")
            .eq("project_id", project_id)
            .execute()
        )
        if not project.data:
            return

        raw = project.data[0].get("sandbox", "{}")
        sandbox_info = json.loads(raw) if isinstance(raw, str) else (raw or {})
        sandbox_id = sandbox_info.get("id")
        if not sandbox_id:
            return
        if sandbox_info.get("state") == "paused":
            return

        last_accessed = _parse_iso_datetime(sandbox_info.get("last_accessed_at"))
        if last_accessed and last_accessed > inactivity_reference:
            inactivity_reference = last_accessed

        grace_seconds = _get_auto_pause_grace_seconds()
        if grace_seconds > 0:
            elapsed = (
                datetime.now(timezone.utc) - inactivity_reference
            ).total_seconds()
            remaining = grace_seconds - elapsed
            if remaining > 0:
                await asyncio.sleep(max(0.5, min(remaining, grace_seconds)))
                continue

        # Re-check once before pausing to avoid races with fresh access updates.
        latest = (
            await client.table("projects")
            .select("sandbox")
            .eq("project_id", project_id)
            .execute()
        )
        if not latest.data:
            return
        latest_raw = latest.data[0].get("sandbox", "{}")
        latest_info = (
            json.loads(latest_raw)
            if isinstance(latest_raw, str)
            else (latest_raw or {})
        )
        latest_id = latest_info.get("id")
        if not latest_id or latest_info.get("state") == "paused":
            return

        latest_accessed = _parse_iso_datetime(latest_info.get("last_accessed_at"))
        if latest_accessed and latest_accessed > inactivity_reference:
            inactivity_reference = latest_accessed
            continue

        if await _project_has_active_run(project_id):
            inactivity_reference = datetime.now(timezone.utc)
            continue

        await _pause_project_sandbox(project_id)
        return


async def _schedule_project_sandbox_auto_pause(project_id: str) -> None:
    """Schedule a per-project inactivity monitor that pauses sandbox after grace period."""
    if not _is_auto_pause_enabled():
        return
    if not _is_auto_pause_delay_enabled():
        await _pause_project_sandbox(project_id)
        return

    reference_time = datetime.now(timezone.utc)
    await _touch_project_sandbox_access(project_id, reference_time)

    lock = _get_pause_monitor_lock()
    async with lock:
        existing_task = _sandbox_pause_monitor_tasks.get(project_id)
        if existing_task and not existing_task.done():
            return

        task = asyncio.create_task(
            _project_sandbox_auto_pause_monitor(project_id, reference_time),
            name=f"sandbox-auto-pause:{project_id}",
        )
        _sandbox_pause_monitor_tasks[project_id] = task

        def _cleanup(
            done_task: asyncio.Task, *, target_project_id: str = project_id
        ) -> None:
            current = _sandbox_pause_monitor_tasks.get(target_project_id)
            if current is done_task:
                _sandbox_pause_monitor_tasks.pop(target_project_id, None)
            try:
                done_task.result()
            except asyncio.CancelledError:
                pass
            except Exception as monitor_error:
                logger.warning(
                    f"Sandbox auto pause monitor failed for project {target_project_id}: {monitor_error}"
                )

        task.add_done_callback(_cleanup)


async def _pause_project_sandbox(project_id: str):
    """Pause the project's sandbox immediately."""
    if not _is_auto_pause_enabled():
        return
    try:
        client = await db.client
        project = (
            await client.table("projects")
            .select("sandbox")
            .eq("project_id", project_id)
            .execute()
        )
        if not project.data:
            return
        raw = project.data[0].get("sandbox", "{}")
        sandbox_info = json.loads(raw) if isinstance(raw, str) else (raw or {})
        sandbox_id = sandbox_info.get("id")
        if not sandbox_id or sandbox_info.get("state") == "paused":
            return

        from sandbox.sandbox import pause_sandbox

        paused = await pause_sandbox(sandbox_id)
        if paused:
            sandbox_info = {
                **sandbox_info,
                "state": "paused",
                "paused_at": datetime.now(timezone.utc).isoformat(),
            }
            await client.table("projects").eq("project_id", project_id).update(
                {"sandbox": json.dumps(sandbox_info)}
            )
            logger.info(f"Sandbox {sandbox_id} paused for project {project_id}")

            # Clear stale Redis mapping so verify_sandbox_access won't hit a mismatch
            try:
                from sandbox.api import SANDBOX_ID_MAP_PREFIX

                mapped = await redis.get(f"{SANDBOX_ID_MAP_PREFIX}{sandbox_id}")
                if mapped and mapped != sandbox_id:
                    await redis.delete(f"{SANDBOX_ID_MAP_PREFIX}{sandbox_id}")
                    logger.info(
                        f"Cleared stale Redis sandbox mapping {sandbox_id} -> {mapped}"
                    )
            except Exception:
                pass
        else:
            logger.warning(
                "Pause request returned unsuccessful result",
                extra={
                    "project_id": project_id,
                    "sandbox_id": sandbox_id,
                    "sandbox_state": sandbox_info.get("state"),
                },
            )
    except Exception as e:
        logger.warning(f"Failed to pause sandbox for project {project_id}: {e}")


# Response history is long-lived enough for late subscribers and downloads, but
# initial writes also get a crash-safe TTL so abandoned runs do not leak forever.
REDIS_RESPONSE_LIST_TTL = max(
    60,
    _read_int_env("AGENTSCOPE_REDIS_RESPONSE_LIST_TTL_SECONDS", 3600 * 24),
)
REDIS_RESPONSE_LIST_INITIAL_TTL = max(
    REDIS_RESPONSE_LIST_TTL,
    _read_int_env("AGENTSCOPE_REDIS_RESPONSE_LIST_INITIAL_TTL_SECONDS", 3600 * 48),
)
MAX_RESPONSE_BYTES = int(os.getenv("AGENTSCOPE_MAX_RESPONSE_BYTES", "500000"))
MAX_REDIS_RESPONSES = int(os.getenv("AGENTSCOPE_MAX_REDIS_RESPONSES", "5000"))
MAX_GENERATED_RESPONSES = int(os.getenv("AGENTSCOPE_MAX_GENERATED_RESPONSES", "200000"))
MAX_EVAL_GENERATED_RESPONSES = int(
    os.getenv("AGENTSCOPE_MAX_EVAL_GENERATED_RESPONSES", "200000")
)
REDIS_WRITE_TIMEOUT_SECONDS = float(
    os.getenv("AGENTSCOPE_REDIS_WRITE_TIMEOUT_SECONDS", "10.0")
)
REDIS_WRITE_RETRY_ATTEMPTS = int(
    os.getenv("AGENTSCOPE_REDIS_WRITE_RETRY_ATTEMPTS", "3")
)
ACTIVE_RUN_KEY_REFRESH_INTERVAL_SECONDS = float(
    os.getenv("AGENTSCOPE_ACTIVE_RUN_REFRESH_INTERVAL_SECONDS", "30")
)


def _coerce_positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _resolve_max_generated_responses(run_metadata: Optional[Dict[str, Any]]) -> int:
    if not isinstance(run_metadata, dict):
        return MAX_GENERATED_RESPONSES

    override = _coerce_positive_int(run_metadata.get("eval_max_generated_responses"))
    if override is None:
        return MAX_GENERATED_RESPONSES

    return min(override, MAX_EVAL_GENERATED_RESPONSES)


def _safe_env_int(name: str, default: int, *, min_value: int = 1) -> int:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        parsed = int(raw)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= min_value else min_value


def _safe_env_float(name: str, default: float, *, min_value: float = 0.1) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        parsed = float(raw)
    except (TypeError, ValueError):
        return default
    return parsed if parsed >= min_value else min_value


def _safe_env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


PROJECT_ACTIVE_RUN_KEY_PREFIX = os.getenv("PROJECT_ACTIVE_RUN_KEY_PREFIX") or os.getenv(
    "AGENTSCOPE_PROJECT_ACTIVE_RUN_KEY_PREFIX", "project_active_run:"
)
PROJECT_ACTIVE_RUN_HEARTBEAT_ENABLED = _safe_env_bool(
    "AGENTSCOPE_PROJECT_ACTIVE_RUN_HEARTBEAT_ENABLED",
    True,
)
PROJECT_ACTIVE_RUN_KEY_TTL_SECONDS = _safe_env_int(
    "AGENTSCOPE_PROJECT_ACTIVE_RUN_KEY_TTL_SECONDS",
    90,
    min_value=10,
)
AUTO_PAUSE_ACTIVE_RUN_POLL_SECONDS = _safe_env_float(
    "SANDBOX_AUTO_PAUSE_ACTIVE_RUN_POLL_SECONDS",
    5.0,
    min_value=0.5,
)
SANDBOX_SET_TIMEOUT_ENABLED = _safe_env_bool(
    "SANDBOX_SET_TIMEOUT_ENABLED",
    True,
)
SANDBOX_SET_TIMEOUT_WINDOW_SECONDS = _safe_env_int(
    "SANDBOX_SET_TIMEOUT_WINDOW_SECONDS",
    1800,
    min_value=60,
)
SANDBOX_SET_TIMEOUT_HEARTBEAT_SECONDS = _safe_env_float(
    "SANDBOX_SET_TIMEOUT_HEARTBEAT_SECONDS",
    120.0,
    min_value=5.0,
)
SANDBOX_SET_TIMEOUT_CONNECT_TIMEOUT_SECONDS = _safe_env_float(
    "SANDBOX_SET_TIMEOUT_CONNECT_TIMEOUT_SECONDS",
    20.0,
    min_value=1.0,
)


QWEN_SANDBOX_FAILURE_THRESHOLD = max(
    1,
    int(os.getenv("AGENTSCOPE_QWEN_SANDBOX_FAILURE_THRESHOLD", "2")),
)
QWEN_WRITE_ARG_MAX_CHUNKS = _safe_env_int(
    "AGENTSCOPE_QWEN_WRITE_ARG_MAX_CHUNKS",
    120,
    min_value=20,
)
QWEN_WRITE_ARG_WARN_CHUNK_INTERVAL = _safe_env_int(
    "AGENTSCOPE_QWEN_WRITE_ARG_WARN_CHUNK_INTERVAL",
    30,
    min_value=5,
)
QWEN_WRITE_ARG_MAX_SECONDS = _safe_env_float(
    "AGENTSCOPE_QWEN_WRITE_ARG_MAX_SECONDS",
    20.0,
    min_value=1.0,
)
QWEN_WRITE_ARG_STALL_SECONDS = _safe_env_float(
    "AGENTSCOPE_QWEN_WRITE_ARG_STALL_SECONDS",
    12.0,
    min_value=1.0,
)
QWEN_WRITE_ARG_STALL_WITH_CONTENT_SECONDS = _safe_env_float(
    "AGENTSCOPE_QWEN_WRITE_STALL_TIMEOUT_WITH_CONTENT_SECONDS",
    20.0,
    min_value=1.0,
)
QWEN_WRITE_CHUNK_MIN_GROWTH_BYTES = _safe_env_int(
    "AGENTSCOPE_QWEN_WRITE_CHUNK_MIN_GROWTH_BYTES",
    256,
    min_value=32,
)
QWEN_WRITE_CHUNK_MIN_EMIT_INTERVAL_MS = _safe_env_int(
    "AGENTSCOPE_QWEN_WRITE_CHUNK_MIN_EMIT_INTERVAL_MS",
    120,
    min_value=20,
)
QWEN_STREAM_POLL_SECONDS = _safe_env_float(
    "AGENTSCOPE_QWEN_STREAM_POLL_SECONDS",
    0.5,
    min_value=0.1,
)
NON_QWEN_RESPONSE_POLL_SECONDS = _safe_env_float(
    "AGENTSCOPE_NON_QWEN_RESPONSE_POLL_SECONDS",
    0.5,
    min_value=0.1,
)
QWEN_WRITE_IDLE_TIMEOUT_SECONDS = _safe_env_float(
    "AGENTSCOPE_QWEN_WRITE_IDLE_TIMEOUT_SECONDS",
    20.0,
    min_value=1.0,
)
QWEN_WRITE_IDLE_TIMEOUT_WITH_CONTENT_SECONDS = _safe_env_float(
    "AGENTSCOPE_QWEN_WRITE_IDLE_TIMEOUT_WITH_CONTENT_SECONDS",
    35.0,
    min_value=1.0,
)
QWEN_TIMEOUT_GRACE_CYCLES = _safe_env_int(
    "AGENTSCOPE_QWEN_TIMEOUT_GRACE_CYCLES",
    1,
    min_value=0,
)
QWEN_GENERATOR_CLOSE_TIMEOUT_SECONDS = _safe_env_float(
    "AGENTSCOPE_QWEN_GENERATOR_CLOSE_TIMEOUT_SECONDS",
    10.0,
    min_value=0.1,
)
QWEN_WRITE_FORCE_COMPLETION_ENABLED = _safe_env_bool(
    "AGENTSCOPE_QWEN_WRITE_ENFORCE_COMPLETION",
    True,
)
QWEN_WRITE_FORCE_MIN_CONTENT_BYTES = _safe_env_int(
    "AGENTSCOPE_QWEN_WRITE_MIN_CONTENT_BYTES",
    64,
    min_value=1,
)
QWEN_WRITE_NO_GROWTH_MAX_CHUNKS = _safe_env_int(
    "AGENTSCOPE_QWEN_WRITE_NO_GROWTH_MAX_CHUNKS",
    80,
    min_value=5,
)
QWEN_WRITE_ARG_ONLY_GROWTH_MIN_BYTES = _safe_env_int(
    "AGENTSCOPE_QWEN_WRITE_ARG_ONLY_GROWTH_MIN_BYTES",
    24,
    min_value=1,
)
QWEN_WRITE_PATH_ONLY_STREAK_THRESHOLD = _safe_env_int(
    "AGENTSCOPE_QWEN_WRITE_PATH_ONLY_STREAK_THRESHOLD",
    40,
    min_value=5,
)
QWEN_WRITE_PATH_ONLY_MIN_SECONDS = _safe_env_float(
    "AGENTSCOPE_QWEN_WRITE_PATH_ONLY_MIN_SECONDS",
    2.5,
    min_value=0.5,
)
QWEN_WRITE_SHORT_ARGS_MAX_BYTES = _safe_env_int(
    "AGENTSCOPE_QWEN_WRITE_SHORT_ARGS_MAX_BYTES",
    32,
    min_value=4,
)
QWEN_WRITE_SHORT_ARGS_STREAK_THRESHOLD = _safe_env_int(
    "AGENTSCOPE_QWEN_WRITE_SHORT_ARGS_STREAK_THRESHOLD",
    40,
    min_value=5,
)
QWEN_WRITE_SHORT_ARGS_MIN_SECONDS = _safe_env_float(
    "AGENTSCOPE_QWEN_WRITE_SHORT_ARGS_MIN_SECONDS",
    2.5,
    min_value=0.5,
)
QWEN_WRITE_AUTO_REPAIR_MAX_RETRIES = _safe_env_int(
    "AGENTSCOPE_QWEN_WRITE_AUTO_REPAIR_MAX_RETRIES",
    0,
    min_value=0,
)
_SANDBOX_NOT_FOUND_MARKERS = (
    "sandbox not found",
    "sandbox was not found",
)


def _is_qwen_35_model(model_name: Optional[str]) -> bool:
    if not model_name:
        return False
    normalized = model_name.lower()
    return (
        "qwen3.5-plus" in normalized
        or "qwen-3.5-plus" in normalized
        or "qwen3.5-397b-a17b" in normalized
        or "qwen" in normalized
    )


def _maybe_enable_qwen_runtime_guards(
    enabled: bool,
    *,
    agent_run_id: str,
) -> bool:
    if enabled:
        return True
    resolved_model = get_agent_model_context()
    if not _is_qwen_35_model(resolved_model):
        return False

    logger.info(
        "Qwen runtime guards enabled from resolved model context for run %s: %s",
        agent_run_id,
        resolved_model,
    )
    return True


_TERMINAL_STATUS_PRIORITY: Dict[str, int] = {
    "running": 0,
    "completed": 1,
    "stopped": 2,
    "failed": 3,
}
_TERMINAL_AGENT_RUN_STATUSES = {"completed", "stopped", "failed"}


def _normalize_terminal_status(status: Optional[str]) -> str:
    normalized = str(status or "").strip().lower()
    if normalized == "error":
        return "failed"
    if normalized in _TERMINAL_STATUS_PRIORITY:
        return normalized
    return "running"


def _merge_terminal_status(
    current_status: Optional[str],
    candidate_status: Optional[str],
    *,
    error_present: bool = False,
) -> str:
    current = _normalize_terminal_status(current_status)
    candidate = _normalize_terminal_status(candidate_status)
    # User-visible stop is sticky. Later cleanup failures are internal warnings,
    # not a terminal-state upgrade from stopped -> failed.
    if current == "stopped" and candidate == "failed":
        return "stopped"
    if error_present and candidate == "completed":
        candidate = "failed"
    if _TERMINAL_STATUS_PRIORITY[candidate] >= _TERMINAL_STATUS_PRIORITY[current]:
        return candidate
    return current


def _finalize_local_terminal_status(
    current_status: Optional[str],
    *,
    external_stop_requested: bool,
    control_plane_failure: bool,
    error_message: Optional[str],
    saw_error_status_message: bool,
) -> str:
    """Resolve the worker-local terminal status before emitting terminal side effects."""
    resolved_status = _normalize_terminal_status(current_status)
    if control_plane_failure:
        return "failed"
    if external_stop_requested and resolved_status != "failed":
        return "stopped"
    if error_message or saw_error_status_message:
        resolved_status = _merge_terminal_status(
            resolved_status,
            "failed",
            error_present=bool(error_message),
        )
    if resolved_status == "running":
        return "completed"
    return resolved_status


async def _resolve_agent_run_db_client(client):
    if hasattr(client, "table"):
        return client
    from services.postgresql import DBConnection

    db_conn = DBConnection()
    return await db_conn.client


async def _fetch_persisted_agent_run_status(
    client,
    agent_run_id: str,
) -> Optional[str]:
    try:
        db_client = await _resolve_agent_run_db_client(client)
        query_result = await (
            db_client.table("agent_runs")
            .select("status")
            .eq("agent_run_id", agent_run_id)
            .execute()
        )
    except Exception as fetch_error:
        logger.warning(
            "Failed to fetch persisted agent run status for %s: %s",
            agent_run_id,
            fetch_error,
        )
        return None

    rows = getattr(query_result, "data", None) or []
    if not rows:
        return None
    return rows[0].get("status")


async def _merge_with_persisted_agent_run_status(
    client,
    agent_run_id: str,
    candidate_status: Optional[str],
    *,
    error_present: bool = False,
) -> str:
    persisted_status = await _fetch_persisted_agent_run_status(client, agent_run_id)
    resolved_status = _merge_terminal_status(
        persisted_status,
        candidate_status,
        error_present=error_present,
    )
    normalized_candidate = _normalize_terminal_status(candidate_status)
    normalized_persisted = _normalize_terminal_status(persisted_status)
    if (
        persisted_status is not None
        and normalized_persisted in _TERMINAL_AGENT_RUN_STATUSES
        and resolved_status != normalized_candidate
    ):
        logger.info(
            "Preserving stronger persisted run status for %s: persisted=%s candidate=%s resolved=%s",
            agent_run_id,
            normalized_persisted,
            normalized_candidate,
            resolved_status,
        )
    return resolved_status


async def _is_current_attempt_epoch(
    client,
    agent_run_id: str,
    *,
    attempt_id: str,
    execution_epoch: int,
) -> bool:
    normalized_agent_run_id = str(agent_run_id or "").strip()
    normalized_attempt_id = _normalize_optional_attempt_id(attempt_id)
    normalized_execution_epoch = _normalize_optional_execution_epoch(execution_epoch)
    if (
        not normalized_agent_run_id
        or normalized_attempt_id is None
        or normalized_execution_epoch is None
    ):
        return False

    try:
        db_client = await _resolve_agent_run_db_client(client)
        query_result = await (
            db_client.table("regular_run_attempts")
            .select("attempt_id, execution_epoch")
            .eq("agent_run_id", normalized_agent_run_id)
            .execute()
        )
    except Exception as fetch_error:
        logger.warning(
            "Failed to resolve current attempt epoch for %s attempt_id=%s execution_epoch=%s: %s",
            normalized_agent_run_id,
            normalized_attempt_id,
            normalized_execution_epoch,
            fetch_error,
        )
        return False

    rows = list(getattr(query_result, "data", None) or [])
    if not rows:
        return False

    def _sort_key(row: dict[str, Any]) -> int:
        try:
            return int(row.get("execution_epoch") or 0)
        except (TypeError, ValueError):
            return 0

    current_row = max((dict(row) for row in rows), key=_sort_key)
    current_attempt_id = _normalize_optional_attempt_id(current_row.get("attempt_id"))
    current_execution_epoch = _normalize_optional_execution_epoch(
        current_row.get("execution_epoch")
    )
    return (
        current_attempt_id == normalized_attempt_id
        and current_execution_epoch == normalized_execution_epoch
    )


async def _should_take_over_stale_run_lock(
    client,
    agent_run_id: str,
    *,
    attempt_id: Optional[str],
    execution_epoch: Optional[int],
) -> bool:
    normalized_attempt_id = _normalize_optional_attempt_id(attempt_id)
    normalized_execution_epoch = _normalize_optional_execution_epoch(execution_epoch)
    if (
        normalized_attempt_id is None
        or normalized_execution_epoch is None
        or normalized_execution_epoch < 2
    ):
        return False

    return await _is_current_attempt_epoch(
        client,
        agent_run_id,
        attempt_id=normalized_attempt_id,
        execution_epoch=normalized_execution_epoch,
    )


async def _persist_and_resolve_final_agent_run_status(
    client,
    agent_run_id: str,
    candidate_status: Optional[str],
    *,
    error_message: Optional[str] = None,
    attempt_id: Optional[str] = None,
    execution_epoch: Optional[int] = None,
) -> str:
    candidate_status_normalized = _normalize_terminal_status(candidate_status)
    normalized_attempt_id = _normalize_optional_attempt_id(attempt_id)
    normalized_execution_epoch = _normalize_optional_execution_epoch(execution_epoch)
    update_status_kwargs: dict[str, Any] = {"error": error_message}
    if normalized_attempt_id is not None and normalized_execution_epoch is not None:
        update_status_kwargs["attempt_id"] = normalized_attempt_id
        update_status_kwargs["execution_epoch"] = normalized_execution_epoch
    update_success = await update_agent_run_status(
        client,
        agent_run_id,
        candidate_status_normalized,
        **update_status_kwargs,
    )
    if not update_success:
        if (
            normalized_attempt_id is not None
            and normalized_execution_epoch is not None
            and not await _is_current_attempt_epoch(
                client,
                agent_run_id,
                attempt_id=normalized_attempt_id,
                execution_epoch=normalized_execution_epoch,
            )
        ):
            logger.warning(
                "Skipping force update for stale attempt terminal write agent_run_id=%s attempt_id=%s execution_epoch=%s candidate=%s",
                agent_run_id,
                normalized_attempt_id,
                normalized_execution_epoch,
                candidate_status_normalized,
            )
            return await _merge_with_persisted_agent_run_status(
                client,
                agent_run_id,
                None,
                error_present=bool(error_message),
            )
        persisted_status = await _fetch_persisted_agent_run_status(client, agent_run_id)
        merged_persisted_status = _merge_terminal_status(
            persisted_status,
            candidate_status_normalized,
            error_present=bool(error_message),
        )
        persisted_status_normalized = _normalize_terminal_status(persisted_status)
        if (
            persisted_status_normalized in _TERMINAL_AGENT_RUN_STATUSES
            and merged_persisted_status == persisted_status_normalized
        ):
            logger.warning(
                "Final status persistence fallback kept stronger persisted terminal truth "
                "for %s: persisted=%s candidate=%s",
                agent_run_id,
                persisted_status_normalized,
                candidate_status_normalized,
            )
        else:
            force_update_kwargs: dict[str, Any] = {"error": error_message}
            if (
                normalized_attempt_id is not None
                and normalized_execution_epoch is not None
            ):
                force_update_kwargs["attempt_id"] = normalized_attempt_id
                force_update_kwargs["execution_epoch"] = normalized_execution_epoch
            force_update_success = await _force_update_agent_run_status(
                client,
                agent_run_id,
                candidate_status_normalized,
                **force_update_kwargs,
            )
            logger.warning(
                "Final status persistence required force update for %s "
                "(candidate=%s initial_success=%s force_success=%s)",
                agent_run_id,
                candidate_status_normalized,
                update_success,
                force_update_success,
            )
    return await _merge_with_persisted_agent_run_status(
        client,
        agent_run_id,
        candidate_status,
        error_present=bool(error_message),
    )


def _should_emit_exception_error_status(final_status: Optional[str]) -> bool:
    return _normalize_terminal_status(final_status) == "failed"


def _resolve_terminal_error_message(
    final_status: Optional[str],
    error_message: Optional[str],
) -> Optional[str]:
    return (
        error_message if _normalize_terminal_status(final_status) == "failed" else None
    )


def _is_sandbox_not_found_text(value: str) -> bool:
    lowered = value.lower()
    return any(marker in lowered for marker in _SANDBOX_NOT_FOUND_MARKERS)


def _response_contains_sandbox_not_found(response: Dict[str, Any]) -> bool:
    try:
        serialized = json.dumps(response, ensure_ascii=False)
    except Exception:
        serialized = str(response)
    return _is_sandbox_not_found_text(serialized)


def _is_write_file_tool_name(tool_name: str) -> bool:
    normalized = str(tool_name or "").strip().lower()
    if normalized in {"write_file", "write-file", "writefile"}:
        return True
    return "write" in normalized and "file" in normalized


def _extract_write_file_tool_call_chunk(
    payload: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    if payload.get("type") != "assistant":
        return None

    metadata = payload.get("metadata")
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except Exception:
            return None
    if not isinstance(metadata, dict):
        return None
    if metadata.get("stream_status") != "tool_call_chunk":
        return None

    tool_calls = metadata.get("tool_calls")
    if not isinstance(tool_calls, list) or not tool_calls:
        return None

    best_chunk: Optional[Dict[str, Any]] = None
    best_score = -1
    for fallback_index, tool_call in enumerate(tool_calls):
        if not isinstance(tool_call, dict):
            continue

        trace_payload = tool_call.get("trace")
        if not isinstance(trace_payload, dict):
            trace_payload = {}

        function_payload = tool_call.get("function")
        if not isinstance(function_payload, dict):
            continue

        tool_name = function_payload.get("name")
        if not isinstance(tool_name, str) or not _is_write_file_tool_name(tool_name):
            continue

        raw_arguments = function_payload.get("arguments")
        if isinstance(raw_arguments, str):
            arguments_text = raw_arguments
        elif isinstance(raw_arguments, dict):
            try:
                arguments_text = json.dumps(raw_arguments, ensure_ascii=False)
            except Exception:
                arguments_text = str(raw_arguments)
        else:
            arguments_text = str(raw_arguments or "")
        arguments_length = len(arguments_text)
        arguments_hash = hashlib.sha1(arguments_text.encode("utf-8")).hexdigest()[:12]
        arguments_source = str(trace_payload.get("trace_args_source") or "parsed_input")

        parsed_arguments: Dict[str, Any] = {}
        if isinstance(raw_arguments, dict):
            parsed_arguments = dict(raw_arguments)
        elif isinstance(raw_arguments, str):
            try:
                loaded = json.loads(raw_arguments)
                if isinstance(loaded, dict):
                    parsed_arguments = loaded
            except Exception:
                parsed_arguments = {}

        path_hint: Optional[str] = None
        for key in ("path", "file_path", "target_file", "file"):
            candidate = parsed_arguments.get(key)
            if isinstance(candidate, str) and candidate.strip():
                path_hint = candidate.strip()
                break
        if path_hint is None and isinstance(raw_arguments, str):
            partial = extract_partial_write_file_input(arguments_text)
            partial_path = partial.get("path")
            if isinstance(partial_path, str) and partial_path.strip():
                path_hint = partial_path.strip()

        sanitized_write_input = sanitize_write_file_input(
            parsed_arguments,
            raw_arguments=arguments_text if isinstance(raw_arguments, str) else None,
        )
        path_value = sanitized_write_input.get("path")
        if not is_plausible_write_file_path(path_value):
            path_value = None
        content = sanitized_write_input.get("content")
        content_length_bytes = (
            len(content.encode("utf-8")) if isinstance(content, str) else 0
        )

        score = (
            (500000 if int(content_length_bytes) > 0 else 0)
            + (2 * int(content_length_bytes))
            + max(0, int(arguments_length))
            + (64 if isinstance(path_value, str) and path_value else 0)
        )
        if score <= best_score:
            continue

        tool_call_id = tool_call.get("id")
        if not tool_call_id:
            tool_call_id = f"{tool_name}:{tool_call.get('index', fallback_index)}"

        best_chunk = {
            "tool_call_id": str(tool_call_id),
            "tool_name": tool_name,
            "arguments_length": max(0, int(arguments_length)),
            "arguments_hash": arguments_hash,
            "arguments_source": arguments_source,
            "path": path_value,
            "path_hint": path_hint,
            "content": content if isinstance(content, str) else "",
            "content_length_bytes": int(content_length_bytes),
        }
        best_score = score

    return best_chunk


def _reset_qwen_write_arg_guard_state(state: Dict[str, Any]) -> None:
    state.clear()
    state.update(
        {
            "tool_call_id": None,
            "chunk_count": 0,
            "phase_started_at": None,
            "last_growth_at": None,
            "last_arguments_length": 0,
            "last_arguments_hash": "",
            "same_arguments_hash_streak": 0,
            "arguments_source": "parsed_input",
            "path": None,
            "path_hint": None,
            "content": "",
            "content_length_bytes": 0,
            "no_growth_streak": 0,
            "path_only_streak": 0,
            "short_args_streak": 0,
            "chunk_limit_warning_logged": False,
            "completion_forced": False,
        }
    )


def _check_qwen_write_arg_guard(
    response: Dict[str, Any],
    state: Dict[str, Any],
    *,
    now_monotonic: float,
) -> Optional[str]:
    status_payload = _extract_write_file_status_payload(response)
    if status_payload:
        _reset_qwen_write_arg_guard_state(state)
        return None

    chunk_payload = _extract_write_file_tool_call_chunk(response)
    if not chunk_payload:
        return None

    if not state:
        _reset_qwen_write_arg_guard_state(state)

    tool_call_id = str(chunk_payload["tool_call_id"])
    arguments_length = int(chunk_payload["arguments_length"])
    arguments_hash = str(chunk_payload.get("arguments_hash") or "")
    arguments_source = str(chunk_payload.get("arguments_source") or "parsed_input")
    chunk_content_value = chunk_payload.get("content")
    chunk_content_bytes = int(chunk_payload.get("content_length_bytes") or 0)

    if state.get("tool_call_id") != tool_call_id:
        _reset_qwen_write_arg_guard_state(state)
        state["tool_call_id"] = tool_call_id
        state["phase_started_at"] = now_monotonic
        state["last_growth_at"] = now_monotonic

    state["chunk_count"] = int(state.get("chunk_count") or 0) + 1
    last_arguments_length = int(state.get("last_arguments_length") or 0)
    arguments_growth_bytes = max(0, arguments_length - last_arguments_length)
    previous_content_bytes = int(state.get("content_length_bytes") or 0)
    previous_path = state.get("path")
    previous_path_hint = state.get("path_hint")
    previous_arguments_hash = str(state.get("last_arguments_hash") or "")

    if arguments_length > last_arguments_length:
        state["last_arguments_length"] = arguments_length
    if arguments_hash and arguments_hash == previous_arguments_hash:
        state["same_arguments_hash_streak"] = (
            int(state.get("same_arguments_hash_streak") or 0) + 1
        )
    else:
        state["same_arguments_hash_streak"] = 0
    state["last_arguments_hash"] = arguments_hash
    state["arguments_source"] = arguments_source

    path_value = chunk_payload.get("path")
    if is_plausible_write_file_path(path_value):
        normalized_path = str(path_value).strip()
        if (
            not isinstance(previous_path, str)
            or not previous_path
            or len(normalized_path) >= len(previous_path)
        ):
            state["path"] = normalized_path

    path_hint_value = chunk_payload.get("path_hint")
    if isinstance(path_hint_value, str):
        normalized_path_hint = path_hint_value.strip()
        if normalized_path_hint:
            if (
                not isinstance(previous_path_hint, str)
                or not previous_path_hint
                or len(normalized_path_hint) >= len(previous_path_hint)
            ):
                state["path_hint"] = normalized_path_hint

    if isinstance(chunk_content_value, str):
        normalized_chunk_bytes = max(0, int(chunk_content_bytes))
        if (
            normalized_chunk_bytes >= previous_content_bytes
            or not previous_content_bytes
        ):
            state["content"] = chunk_content_value
            state["content_length_bytes"] = normalized_chunk_bytes

    content_bytes = int(state.get("content_length_bytes") or 0)
    has_path_progress = not isinstance(previous_path, str) and isinstance(
        state.get("path"), str
    )
    has_content_progress = content_bytes > previous_content_bytes
    has_argument_growth = arguments_growth_bytes > 0
    effective_growth = has_content_progress or has_path_progress
    if (
        not effective_growth
        and has_argument_growth
        and (
            content_bytes >= QWEN_WRITE_FORCE_MIN_CONTENT_BYTES
            or arguments_growth_bytes >= QWEN_WRITE_ARG_ONLY_GROWTH_MIN_BYTES
        )
    ):
        # Treat sizable argument-length growth as progress even before content parses cleanly.
        effective_growth = True

    if effective_growth:
        state["last_growth_at"] = now_monotonic
        state["no_growth_streak"] = 0
    else:
        state["no_growth_streak"] = int(state.get("no_growth_streak") or 0) + 1

    has_path_hint = isinstance(state.get("path"), str) or isinstance(
        state.get("path_hint"), str
    )
    if has_path_hint and content_bytes <= 0:
        state["path_only_streak"] = int(state.get("path_only_streak") or 0) + 1
    else:
        state["path_only_streak"] = 0

    short_args_active = (
        content_bytes <= 0
        and not has_path_hint
        and arguments_length > 0
        and arguments_length <= QWEN_WRITE_SHORT_ARGS_MAX_BYTES
    )
    if short_args_active:
        state["short_args_streak"] = int(state.get("short_args_streak") or 0) + 1
    else:
        state["short_args_streak"] = 0

    phase_started_at = float(state.get("phase_started_at") or now_monotonic)
    last_growth_at = float(state.get("last_growth_at") or phase_started_at)
    phase_elapsed = max(0.0, now_monotonic - phase_started_at)
    stall_elapsed = max(0.0, now_monotonic - last_growth_at)
    chunk_count = int(state.get("chunk_count") or 0)
    if chunk_count == 1 or chunk_count % 10 == 0:
        logger.info(
            "Qwen write_file stream progress run_phase "
            "(tool_call_id=%s chunk_count=%s args_len=%s args_hash=%s args_source=%s path=%s path_hint=%s content_bytes=%s)",
            tool_call_id,
            chunk_count,
            arguments_length,
            arguments_hash,
            arguments_source,
            state.get("path"),
            state.get("path_hint"),
            state.get("content_length_bytes"),
        )

    if chunk_count > QWEN_WRITE_ARG_MAX_CHUNKS:
        warning_logged = bool(state.get("chunk_limit_warning_logged"))
        warning_interval = max(1, int(QWEN_WRITE_ARG_WARN_CHUNK_INTERVAL))
        if (not warning_logged) or (
            (chunk_count - QWEN_WRITE_ARG_MAX_CHUNKS) % warning_interval == 0
        ):
            logger.warning(
                "Qwen write_file chunk count crossed warning threshold "
                "(tool_call_id=%s chunk_count=%s threshold=%s args_len=%s content_bytes=%s no_growth_streak=%s)",
                tool_call_id,
                chunk_count,
                QWEN_WRITE_ARG_MAX_CHUNKS,
                arguments_length,
                content_bytes,
                state.get("no_growth_streak"),
            )
            state["chunk_limit_warning_logged"] = True

    no_growth_streak = int(state.get("no_growth_streak") or 0)
    effective_stall_timeout = _resolve_qwen_write_stall_timeout_seconds(state)
    if (
        phase_elapsed > QWEN_WRITE_ARG_MAX_SECONDS
        and no_growth_streak >= QWEN_WRITE_NO_GROWTH_MAX_CHUNKS
    ):
        return (
            "Qwen write_file tool-call stream exceeded safety time budget without effective progress "
            f"({phase_elapsed:.1f}s > {QWEN_WRITE_ARG_MAX_SECONDS:.1f}s, "
            f"no_growth_streak={no_growth_streak})."
        )
    if (
        stall_elapsed > effective_stall_timeout
        and no_growth_streak >= QWEN_WRITE_NO_GROWTH_MAX_CHUNKS
    ):
        if _is_qwen_write_short_args_loop(state, now_monotonic=now_monotonic):
            return (
                "Qwen write_file tool-call stream is unreconstructable "
                "(short-args loop without path/content progress) "
                f"(args_len={arguments_length}, args_hash={arguments_hash}, "
                f"short_args_streak={state.get('short_args_streak')})."
            )
        return (
            "Qwen write_file tool-call stream stalled without effective progress "
            f"({stall_elapsed:.1f}s > {effective_stall_timeout:.1f}s, "
            f"no_growth_streak={no_growth_streak}, content_bytes={content_bytes}, "
            f"effective_stall_timeout={effective_stall_timeout:.1f}s)."
        )
    return None


def _is_qwen_write_path_only_loop(
    state: Dict[str, Any],
    *,
    now_monotonic: float,
) -> bool:
    if not _is_qwen_write_contentless_loop(state, now_monotonic=now_monotonic):
        return False
    return _resolve_qwen_write_retry_path_hint(state) is not None


def _is_qwen_write_contentless_loop(
    state: Dict[str, Any],
    *,
    now_monotonic: float,
) -> bool:
    if not _is_qwen_write_arg_phase_active(state):
        return False

    content_bytes = int(state.get("content_length_bytes") or 0)
    if content_bytes > 0:
        return False

    streak = int(state.get("path_only_streak") or 0)
    if streak < QWEN_WRITE_PATH_ONLY_STREAK_THRESHOLD:
        return False

    phase_started_at = float(state.get("phase_started_at") or now_monotonic)
    phase_elapsed = max(0.0, now_monotonic - phase_started_at)
    if phase_elapsed < QWEN_WRITE_PATH_ONLY_MIN_SECONDS:
        return False

    return True


def _is_qwen_write_short_args_loop(
    state: Dict[str, Any],
    *,
    now_monotonic: float,
) -> bool:
    if not _is_qwen_write_arg_phase_active(state):
        return False
    if isinstance(state.get("path"), str) or isinstance(state.get("path_hint"), str):
        return False

    content_bytes = int(state.get("content_length_bytes") or 0)
    if content_bytes > 0:
        return False

    short_args_streak = int(state.get("short_args_streak") or 0)
    if short_args_streak < QWEN_WRITE_SHORT_ARGS_STREAK_THRESHOLD:
        return False

    same_hash_streak = int(state.get("same_arguments_hash_streak") or 0)
    if same_hash_streak < max(1, QWEN_WRITE_SHORT_ARGS_STREAK_THRESHOLD // 2):
        return False

    arguments_length = int(state.get("last_arguments_length") or 0)
    if arguments_length <= 0 or arguments_length > QWEN_WRITE_SHORT_ARGS_MAX_BYTES:
        return False

    phase_started_at = float(state.get("phase_started_at") or now_monotonic)
    phase_elapsed = max(0.0, now_monotonic - phase_started_at)
    return phase_elapsed >= QWEN_WRITE_SHORT_ARGS_MIN_SECONDS


def _resolve_qwen_write_retry_path_hint(state: Dict[str, Any]) -> Optional[str]:
    canonical_path = state.get("path")
    if is_plausible_write_file_path(canonical_path):
        return str(canonical_path).strip()

    hint = state.get("path_hint")
    if not isinstance(hint, str):
        return None
    clean_hint = hint.strip()
    if not clean_hint:
        return None
    if len(clean_hint) > 1024:
        return None
    if any(char in clean_hint for char in ("\n", "\r", "\t", '"', "'")):
        return None
    if "://" in clean_hint:
        return None
    if not is_plausible_write_file_path(clean_hint):
        return None
    return clean_hint


def _should_attempt_qwen_write_path_only_retry(
    state: Dict[str, Any],
    *,
    now_monotonic: float,
    attempts_made: int,
) -> bool:
    if QWEN_WRITE_AUTO_REPAIR_MAX_RETRIES <= 0:
        return False
    if attempts_made >= QWEN_WRITE_AUTO_REPAIR_MAX_RETRIES:
        return False
    return _is_qwen_write_path_only_loop(state, now_monotonic=now_monotonic)


def _build_qwen_write_path_only_retry_instruction(
    *,
    path: Optional[str],
    attempt: int,
    path_hint: Optional[str] = None,
) -> str:
    clean_path = path.strip() if isinstance(path, str) else ""
    clean_hint = path_hint.strip() if isinstance(path_hint, str) else ""

    target_section: str
    path_field_rule: str
    if clean_path:
        target_section = f"Target path: {clean_path}\n\n"
        path_field_rule = '1) "path": use the exact target path above\n'
    elif clean_hint:
        target_section = (
            "Path hint from previous failed call "
            f"(may be truncated): {clean_hint}\n\n"
        )
        path_field_rule = '1) "path": provide the intended FULL final path (do not send partial fragments)\n'
    else:
        target_section = ""
        path_field_rule = (
            '1) "path": provide the intended FULL final path for the output file\n'
        )

    return (
        "RETRY REQUIRED: the previous write_file call did not include valid complete file content.\n"
        f"Retry attempt: {attempt}\n"
        f"{target_section}"
        "You MUST call write_file exactly once with BOTH fields in the same tool call:\n"
        f"{path_field_rule}"
        '2) "content": provide the complete final file contents (full HTML, no placeholders)\n\n'
        "Output a valid JSON object in tool arguments with properly escaped quotes/newlines.\n"
        "Do NOT emit path-only write_file arguments. Do NOT split content across multiple write_file calls."
    )


def _is_qwen_write_arg_phase_active(state: Dict[str, Any]) -> bool:
    return bool(state.get("tool_call_id")) and state.get("phase_started_at") is not None


def _resolve_qwen_write_idle_timeout_seconds(state: Dict[str, Any]) -> float:
    content_bytes = int(state.get("content_length_bytes") or 0)
    if content_bytes > 0:
        return QWEN_WRITE_IDLE_TIMEOUT_WITH_CONTENT_SECONDS
    return QWEN_WRITE_IDLE_TIMEOUT_SECONDS


def _resolve_qwen_write_stall_timeout_seconds(state: Dict[str, Any]) -> float:
    content_bytes = int(state.get("content_length_bytes") or 0)
    if content_bytes > 0:
        return QWEN_WRITE_ARG_STALL_WITH_CONTENT_SECONDS
    return QWEN_WRITE_ARG_STALL_SECONDS


def _check_qwen_write_idle_guard(
    state: Dict[str, Any],
    *,
    now_monotonic: float,
    last_response_monotonic: float,
) -> Optional[str]:
    if not _is_qwen_write_arg_phase_active(state):
        return None

    effective_timeout = _resolve_qwen_write_idle_timeout_seconds(state)
    idle_elapsed = max(0.0, now_monotonic - last_response_monotonic)
    if idle_elapsed <= effective_timeout:
        return None

    return (
        "Qwen write_file tool-call stream produced no new chunks "
        f"({idle_elapsed:.1f}s > {effective_timeout:.1f}s, "
        f"content_bytes={int(state.get('content_length_bytes') or 0)}, "
        f"effective_timeout={effective_timeout:.1f}s)."
    )


def _has_qwen_write_payload_for_forced_completion(state: Dict[str, Any]) -> bool:
    if state.get("completion_forced"):
        return False
    path = state.get("path")
    content = state.get("content")
    if not is_plausible_write_file_path(path):
        return False
    if not isinstance(content, str):
        return False
    content_bytes = state.get("content_length_bytes")
    if not isinstance(content_bytes, int):
        content_bytes = len(content.encode("utf-8"))
    return content_bytes >= QWEN_WRITE_FORCE_MIN_CONTENT_BYTES


async def _force_qwen_write_completion(
    *,
    project_id: str,
    state: Dict[str, Any],
    agent_run_id: str,
    trigger_reason: str,
    shadow_clone_run_id: Optional[str] = None,
    strict_sandbox: bool = False,
) -> tuple[bool, Optional[str]]:
    if not QWEN_WRITE_FORCE_COMPLETION_ENABLED:
        return False, "forced completion disabled"
    if not _has_qwen_write_payload_for_forced_completion(state):
        return False, "write payload not ready for forced completion"

    path = str(state.get("path") or "")
    content = str(state.get("content") or "")

    try:
        from agentscope_integration.adapters.thread_manager_adapter import (
            ThreadManagerAdapter,
        )
        from agent.tools.sandbox_code_tool import SandboxCodeTool

        tool = SandboxCodeTool(
            project_id=project_id,
            thread_manager=ThreadManagerAdapter(db_client=await db.client),
            shadow_clone_run_id=shadow_clone_run_id,
            strict_sandbox=strict_sandbox,
        )
        result = await tool.write_file(path=path, content=content)
    except Exception as force_error:
        return False, f"forced completion call failed: {force_error}"

    if not result.success:
        output_payload = result.output if isinstance(result.output, dict) else {}
        error_message = (
            output_payload.get("error")
            or "write_file tool returned unsuccessful result"
        )
        return False, str(error_message)

    state["completion_forced"] = True
    logger.warning(
        "Forced qwen write_file completion succeeded for run=%s path=%s content_bytes=%s reason=%s",
        agent_run_id,
        path,
        state.get("content_length_bytes"),
        trigger_reason,
    )
    return True, None


async def _close_agent_generator(
    agent_gen: Any,
    *,
    agent_run_id: str,
) -> None:
    if agent_gen is None:
        return
    closer = getattr(agent_gen, "aclose", None)
    if not callable(closer):
        return
    try:
        await asyncio.wait_for(
            closer(),
            timeout=QWEN_GENERATOR_CLOSE_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "Agent generator close timed out for run %s after %.1fs",
            agent_run_id,
            QWEN_GENERATOR_CLOSE_TIMEOUT_SECONDS,
        )
    except Exception as close_error:
        logger.debug(
            "Ignoring agent generator close error for run %s: %s",
            agent_run_id,
            close_error,
        )


def _should_emit_qwen_write_chunk(
    response: Dict[str, Any],
    emit_state: Dict[str, Dict[str, float]],
    *,
    now_monotonic: float,
) -> bool:
    status_payload = _extract_write_file_status_payload(response)
    if status_payload:
        emit_state.clear()
        return True

    chunk_payload = _extract_write_file_tool_call_chunk(response)
    if not chunk_payload:
        return True

    tool_call_id = str(chunk_payload["tool_call_id"])
    arguments_length = float(chunk_payload["arguments_length"])
    tracker = emit_state.get(tool_call_id)
    if tracker is None:
        emit_state.clear()
        emit_state[tool_call_id] = {
            "last_arguments_length": arguments_length,
            "last_emit_at": now_monotonic,
        }
        return True

    growth = max(0.0, arguments_length - tracker["last_arguments_length"])
    elapsed_ms = (now_monotonic - tracker["last_emit_at"]) * 1000.0
    if growth >= float(QWEN_WRITE_CHUNK_MIN_GROWTH_BYTES) or elapsed_ms >= float(
        QWEN_WRITE_CHUNK_MIN_EMIT_INTERVAL_MS
    ):
        tracker["last_arguments_length"] = max(
            tracker["last_arguments_length"],
            arguments_length,
        )
        tracker["last_emit_at"] = now_monotonic
        return True

    return False


def _response_stream_contains_tool_execution(
    responses: list,
) -> bool:
    """Return True if any response indicates a tool was actually executed.

    Matches both SSE-format status messages (type='status' with nested
    content.status_type) and plain dict payloads that carry function_name +
    status_type fields at any nesting level.
    """
    for raw in responses:
        try:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            if isinstance(raw, str):
                parsed = json.loads(raw)
            elif isinstance(raw, dict):
                parsed = raw
            else:
                continue
        except Exception:
            continue

        # ── Look for status_type in the parsed payload ────────────────
        # Path 1: payload.content.status_type (SSE-format status message)
        content = parsed.get("content") if isinstance(parsed, dict) else None
        if isinstance(content, str):
            try:
                inner = json.loads(content)
                if isinstance(inner, dict) and inner.get("status_type") in (
                    "tool_started",
                    "tool_completed",
                    "tool_failed",
                ):
                    return True
            except Exception:
                pass

        # Path 2: payload.status_type at top level (log-format dict)
        if isinstance(parsed, dict) and parsed.get("status_type") in (
            "tool_started",
            "tool_completed",
            "tool_failed",
        ):
            return True

        # Path 3: full-text scan for tool status keywords
        # (catches messages where the format is unexpected)
        raw_str = (
            raw
            if isinstance(raw, str)
            else (raw.decode("utf-8") if isinstance(raw, bytes) else json.dumps(raw))
        )
        if any(
            kw in raw_str
            for kw in (
                '"status_type":"tool_started"',
                '"status_type":"tool_completed"',
                '"status_type":"tool_failed"',
            )
        ):
            return True

    return False


def _extract_write_file_status_payload(
    payload: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    if payload.get("type") != "status":
        return None

    content = payload.get("content")
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except Exception:
            return None
    if not isinstance(content, dict):
        return None
    if content.get("function_name") != "write_file":
        return None
    status_type = content.get("status_type")
    if status_type not in {"tool_started", "tool_completed", "tool_failed"}:
        return None
    return content


def _extract_write_file_path_from_status(
    content_payload: Dict[str, Any],
) -> Optional[str]:
    arguments = content_payload.get("arguments")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except Exception:
            return None
    if isinstance(arguments, dict):
        path = arguments.get("path")
        if isinstance(path, str) and path.strip():
            return path
    return None


def _parse_json_object_value(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except Exception:
            return {}
        if isinstance(parsed, dict):
            return dict(parsed)
    return {}


def _is_shadow_clone_v2_final_assistant_response(
    response: Dict[str, Any],
    *,
    effective_shadow_clone_mode: ShadowCloneMode,
) -> bool:
    """Return true for the V2 main-agent final answer that must appear in chat."""
    if not _is_shadow_clone_v2_user_facing_main_agent_response(
        response,
        effective_shadow_clone_mode=effective_shadow_clone_mode,
    ):
        return False

    metadata = _parse_json_object_value(response.get("metadata"))
    return metadata.get("phase_reason") == "shadow_clone_v2_final_output"


def _is_shadow_clone_v2_user_facing_main_agent_response(
    response: Dict[str, Any],
    *,
    effective_shadow_clone_mode: ShadowCloneMode,
) -> bool:
    """Return true for V2 main-agent natural-language assistant messages."""
    if effective_shadow_clone_mode == ShadowCloneMode.OFF:
        return False
    if response.get("type") != "assistant":
        return False
    if not response.get("message_id") or not response.get("thread_id"):
        return False
    content = response.get("content")
    if content is None or not str(content).strip():
        return False

    metadata = _parse_json_object_value(response.get("metadata"))
    return (
        metadata.get("activity_owner") == "main_agent"
        and metadata.get("stream_status") == "complete"
        and metadata.get("phase_reason")
        in {
            "shadow_clone_v2_main_agent_planning_output",
            "shadow_clone_v2_final_output",
        }
    )


async def _persist_shadow_clone_v2_final_assistant_message(
    client: Any,
    response: Dict[str, Any],
) -> None:
    """Persist the V2 main-agent final answer as a canonical thread message."""
    await _persist_shadow_clone_v2_main_agent_assistant_message(client, response)


def _coerce_shadow_clone_v2_message_timestamp(value: Any, fallback: datetime) -> datetime:
    """Return a database-ready timestamp for persisted V2 chat messages."""
    if isinstance(value, datetime):
        return value
    if isinstance(value, str) and value.strip():
        normalized = value.strip()
        try:
            return datetime.fromisoformat(normalized.replace("Z", "+00:00"))
        except ValueError:
            return fallback
    return fallback


async def _persist_shadow_clone_v2_main_agent_assistant_message(
    client: Any,
    response: Dict[str, Any],
) -> None:
    """Persist a V2 main-agent natural-language answer as a thread message."""
    now = datetime.now(timezone.utc)
    content = response.get("content")
    metadata = response.get("metadata")
    metadata_obj = _parse_json_object_value(metadata)
    source_message_id = str(response["message_id"])
    try:
        canonical_message_id = str(uuid.UUID(source_message_id))
    except (TypeError, ValueError):
        canonical_message_id = str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                f"shadow-clone-v2-main-agent-message:{source_message_id}",
            )
        )
        metadata_obj.setdefault(
            "shadow_clone_v2_source_message_id",
            source_message_id,
        )
    row = {
        "message_id": canonical_message_id,
        "thread_id": str(response["thread_id"]),
        "project_id": str(response["project_id"]),
        "type": "assistant",
        "role": "assistant",
        "is_llm_message": bool(response.get("is_llm_message", True)),
        "content": (
            content
            if isinstance(content, str)
            else json.dumps(content, ensure_ascii=False)
        ),
        "metadata": json.dumps(metadata_obj, ensure_ascii=False),
        "created_at": _coerce_shadow_clone_v2_message_timestamp(
            response.get("created_at"),
            now,
        ),
        "updated_at": _coerce_shadow_clone_v2_message_timestamp(
            response.get("updated_at") or response.get("created_at"),
            now,
        ),
    }
    insert_result = client.table("messages").insert(row)
    if inspect.isawaitable(insert_result):
        await insert_result
        return
    execute = getattr(insert_result, "execute", None)
    if callable(execute):
        execute_result = execute()
        if inspect.isawaitable(execute_result):
            await execute_result
        return
    raise TypeError("messages insert did not return an awaitable or executable query")


async def _append_missing_write_file_terminal_status(
    *,
    response_list_key: str,
    response_channel: str,
    agent_run_id: str,
    thread_id: str,
    final_status: str,
    enabled: bool,
) -> str | None:
    if not enabled or final_status not in {"completed", "failed", "stopped"}:
        return None

    try:
        recent_responses = await redis.lrange(response_list_key, -500, -1)
    except Exception as fetch_error:
        logger.warning(
            f"Failed to inspect write_file statuses for compensation on {response_list_key}: {fetch_error}"
        )
        return None

    open_write_paths: list[str] = []
    for raw in recent_responses:
        try:
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            parsed = json.loads(raw)
        except Exception:
            continue

        content_payload = _extract_write_file_status_payload(parsed)
        if not content_payload:
            continue

        status_type = content_payload["status_type"]
        if status_type == "tool_started":
            tracked_path = (
                _extract_write_file_path_from_status(content_payload)
                or "/workspace/unknown"
            )
            open_write_paths.append(tracked_path)
            continue

        if status_type in {"tool_completed", "tool_failed"} and open_write_paths:
            open_write_paths.pop()

    if not open_write_paths:
        return None

    now = datetime.now(timezone.utc).isoformat()
    path = open_write_paths[-1]
    compensation_content = {
        "role": "assistant",
        "status_type": "tool_failed",
        "function_name": "write_file",
        "arguments": json.dumps({"path": path}, ensure_ascii=False),
        "xml_tag_name": "write_file",
        "tool_index": 0,
        "message": f"Run ended with status '{final_status}' before write_file finalized.",
    }
    payload = {
        "sequence": int(time.time() * 1000),
        "message_id": None,
        "thread_id": thread_id,
        "type": "status",
        "is_llm_message": False,
        "content": json.dumps(compensation_content, ensure_ascii=False),
        "metadata": json.dumps({"thread_run_id": agent_run_id}, ensure_ascii=False),
        "created_at": now,
        "updated_at": now,
    }

    payload_json = json.dumps(payload, ensure_ascii=False)

    try:
        push_succeeded = await _push_response_with_retry(
            response_list_key,
            response_channel,
            payload_json,
            agent_run_id=agent_run_id,
        )
    except RuntimeError as push_error:
        logger.warning(
            "Failed to append write_file terminal compensation for run=%s: %s",
            agent_run_id,
            push_error,
        )
        push_succeeded = False
    if push_succeeded:
        logger.info(
            "Appended write_file terminal compensation status for run=%s path=%s final_status=%s",
            agent_run_id,
            path,
            final_status,
        )
    else:
        logger.warning(
            "Failed to append write_file terminal compensation for run=%s; continuing cleanup",
            agent_run_id,
        )
    return payload_json


async def _write_response_with_retry(
    response_list_key: str,
    response_channel: str,
    payload_json: str,
    *,
    agent_run_id: str,
    publish_message: str,
) -> bool:
    """Push one response to Redis list + pubsub with bounded retry/timeout."""
    attempts = max(1, REDIS_WRITE_RETRY_ATTEMPTS + 1)
    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        try:
            await asyncio.wait_for(
                redis.eval_script(
                    _PUSH_RESPONSE_WITH_NOTIFY_LUA,
                    keys=[response_list_key, response_channel],
                    args=[
                        payload_json,
                        str(REDIS_RESPONSE_LIST_INITIAL_TTL),
                        publish_message,
                    ],
                    timeout=REDIS_WRITE_TIMEOUT_SECONDS,
                ),
                timeout=REDIS_WRITE_TIMEOUT_SECONDS,
            )
            return True
        except Exception as redis_error:
            last_error = redis_error
            if attempt >= attempts:
                break
            backoff = min(0.5 * attempt, 2.0)
            logger.warning(
                "Redis push failed for %s (attempt %s/%s): %s",
                agent_run_id,
                attempt,
                attempts,
                redis_error,
            )
            await asyncio.sleep(backoff)

    message = (
        f"Redis push failed for {agent_run_id} after {attempts} attempts: {last_error}"
    )
    logger.error(message)
    raise RuntimeError(message) from last_error


async def _push_response_with_retry(
    response_list_key: str,
    response_channel: str,
    payload_json: str,
    *,
    agent_run_id: str,
) -> bool:
    return await _write_response_with_retry(
        response_list_key,
        response_channel,
        payload_json,
        agent_run_id=agent_run_id,
        publish_message="new",
    )


async def _mirror_response_without_notify_with_retry(
    response_list_key: str,
    payload_json: str,
    *,
    agent_run_id: str,
) -> bool:
    return await _write_response_with_retry(
        response_list_key,
        f"agent_run:{agent_run_id}:new_response",
        payload_json,
        agent_run_id=agent_run_id,
        publish_message="",
    )


async def _get_final_responses_best_effort(
    response_list_key: str,
    *,
    agent_run_id: str,
    fallback_responses: list[Any] | None = None,
) -> list[Any]:
    """Fetch final responses without letting replay failures change terminal status."""
    fallback = list(fallback_responses or [])
    if fallback:
        logger.debug(
            "Using %s in-memory responses for %s without Redis replay",
            len(fallback),
            agent_run_id,
        )
        return fallback

    try:
        responses = await redis.lrange(response_list_key, 0, -1)
    except Exception as fetch_error:
        logger.warning(
            "Failed to fetch final responses from Redis for %s; using %s fallback responses: %s",
            agent_run_id,
            len(fallback),
            fetch_error,
        )
        return fallback

    logger.info("Fetched %s responses from Redis for %s", len(responses), agent_run_id)
    return list(responses)


async def _cleanup_redis_response_list(
    agent_run_id: str,
    *,
    execution_epoch: Optional[int] = None,
    response_list_key: Optional[str] = None,
):
    """Set TTL on the Redis response list."""
    resolved_response_list_key = response_list_key or _build_response_list_key(
        agent_run_id,
        execution_epoch=execution_epoch,
    )
    try:
        # 销毁Redis响应列表
        await redis.expire(resolved_response_list_key, REDIS_RESPONSE_LIST_TTL)
        logger.debug(
            f"Set TTL ({REDIS_RESPONSE_LIST_TTL}s) on response list: {resolved_response_list_key}"
        )
    except Exception as e:
        logger.warning(
            f"Failed to set TTL on response list {resolved_response_list_key}: {str(e)}"
        )


async def _ensure_terminal_status_message(
    response_list_key: str,
    response_channel: str,
    final_status: str,
    error_message: Optional[str] = None,
) -> str | None:
    """Ensure a terminal status message exists in the Redis response list."""
    if final_status not in ("completed", "failed", "stopped"):
        return None

    existing_terminal_status: str | None = None
    try:
        recent_responses = await redis.lrange(response_list_key, -20, -1)
        for raw in recent_responses:
            try:
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8")
                parsed = json.loads(raw)
            except Exception:
                continue
            parsed_status = _normalize_terminal_status(parsed.get("status"))
            if parsed.get("type") == "status" and parsed_status in (
                "completed",
                "failed",
                "stopped",
            ):
                existing_terminal_status = parsed_status
    except Exception as e:
        logger.warning(
            f"Failed to check terminal status messages for {response_list_key}: {e}"
        )

    if existing_terminal_status == final_status:
        return None

    if existing_terminal_status is not None:
        logger.info(
            "Appending corrective terminal status for %s: existing=%s final=%s",
            response_list_key,
            existing_terminal_status,
            final_status,
        )

    status_message = {"type": "status", "status": final_status}
    if final_status == "completed":
        status_message["message"] = "Agent run completed successfully"
    else:
        status_message["message"] = (
            error_message or f"Run ended with status: {final_status}"
        )

    payload_json = json.dumps(status_message, ensure_ascii=False)

    try:
        push_succeeded = await _push_response_with_retry(
            response_list_key,
            response_channel,
            payload_json,
            agent_run_id=response_list_key.split(":")[1],
        )
    except RuntimeError as push_error:
        logger.warning(
            "Failed to append terminal status message for %s: %s",
            response_list_key,
            push_error,
        )
        push_succeeded = False
    if push_succeeded:
        logger.info(
            f"Appended terminal status message ({final_status}) to {response_list_key}"
        )
    else:
        logger.warning(
            "Failed to append terminal status message for %s; continuing cleanup",
            response_list_key,
        )
    return payload_json


async def update_agent_run_status(
    client,
    agent_run_id: str,
    status: str,
    error: Optional[str] = None,
    attempt_id: Optional[str] = None,
    execution_epoch: Optional[int] = None,
) -> bool:
    """
    Centralized function to update agent run status.
    Returns True if update was successful.
    """
    try:
        candidate_status = _normalize_terminal_status(status)
        normalized_error = error
        if normalized_error:
            if isinstance(normalized_error, list):
                normalized_error = str(normalized_error)
            elif not isinstance(normalized_error, str):
                normalized_error = str(normalized_error)
        normalized_attempt_id = _normalize_optional_attempt_id(attempt_id)
        normalized_execution_epoch = _normalize_optional_execution_epoch(
            execution_epoch
        )
        if (
            normalized_attempt_id is not None
            and normalized_execution_epoch is not None
            and not await _is_current_attempt_epoch(
                client,
                agent_run_id,
                attempt_id=normalized_attempt_id,
                execution_epoch=normalized_execution_epoch,
            )
        ):
            logger.warning(
                "Rejected stale attempt terminal status write agent_run_id=%s attempt_id=%s execution_epoch=%s candidate=%s",
                agent_run_id,
                normalized_attempt_id,
                normalized_execution_epoch,
                candidate_status,
            )
            return False

        # 重试3次
        for retry in range(3):
            try:
                current_status = await _fetch_persisted_agent_run_status(
                    client,
                    agent_run_id,
                )
                resolved_status = _merge_terminal_status(
                    current_status,
                    candidate_status,
                    error_present=bool(normalized_error),
                )
                current_normalized = _normalize_terminal_status(current_status)
                if (
                    current_status is not None
                    and current_normalized in _TERMINAL_AGENT_RUN_STATUSES
                    and resolved_status == current_normalized
                    and resolved_status != candidate_status
                ):
                    logger.info(
                        "Skipping weaker terminal status update for %s: current=%s candidate=%s",
                        agent_run_id,
                        current_normalized,
                        candidate_status,
                    )
                    return True

                update_data = {
                    "status": resolved_status,
                    "completed_at": datetime.now(
                        timezone.utc
                    ),  # 直接传递 datetime 对象，而不是字符串
                }
                if normalized_error:
                    update_data["error"] = normalized_error

                # 确保 client 是已初始化的数据库客户端
                db_client = await _resolve_agent_run_db_client(client)
                update_query = db_client.table("agent_runs").eq(
                    "agent_run_id", agent_run_id
                )
                if current_status is not None:
                    update_query = update_query.eq("status", current_status)

                update_result = await update_query.update(update_data)
                if hasattr(update_result, "data") and update_result.data:
                    logger.info(
                        f"Successfully updated agent run {agent_run_id} status to '{resolved_status}' (retry {retry})"
                    )
                    return True
                else:
                    latest_status = await _fetch_persisted_agent_run_status(
                        client,
                        agent_run_id,
                    )
                    latest_normalized = _normalize_terminal_status(latest_status)
                    if latest_status is not None and latest_status != current_status:
                        logger.info(
                            "Detected concurrent agent run status change for %s: expected=%s latest=%s candidate=%s",
                            agent_run_id,
                            current_status,
                            latest_normalized,
                            candidate_status,
                        )
                        if (
                            latest_normalized in _TERMINAL_AGENT_RUN_STATUSES
                            and _merge_terminal_status(
                                latest_status,
                                candidate_status,
                                error_present=bool(normalized_error),
                            )
                            == latest_normalized
                            and latest_normalized != candidate_status
                        ):
                            logger.info(
                                "Concurrent stronger terminal status already persisted for %s: latest=%s candidate=%s",
                                agent_run_id,
                                latest_normalized,
                                candidate_status,
                            )
                            return True
                        if retry < 2:
                            continue
                    logger.warning(
                        f"Database update returned no data for agent run {agent_run_id} on retry {retry}: {update_result}"
                    )
                    if retry == 2:  # Last retry
                        logger.error(
                            f"Failed to update agent run status after all retries: {agent_run_id}"
                        )
                        return False
            except Exception as db_error:
                logger.error(
                    f"Database error on retry {retry} updating status for {agent_run_id}: {str(db_error)}"
                )
                if retry < 2:  # Not the last retry yet
                    await asyncio.sleep(0.5 * (2**retry))  # Exponential backoff
                else:
                    logger.error(
                        f"Failed to update agent run status after all retries: {agent_run_id}",
                        exc_info=True,
                    )
                    return False
    except Exception as e:
        logger.error(
            f"Unexpected error updating agent run status for {agent_run_id}: {str(e)}",
            exc_info=True,
        )
        return False
    return False


async def _force_update_agent_run_status(
    client,
    agent_run_id: str,
    status: str,
    error: Optional[str] = None,
    attempt_id: Optional[str] = None,
    execution_epoch: Optional[int] = None,
) -> bool:
    try:
        candidate_status = _normalize_terminal_status(status)
        normalized_error = error
        if normalized_error:
            if isinstance(normalized_error, list):
                normalized_error = str(normalized_error)
            elif not isinstance(normalized_error, str):
                normalized_error = str(normalized_error)
        normalized_attempt_id = _normalize_optional_attempt_id(attempt_id)
        normalized_execution_epoch = _normalize_optional_execution_epoch(
            execution_epoch
        )
        if (
            normalized_attempt_id is not None
            and normalized_execution_epoch is not None
            and not await _is_current_attempt_epoch(
                client,
                agent_run_id,
                attempt_id=normalized_attempt_id,
                execution_epoch=normalized_execution_epoch,
            )
        ):
            logger.warning(
                "Rejected stale forced terminal status write agent_run_id=%s attempt_id=%s execution_epoch=%s candidate=%s",
                agent_run_id,
                normalized_attempt_id,
                normalized_execution_epoch,
                candidate_status,
            )
            return False

        current_status = await _fetch_persisted_agent_run_status(client, agent_run_id)
        resolved_status = _merge_terminal_status(
            current_status,
            candidate_status,
            error_present=bool(normalized_error),
        )
        current_normalized = _normalize_terminal_status(current_status)
        if (
            current_normalized in _TERMINAL_AGENT_RUN_STATUSES
            and resolved_status == current_normalized
            and resolved_status != candidate_status
        ):
            logger.info(
                "Skipping weaker forced terminal status update for %s: current=%s candidate=%s",
                agent_run_id,
                current_normalized,
                candidate_status,
            )
            return True

        update_data = {
            "status": resolved_status,
            "completed_at": datetime.now(timezone.utc),
        }
        if normalized_error:
            update_data["error"] = normalized_error

        db_client = await _resolve_agent_run_db_client(client)
        update_result = await (
            db_client.table("agent_runs")
            .eq("agent_run_id", agent_run_id)
            .update(update_data)
        )
        if hasattr(update_result, "data") and update_result.data:
            logger.info(
                "Force-updated agent run %s status to '%s'",
                agent_run_id,
                resolved_status,
            )
            return True

        latest_status = await _fetch_persisted_agent_run_status(client, agent_run_id)
        latest_normalized = _normalize_terminal_status(latest_status)
        if latest_normalized == resolved_status:
            logger.info(
                "Force update observed desired terminal status already persisted for %s: %s",
                agent_run_id,
                latest_normalized,
            )
            return True

        logger.error(
            "Force update returned no data for agent run %s: latest=%s desired=%s result=%s",
            agent_run_id,
            latest_normalized,
            resolved_status,
            update_result,
        )
        return False
    except Exception as error:
        logger.error(
            "Unexpected error force-updating agent run status for %s: %s",
            agent_run_id,
            error,
            exc_info=True,
        )
        return False
