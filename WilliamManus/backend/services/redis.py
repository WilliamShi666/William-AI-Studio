"""
Redis connection service - asyncio version

前端请求 → 后端API → 启动Agent后台进程
                ↓
         创建Redis Keys:
         - response_list_key (存储响应)
         - response_channel (通知频道)
                ↓
    Agent开始运行，每产生一个响应:
    1. rpush(response_list_key, response)  ← 存储数据
    2. publish(response_channel, "new")    ← 发送通知
                ↓
    前端API订阅response_channel:
    1. 收到"new"通知
    2. lrange(response_list_key, last_index, -1)  ← 获取新数据
    3. 立即返回给前端渲染

await redis.rpush("my_list", "item1")
await redis.rpush("my_list", "item2") 
await redis.rpush("my_list", "item3")

# 结果：["item1", "item2", "item3"]
#        ↑                    ↑
#      左侧(头)             右侧(尾)
"""

import redis.asyncio as redis # type: ignore
import os
from dotenv import load_dotenv # type: ignore
import asyncio
from utils.logger import logger
from typing import List, Any
from utils.retry import retry

# Redis客户端和连接池全局变量
client: redis.Redis | None = None  # Redis客户端实例
pool: redis.ConnectionPool | None = None  # Redis连接池
_initialized = False  # 初始化状态标志
_init_lock = asyncio.Lock()  # 异步初始化锁，防止并发初始化
_pubsub_client: redis.Redis | None = None  # Redis Pub/Sub客户端实例
_pubsub_pool: redis.ConnectionPool | None = None  # Redis Pub/Sub连接池
_pubsub_initialized = False  # Pub/Sub初始化状态标志
_pubsub_init_lock = asyncio.Lock()  # Pub/Sub异步初始化锁

# Redis键值过期时间配置
REDIS_KEY_TTL = 3600 * 24  # 默认24小时过期时间
# Redis操作超时与熔断配置
REDIS_OP_TIMEOUT_SECONDS = float(os.getenv("REDIS_OP_TIMEOUT_SECONDS", "3.0"))
REDIS_CIRCUIT_FAILURE_THRESHOLD = int(os.getenv("REDIS_CIRCUIT_FAILURE_THRESHOLD", "5"))
REDIS_CIRCUIT_COOLDOWN_SECONDS = float(os.getenv("REDIS_CIRCUIT_COOLDOWN_SECONDS", "10.0"))

_circuit_failure_count = 0
_circuit_open_until = 0.0
_circuit_lock = asyncio.Lock()



def _load_redis_env():
    """Load Redis configuration from environment variables."""
    load_dotenv(override=False)
    redis_host = os.getenv("REDIS_HOST", "redis")  # Redis主机地址，默认"redis"
    redis_port = int(os.getenv("REDIS_PORT", 6379))  # Redis端口，默认6379
    redis_password = os.getenv("REDIS_PASSWORD", "")  # Redis密码，默认空
    redis_db = int(os.getenv("REDIS_DB", 0))
    return redis_host, redis_port, redis_password, redis_db


def _parse_optional_float(value: str | None, default: float | None) -> float | None:
    if value is None:
        return default
    trimmed = value.strip()
    if not trimmed or trimmed.lower() in ("none", "null"):
        return None
    try:
        return float(trimmed)
    except ValueError:
        logger.warning("Invalid float value '%s', using default %s", value, default)
        return default


def _parse_optional_int(value: str | None, default: int) -> int:
    if value is None or not value.strip():
        return default
    try:
        return int(value)
    except ValueError:
        logger.warning("Invalid int value '%s', using default %s", value, default)
        return default

def _now_monotonic() -> float:
    return asyncio.get_running_loop().time() if asyncio.get_running_loop().is_running() else 0.0


async def _before_redis_operation(operation: str) -> None:
    global _circuit_open_until
    now = _now_monotonic()
    async with _circuit_lock:
        if _circuit_open_until > now:
            remaining = _circuit_open_until - now
            raise ConnectionError(
                f"Redis circuit open for another {remaining:.1f}s while executing {operation}"
            )


async def _record_redis_success() -> None:
    global _circuit_failure_count, _circuit_open_until
    async with _circuit_lock:
        _circuit_failure_count = 0
        _circuit_open_until = 0.0


async def _record_redis_failure(operation: str, error: Exception) -> None:
    global _circuit_failure_count, _circuit_open_until
    now = _now_monotonic()
    async with _circuit_lock:
        _circuit_failure_count += 1
        if _circuit_failure_count >= REDIS_CIRCUIT_FAILURE_THRESHOLD:
            _circuit_open_until = max(_circuit_open_until, now + REDIS_CIRCUIT_COOLDOWN_SECONDS)
            logger.warning(
                "Redis circuit opened after %s failures (operation=%s, error=%s)",
                _circuit_failure_count,
                operation,
                error,
            )


async def _run_redis_operation(operation: str, call, *, timeout: float | None = None):
    await _before_redis_operation(operation)
    effective_timeout = timeout if timeout is not None else REDIS_OP_TIMEOUT_SECONDS
    try:
        result = await asyncio.wait_for(call(), timeout=effective_timeout)
    except Exception as error:
        await _record_redis_failure(operation, error)
        raise
    await _record_redis_success()
    return result


def initialize():
    """Initialize Redis connection pool and client"""
    global client, pool

    # 从环境变量获取Redis配置
    redis_host, redis_port, redis_password, redis_db = _load_redis_env()
    
    # 连接池配置 - 针对生产环境优化
    max_connections = _parse_optional_int(
        os.getenv("REDIS_MAX_CONNECTIONS"),
        64,
    )
    socket_timeout = 15.0            # Socket超时时间，15秒
    connect_timeout = 10.0           # 连接超时时间，10秒
    retry_on_timeout = not (os.getenv("REDIS_RETRY_ON_TIMEOUT", "True").lower() != "true")  # 超时重试开关

    logger.info(
        "Initializing Redis connection pool: %s:%s db=%s max connections=%s",
        redis_host,
        redis_port,
        redis_db,
        max_connections,
    )

    # 创建生产环境优化的连接池
    pool = redis.ConnectionPool(
        host=redis_host,
        port=redis_port,
        password=redis_password,
        decode_responses=True,  # 自动解码响应为字符串
        db=redis_db,
        socket_timeout=socket_timeout,  # Socket操作超时
        socket_connect_timeout=connect_timeout,  # 连接超时
        socket_keepalive=True,  # 启用Socket保活
        retry_on_timeout=retry_on_timeout,  # 超时重试
        health_check_interval=30,  # 健康检查间隔，30秒
        max_connections=max_connections,  # 最大连接数
    )

    # 从连接池创建Redis客户端
    client = redis.Redis(connection_pool=pool)

    return client

async def initialize_async():
    """Async initialize Redis connection"""
    global client, _initialized

    async with _init_lock:  # 使用异步锁防止并发初始化
        if not _initialized:
            logger.info("Initializing Redis connection")
            initialize()  # 调用同步初始化函数

        try:
            # 测试连接，设置5秒超时
            await asyncio.wait_for(client.ping(), timeout=5.0)
            logger.info("Redis connection initialized successfully")
            _initialized = True
        except asyncio.TimeoutError:
            logger.error("Redis connection initialization timeout")
            client = None
            _initialized = False
            raise ConnectionError("Redis connection timeout")
        except Exception as e:
            logger.error(f"Redis connection failed: {e}")
            client = None
            _initialized = False
            raise

    return client

def initialize_pubsub():
    """Initialize Redis Pub/Sub connection pool and client"""
    global _pubsub_client, _pubsub_pool

    redis_host, redis_port, redis_password, redis_db = _load_redis_env()

    max_connections = _parse_optional_int(
        os.getenv("REDIS_MAX_CONNECTIONS"),
        64,
    )
    connect_timeout = 10.0
    retry_on_timeout = not (os.getenv("REDIS_RETRY_ON_TIMEOUT", "True").lower() != "true")
    pubsub_socket_timeout = _parse_optional_float(
        os.getenv("REDIS_PUBSUB_SOCKET_TIMEOUT"),
        None,
    )
    pubsub_health_check_interval = _parse_optional_int(
        os.getenv("REDIS_PUBSUB_HEALTH_CHECK_INTERVAL"),
        30,
    )

    logger.info(
        "Initializing Redis Pub/Sub connection pool: %s:%s db=%s socket_timeout=%s health_check_interval=%s",
        redis_host,
        redis_port,
        redis_db,
        pubsub_socket_timeout,
        pubsub_health_check_interval,
    )

    _pubsub_pool = redis.ConnectionPool(
        host=redis_host,
        port=redis_port,
        password=redis_password,
        decode_responses=True,
        db=redis_db,
        socket_timeout=pubsub_socket_timeout,
        socket_connect_timeout=connect_timeout,
        socket_keepalive=True,
        retry_on_timeout=retry_on_timeout,
        health_check_interval=pubsub_health_check_interval,
        max_connections=max_connections,
    )

    _pubsub_client = redis.Redis(connection_pool=_pubsub_pool)

    return _pubsub_client


async def initialize_pubsub_async():
    """Async initialize Redis Pub/Sub connection"""
    global _pubsub_client, _pubsub_initialized

    async with _pubsub_init_lock:
        if not _pubsub_initialized:
            logger.info("Initializing Redis Pub/Sub connection")
            initialize_pubsub()

        try:
            await asyncio.wait_for(_pubsub_client.ping(), timeout=5.0)
            logger.info("Redis Pub/Sub connection initialized successfully")
            _pubsub_initialized = True
        except asyncio.TimeoutError:
            logger.error("Redis Pub/Sub connection initialization timeout")
            _pubsub_client = None
            _pubsub_initialized = False
            raise ConnectionError("Redis Pub/Sub connection timeout")
        except Exception as e:
            logger.error(f"Redis Pub/Sub connection failed: {e}")
            _pubsub_client = None
            _pubsub_initialized = False
            raise

    return _pubsub_client


async def get_pubsub_client():
    """Get Redis Pub/Sub client, if not initialized then initialize it"""
    global _pubsub_client, _pubsub_initialized
    if _pubsub_client is None or not _pubsub_initialized:
        await retry(lambda: initialize_pubsub_async())
    return _pubsub_client


async def close():
    """Close Redis connection and connection pool"""
    global client, pool, _initialized, _pubsub_client, _pubsub_pool, _pubsub_initialized
    global _circuit_failure_count, _circuit_open_until
    
    # 关闭Redis客户端连接
    if client:
        logger.info("Closing Redis connection")
        try:
            await asyncio.wait_for(client.aclose(), timeout=5.0)  # 5秒超时关闭
        except asyncio.TimeoutError:
            logger.warning("Redis connection close timeout, force close")
        except Exception as e:
            logger.warning(f"Error closing Redis client: {e}")
        finally:
            client = None  # 清空客户端引用
    
    # 关闭Redis连接池
    if pool:
        logger.info("Closing Redis connection pool")
        try:
            await asyncio.wait_for(pool.aclose(), timeout=5.0)  # 5秒超时关闭
        except asyncio.TimeoutError:
            logger.warning("Redis connection pool close timeout, force close")
        except Exception as e:
            logger.warning(f"Error closing Redis connection pool: {e}")
        finally:
            pool = None  # 清空连接池引用

    if _pubsub_client:
        logger.info("Closing Redis Pub/Sub connection")
        try:
            await asyncio.wait_for(_pubsub_client.aclose(), timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning("Redis Pub/Sub connection close timeout, force close")
        except Exception as e:
            logger.warning(f"Error closing Redis Pub/Sub client: {e}")
        finally:
            _pubsub_client = None

    if _pubsub_pool:
        logger.info("Closing Redis Pub/Sub connection pool")
        try:
            await asyncio.wait_for(_pubsub_pool.aclose(), timeout=5.0)
        except asyncio.TimeoutError:
            logger.warning("Redis Pub/Sub connection pool close timeout, force close")
        except Exception as e:
            logger.warning(f"Error closing Redis Pub/Sub connection pool: {e}")
        finally:
            _pubsub_pool = None
    
    _initialized = False  # 重置初始化状态
    _pubsub_initialized = False
    _circuit_failure_count = 0
    _circuit_open_until = 0.0
    logger.info("Redis connection and connection pool closed")

async def get_client():
    """Get Redis client, if not initialized then initialize it"""
    global client, _initialized
    if client is None or not _initialized:
        # 默认配置
        # max_attempts = 3 - 最多尝试3次
        # delay_seconds = 1 - 每次重试间隔1秒
        await retry(lambda: initialize_async())  # 使用重试机制初始化
    return client

async def set(
    key: str,
    value: str,
    ex: int = None,
    nx: bool = False,
    *,
    timeout: float | None = None,
):
    """Set Redis key-value pair."""
    redis_client = await get_client()
    return await _run_redis_operation(
        "set",
        lambda: redis_client.set(key, value, ex=ex, nx=nx),
        timeout=timeout,
    )

async def get(key: str, default: str = None, *, timeout: float | None = None):
    """Get Redis key-value."""
    redis_client = await get_client()
    result = await _run_redis_operation(
        "get",
        lambda: redis_client.get(key),
        timeout=timeout,
    )
    return result if result is not None else default

async def delete(key: str, *, timeout: float | None = None):
    """Delete Redis key."""
    redis_client = await get_client()
    return await _run_redis_operation(
        "delete",
        lambda: redis_client.delete(key),
        timeout=timeout,
    )

async def publish(channel: str, message: str, *, timeout: float | None = None):
    """Publish message to Redis channel (publish/subscribe mode)."""
    redis_client = await get_client()
    return await _run_redis_operation(
        "publish",
        lambda: redis_client.publish(channel, message),
        timeout=timeout,
    )

async def create_pubsub():
    """Create Redis publish/subscribe object
    
    Returns:
        Redis pubsub object, for subscribing to channels
    """
    redis_client = await get_pubsub_client()
    return redis_client.pubsub()

async def hset(key: str, mapping: dict[str, Any], *, timeout: float | None = None):
    """Set multiple hash fields."""
    redis_client = await get_client()
    return await _run_redis_operation(
        "hset",
        lambda: redis_client.hset(key, mapping=mapping),
        timeout=timeout,
    )


async def hget(key: str, field: str, *, timeout: float | None = None):
    """Get one hash field value."""
    redis_client = await get_client()
    return await _run_redis_operation(
        "hget",
        lambda: redis_client.hget(key, field),
        timeout=timeout,
    )


async def hgetall(key: str, *, timeout: float | None = None):
    """Get all hash fields."""
    redis_client = await get_client()
    return await _run_redis_operation(
        "hgetall",
        lambda: redis_client.hgetall(key),
        timeout=timeout,
    )


async def hdel(key: str, *fields: str, timeout: float | None = None):
    """Delete one or more hash fields."""
    redis_client = await get_client()
    return await _run_redis_operation(
        "hdel",
        lambda: redis_client.hdel(key, *fields),
        timeout=timeout,
    )


async def sadd(key: str, *values: str, timeout: float | None = None):
    """Add values into a set."""
    redis_client = await get_client()
    return await _run_redis_operation(
        "sadd",
        lambda: redis_client.sadd(key, *values),
        timeout=timeout,
    )


async def smembers(key: str, *, timeout: float | None = None):
    """Get members of a set."""
    redis_client = await get_client()
    return await _run_redis_operation(
        "smembers",
        lambda: redis_client.smembers(key),
        timeout=timeout,
    )


async def srem(key: str, *values: str, timeout: float | None = None):
    """Remove values from a set."""
    redis_client = await get_client()
    return await _run_redis_operation(
        "srem",
        lambda: redis_client.srem(key, *values),
        timeout=timeout,
    )


async def rpush(key: str, *values: Any, timeout: float | None = None):
    """Add one or more values to the right side of the list."""
    redis_client = await get_client()
    return await _run_redis_operation(
        "rpush",
        lambda: redis_client.rpush(key, *values),
        timeout=timeout,
    )

async def lrange(
    key: str,
    start: int,
    end: int,
    *,
    timeout: float | None = None,
) -> List[str]:
    """Get elements in the specified range of the list."""
    redis_client = await get_client()
    return await _run_redis_operation(
        "lrange",
        lambda: redis_client.lrange(key, start, end),
        timeout=timeout,
    )


async def llen(key: str, *, timeout: float | None = None) -> int:
    """Get the current length of a list."""
    redis_client = await get_client()
    return await _run_redis_operation(
        "llen",
        lambda: redis_client.llen(key),
        timeout=timeout,
    )

async def keys(pattern: str, *, timeout: float | None = None) -> List[str]:
    """Find keys matching the pattern."""
    redis_client = await get_client()
    return await _run_redis_operation(
        "keys",
        lambda: redis_client.keys(pattern),
        timeout=timeout,
    )


async def scan_keys(
    pattern: str,
    *,
    count: int = 1000,
    timeout: float | None = None,
) -> List[str]:
    """Find keys matching the pattern without blocking Redis with KEYS."""
    redis_client = await get_client()
    scan_count = max(1, int(count))

    async def _scan() -> List[str]:
        cursor: int | str = 0
        matched_keys: List[str] = []

        while True:
            cursor, batch = await redis_client.scan(
                cursor=cursor,
                match=pattern,
                count=scan_count,
            )
            matched_keys.extend(batch or [])
            if cursor in (0, "0"):
                break

        return matched_keys

    return await _run_redis_operation(
        "scan",
        _scan,
        timeout=timeout,
    )

async def expire(key: str, seconds: int, *, timeout: float | None = None):
    """Set the expiration time of the key."""
    redis_client = await get_client()
    return await _run_redis_operation(
        "expire",
        lambda: redis_client.expire(key, seconds),
        timeout=timeout,
    )


async def eval_script(
    script: str,
    keys: List[str],
    args: List[Any],
    *,
    timeout: float | None = None,
):
    """Execute a Lua script with explicit key/arg separation."""
    redis_client = await get_client()
    safe_keys = list(keys or [])
    safe_args = list(args or [])
    eval_args = [len(safe_keys), *safe_keys, *safe_args]
    return await _run_redis_operation(
        "eval",
        lambda: redis_client.eval(script, *eval_args),
        timeout=timeout,
    )
