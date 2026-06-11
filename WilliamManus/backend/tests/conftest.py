import sys
from pathlib import Path
import types


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


if "structlog" not in sys.modules:
    class _DummyBoundLogger:
        def info(self, *args, **kwargs):
            return None

        def warning(self, *args, **kwargs):
            return None

        def error(self, *args, **kwargs):
            return None

        def debug(self, *args, **kwargs):
            return None

    class _DummyProcessorFormatter:
        @staticmethod
        def wrap_for_formatter(*args, **kwargs):
            return None

    sys.modules["structlog"] = types.SimpleNamespace(
        configure=lambda **kwargs: None,
        get_logger=lambda *args, **kwargs: _DummyBoundLogger(),
        stdlib=types.SimpleNamespace(
            add_log_level=lambda *args, **kwargs: None,
            PositionalArgumentsFormatter=lambda *args, **kwargs: None,
            ProcessorFormatter=_DummyProcessorFormatter,
            LoggerFactory=lambda *args, **kwargs: None,
            BoundLogger=_DummyBoundLogger,
        ),
        processors=types.SimpleNamespace(TimeStamper=lambda *args, **kwargs: None),
        contextvars=types.SimpleNamespace(
            clear_contextvars=lambda: None,
            bind_contextvars=lambda **kwargs: None,
            get_contextvars=lambda: {},
        ),
    )


if "dramatiq" not in sys.modules:
    dramatiq_stub = types.ModuleType("dramatiq")

    def _actor(*args, **kwargs):
        def _decorator(fn):
            fn.fn = fn
            fn.send = lambda *a, **kw: None
            fn.options = dict(kwargs)
            return fn

        return _decorator

    dramatiq_stub.actor = _actor
    dramatiq_stub.set_broker = lambda *args, **kwargs: None
    dramatiq_stub.middleware = types.SimpleNamespace(
        AsyncIO=type("AsyncIO", (), {}),
        Retries=type("Retries", (), {}),
        TimeLimit=type("TimeLimit", (), {}),
    )
    sys.modules["dramatiq"] = dramatiq_stub
    dramatiq_brokers_mod = types.ModuleType("dramatiq.brokers")
    dramatiq_brokers_redis_mod = types.ModuleType("dramatiq.brokers.redis")
    dramatiq_brokers_redis_mod.RedisBroker = type(
        "RedisBroker",
        (),
        {"__init__": lambda self, *args, **kwargs: None},
    )
    sys.modules["dramatiq.brokers"] = dramatiq_brokers_mod
    sys.modules["dramatiq.brokers.redis"] = dramatiq_brokers_redis_mod


if "redis.asyncio" not in sys.modules:
    redis_pkg = sys.modules.get("redis")
    if redis_pkg is None:
        redis_pkg = types.ModuleType("redis")
        sys.modules["redis"] = redis_pkg

    redis_asyncio_mod = types.ModuleType("redis.asyncio")

    class _DummyConnectionPool:
        def __init__(self, *args, **kwargs):
            pass

        async def aclose(self):
            return None

    class _DummyPubSub:
        async def subscribe(self, *args, **kwargs):
            return None

        async def unsubscribe(self, *args, **kwargs):
            return None

        async def close(self):
            return None

        async def get_message(self, *args, **kwargs):
            return None

    class _DummyRedis:
        def __init__(self, *args, **kwargs):
            pass

        async def ping(self):
            return True

        async def aclose(self):
            return None

        async def set(self, *args, **kwargs):
            return True

        async def get(self, *args, **kwargs):
            return None

        async def delete(self, *args, **kwargs):
            return 1

        async def publish(self, *args, **kwargs):
            return 1

        async def hset(self, *args, **kwargs):
            return 1

        async def hget(self, *args, **kwargs):
            return None

        async def hgetall(self, *args, **kwargs):
            return {}

        async def sadd(self, *args, **kwargs):
            return 1

        async def smembers(self, *args, **kwargs):
            return set()

        async def srem(self, *args, **kwargs):
            return 1

        async def rpush(self, *args, **kwargs):
            return 1

        async def lrange(self, *args, **kwargs):
            return []

        async def keys(self, *args, **kwargs):
            return []

        async def expire(self, *args, **kwargs):
            return True

        async def eval(self, *args, **kwargs):
            return 1

        def pubsub(self):
            return _DummyPubSub()

    redis_asyncio_mod.ConnectionPool = _DummyConnectionPool
    redis_asyncio_mod.Redis = _DummyRedis

    sys.modules["redis.asyncio"] = redis_asyncio_mod
    setattr(redis_pkg, "asyncio", redis_asyncio_mod)


if "asyncpg" not in sys.modules:
    asyncpg_mod = types.ModuleType("asyncpg")

    class _DummyPool:
        def acquire(self):
            return self

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc_val, exc_tb):
            return None

        async def fetch(self, *args, **kwargs):
            return []

        async def fetchval(self, *args, **kwargs):
            return 0

        async def close(self):
            return None

    async def _create_pool(*args, **kwargs):
        return _DummyPool()

    asyncpg_mod.create_pool = _create_pool
    asyncpg_mod.Pool = _DummyPool
    sys.modules["asyncpg"] = asyncpg_mod


if "agentpress" not in sys.modules:
    sys.modules["agentpress"] = types.ModuleType("agentpress")


if "agentpress.adk_thread_manager" not in sys.modules:
    agentpress_adk_mod = types.ModuleType("agentpress.adk_thread_manager")
    agentpress_adk_mod.ADKThreadManager = type("ADKThreadManager", (), {})
    sys.modules["agentpress.adk_thread_manager"] = agentpress_adk_mod


if "agentpress.tool" not in sys.modules:
    agentpress_tool_mod = types.ModuleType("agentpress.tool")

    class _Tool:
        def __init__(self, *args, **kwargs):
            return None

    class _ToolResult:
        def __init__(self, success=True, output=None):
            self.success = success
            self.output = output

    agentpress_tool_mod.Tool = _Tool
    agentpress_tool_mod.ToolResult = _ToolResult
    sys.modules["agentpress.tool"] = agentpress_tool_mod


if "fastapi" not in sys.modules:
    fastapi_mod = types.ModuleType("fastapi")
    fastapi_security_mod = types.ModuleType("fastapi.security")
    fastapi_responses_mod = types.ModuleType("fastapi.responses")

    class _HTTPException(Exception):
        def __init__(self, status_code: int, detail=None, headers=None):
            super().__init__(detail)
            self.status_code = status_code
            self.detail = detail
            self.headers = headers

    class _APIRouter:
        def __init__(self, *args, **kwargs):
            return None

        def include_router(self, *args, **kwargs):
            return None

        def _route(self, *args, **kwargs):
            def _decorator(fn):
                return fn

            return _decorator

        post = _route
        get = _route
        put = _route
        delete = _route
        patch = _route

    def _depends(dep):
        return dep

    def _identity(value=None, *args, **kwargs):
        return value

    class _FastAPI:
        def __init__(self, *args, **kwargs):
            return None

        def include_router(self, *args, **kwargs):
            return None

    class _BackgroundTasks:
        def add_task(self, *args, **kwargs):
            return None

    fastapi_mod.APIRouter = _APIRouter
    fastapi_mod.FastAPI = _FastAPI
    fastapi_mod.BackgroundTasks = _BackgroundTasks
    fastapi_mod.HTTPException = _HTTPException
    fastapi_mod.Depends = _depends
    fastapi_mod.Body = _identity
    fastapi_mod.File = _identity
    fastapi_mod.Form = _identity
    fastapi_mod.Query = _identity
    fastapi_mod.Header = _identity
    fastapi_mod.UploadFile = type("UploadFile", (), {})
    fastapi_mod.Request = type("Request", (), {})

    class _FileResponse:
        def __init__(
            self,
            path=None,
            media_type=None,
            filename=None,
            background=None,
            headers=None,
            status_code=200,
            *args,
            **kwargs,
        ):
            self.path = path
            self.media_type = media_type
            self.filename = filename
            self.background = background
            self.headers = {k.lower(): v for k, v in (headers or {}).items()}
            self.status_code = status_code

    class _Response:
        def __init__(self, content=None, status_code=200, headers=None, media_type=None, *args, **kwargs):
            self.content = content
            self.status_code = status_code
            self.headers = {k.lower(): v for k, v in (headers or {}).items()}
            self.media_type = media_type

    class _StreamingResponse(_Response):
        def __init__(self, content=None, *args, **kwargs):
            super().__init__(content=content, *args, **kwargs)
            if hasattr(content, "__aiter__"):
                self.body_iterator = content
            else:
                async def _body_iterator():
                    if content is not None:
                        yield content

                self.body_iterator = _body_iterator()

    fastapi_responses_mod.FileResponse = _FileResponse
    fastapi_responses_mod.Response = _Response
    fastapi_responses_mod.StreamingResponse = _StreamingResponse
    fastapi_security_mod.HTTPBearer = type(
        "HTTPBearer",
        (),
        {"__init__": lambda self, *args, **kwargs: None},
    )
    fastapi_security_mod.HTTPAuthorizationCredentials = type(
        "HTTPAuthorizationCredentials",
        (),
        {},
    )
    sys.modules["fastapi"] = fastapi_mod
    sys.modules["fastapi.security"] = fastapi_security_mod
    sys.modules["fastapi.responses"] = fastapi_responses_mod


if "sentry_sdk" not in sys.modules:
    sentry_sdk_mod = types.ModuleType("sentry_sdk")
    sentry_sdk_mod.init = lambda *args, **kwargs: None
    sentry_sdk_mod.set_tag = lambda *args, **kwargs: None
    sentry_sdk_mod.capture_exception = lambda *args, **kwargs: None

    sentry_integrations_mod = types.ModuleType("sentry_sdk.integrations")
    sentry_dramatiq_mod = types.ModuleType("sentry_sdk.integrations.dramatiq")
    sentry_dramatiq_mod.DramatiqIntegration = type("DramatiqIntegration", (), {})
    sentry_integrations_mod.dramatiq = sentry_dramatiq_mod

    sys.modules["sentry_sdk"] = sentry_sdk_mod
    sys.modules["sentry_sdk.integrations"] = sentry_integrations_mod
    sys.modules["sentry_sdk.integrations.dramatiq"] = sentry_dramatiq_mod


if "langfuse" not in sys.modules:
    langfuse_mod = types.ModuleType("langfuse")
    langfuse_client_mod = types.ModuleType("langfuse.client")
    langfuse_span_mod = types.ModuleType("langfuse._client.span")

    class _DummyObservation:
        trace_id: str = "00000000000000000000000000000000"
        id: str = "00000000000000000000000000000000"

        def start_observation(self, *args, **kwargs):
            return _DummyObservation()

        def start_span(self, *args, **kwargs):
            return _DummyObservation()

        def start_generation(self, *args, **kwargs):
            return _DummyObservation()

        def generation(self, *args, **kwargs):
            return _DummyObservation()

        def span(self, *args, **kwargs):
            return _DummyObservation()

        def update(self, *args, **kwargs):
            return self

        def update_trace(self, *args, **kwargs):
            return self

        def end(self, *args, **kwargs):
            return self

        def create_event(self, *args, **kwargs):
            return self

    # Keep old name for any remaining TYPE_CHECKING references
    _DummyStatefulTraceClient = _DummyObservation

    class _DummyLangfuse:
        def __init__(self, *args, **kwargs):
            return None

        def start_span(self, *args, **kwargs):
            return _DummyObservation()

        def start_observation(self, *args, **kwargs):
            return _DummyObservation()

        def start_generation(self, *args, **kwargs):
            return _DummyObservation()

        def trace(self, *args, **kwargs):
            return _DummyObservation()

        def create_score(self, *args, **kwargs):
            return None

        def create_trace_id(self, *, seed: str = "") -> str:
            import hashlib
            return hashlib.md5(seed.encode()).hexdigest()

        def flush(self, *args, **kwargs):
            return None

    langfuse_mod.Langfuse = _DummyLangfuse
    langfuse_mod.StatefulTraceClient = _DummyStatefulTraceClient
    langfuse_client_mod.StatefulTraceClient = _DummyStatefulTraceClient
    langfuse_span_mod.LangfuseSpan = _DummyObservation
    langfuse_span_mod.LangfuseGeneration = _DummyObservation

    sys.modules["langfuse"] = langfuse_mod
    sys.modules["langfuse.client"] = langfuse_client_mod
    sys.modules["langfuse._client"] = types.ModuleType("langfuse._client")
    sys.modules["langfuse._client.span"] = langfuse_span_mod
