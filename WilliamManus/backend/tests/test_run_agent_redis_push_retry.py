import asyncio
from datetime import datetime
import json
import sys
import types
import uuid
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))
AGENT_DIR = str(BACKEND_ROOT / "agent")
AGENT_TOOLS_DIR = str(BACKEND_ROOT / "agent" / "tools")
SERVICES_DIR = str(BACKEND_ROOT / "services")

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

    dummy_structlog = types.SimpleNamespace(
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
    sys.modules["structlog"] = dummy_structlog

if "sentry_sdk" not in sys.modules:
    sentry_sdk_mod = types.ModuleType("sentry_sdk")
    sentry_sdk_mod.init = lambda *args, **kwargs: None
    sentry_sdk_mod.set_tag = lambda *args, **kwargs: None
    sentry_sdk_mod.capture_exception = lambda *args, **kwargs: None
    integrations_mod = types.ModuleType("sentry_sdk.integrations")
    dramatiq_mod = types.ModuleType("sentry_sdk.integrations.dramatiq")
    dramatiq_mod.DramatiqIntegration = type("DramatiqIntegration", (), {})
    integrations_mod.dramatiq = dramatiq_mod
    sentry_sdk_mod.integrations = integrations_mod
    sys.modules["sentry_sdk"] = sentry_sdk_mod
    sys.modules["sentry_sdk.integrations"] = integrations_mod
    sys.modules["sentry_sdk.integrations.dramatiq"] = dramatiq_mod

if "dramatiq" not in sys.modules:
    dramatiq_stub = types.ModuleType("dramatiq")
    dramatiq_stub.actor = lambda *a, **kw: (lambda fn: fn)
    dramatiq_stub.set_broker = lambda *a, **kw: None
    dramatiq_stub.middleware = types.SimpleNamespace(
        AsyncIO=type("AsyncIO", (), {}),
    )
    sys.modules["dramatiq"] = dramatiq_stub

if "dramatiq.brokers.redis" not in sys.modules:
    dramatiq_broker_mod = types.ModuleType("dramatiq.brokers.redis")
    dramatiq_broker_mod.RedisBroker = type(
        "RedisBroker",
        (),
        {"__init__": lambda self, *a, **kw: None},
    )
    sys.modules["dramatiq.brokers.redis"] = dramatiq_broker_mod

if "services" not in sys.modules:
    services_pkg = types.ModuleType("services")
    services_pkg.__path__ = [SERVICES_DIR]
    sys.modules["services"] = services_pkg
elif not hasattr(sys.modules["services"], "__path__"):
    sys.modules["services"].__path__ = [SERVICES_DIR]
elif SERVICES_DIR not in sys.modules["services"].__path__:
    sys.modules["services"].__path__.append(SERVICES_DIR)

if "services.redis" not in sys.modules:

    async def _noop_async(*_args, **_kwargs):
        return 1

    async def _noop_lrange(*_args, **_kwargs):
        return []

    services_redis_stub = types.SimpleNamespace(
        REDIS_KEY_TTL=3600,
        initialize_async=lambda: _noop_async(),
        set=_noop_async,
        get=_noop_async,
        delete=_noop_async,
        expire=_noop_async,
        publish=_noop_async,
        rpush=_noop_async,
        lrange=_noop_lrange,
        eval_script=_noop_async,
    )
    sys.modules["services.redis"] = services_redis_stub
    setattr(sys.modules["services"], "redis", services_redis_stub)

if "services.postgresql" not in sys.modules:

    class _DummyDBConnection:
        @property
        async def client(self):
            return None

        async def initialize(self):
            return None

    services_pg_stub = types.ModuleType("services.postgresql")
    services_pg_stub.DBConnection = _DummyDBConnection
    sys.modules["services.postgresql"] = services_pg_stub
    setattr(sys.modules["services"], "postgresql", services_pg_stub)

if "services.langfuse" not in sys.modules:

    class _DummyTrace:
        def update(self, *args, **kwargs):
            return self

        def update_trace(self, *args, **kwargs):
            return self

        def start_observation(self, *args, **kwargs):
            return self

        def start_generation(self, *args, **kwargs):
            return self

        def create_event(self, *args, **kwargs):
            return self

        def end(self, *args, **kwargs):
            return None

        def span(self, *args, **kwargs):
            return types.SimpleNamespace(end=lambda *a, **kw: None)

    services_langfuse_stub = types.ModuleType("services.langfuse")
    services_langfuse_stub.langfuse = types.SimpleNamespace(
        trace=lambda *a, **kw: _DummyTrace(),
        flush=lambda *a, **kw: None,
    )
    services_langfuse_stub.generate_trace_id = lambda seed: "0" * 32
    services_langfuse_stub.get_trace_url = lambda trace_id: "(unavailable)"
    services_langfuse_stub.start_root_span = lambda *, name, trace_id: _DummyTrace()
    services_langfuse_stub.filter_registered_scores = lambda scores, **kwargs: scores
    services_langfuse_stub.score_many_numeric = lambda *a, **kw: None
    sys.modules["services.langfuse"] = services_langfuse_stub
    setattr(sys.modules["services"], "langfuse", services_langfuse_stub)

if "services.run_capacity" not in sys.modules:

    async def _capacity_snapshot(*_args, **_kwargs):
        return {
            "enabled": False,
            "budget": 0,
            "in_use": 0,
            "remaining": 0,
            "requested_cost": 1,
            "can_admit": True,
            "capacity_kind": "regular",
            "acquired": True,
            "refreshed": False,
            "lease_ttl_seconds": 0,
        }

    def _capacity_limit_detail(*, mode=None, snapshot=None):
        snapshot = snapshot or {}
        return {
            "code": "server_concurrency_limit",
            "message": "Server concurrency budget is exhausted. Please retry shortly.",
            "capacity_kind": (
                "shadow_clone"
                if str(mode or "").lower() in {"on", "auto"}
                else "regular"
            ),
            "budget": int(snapshot.get("budget") or 0),
            "in_use": int(snapshot.get("in_use") or 0),
            "remaining": int(snapshot.get("remaining") or 0),
            "requested_cost": int(snapshot.get("requested_cost") or 1),
        }

    services_run_capacity_stub = types.ModuleType("services.run_capacity")
    services_run_capacity_stub.build_run_capacity_limit_detail = _capacity_limit_detail
    services_run_capacity_stub.get_run_capacity_cost = lambda mode=None: (
        3 if str(mode or "").lower() in {"on", "auto"} else 1
    )
    services_run_capacity_stub.is_server_concurrency_budget_enabled = lambda: False
    services_run_capacity_stub.get_safe_run_capacity_snapshot = _capacity_snapshot
    services_run_capacity_stub.refresh_run_capacity_lease = _capacity_snapshot
    services_run_capacity_stub.release_run_capacity_lease = _capacity_snapshot
    services_run_capacity_stub.try_acquire_run_capacity_lease = _capacity_snapshot
    sys.modules["services.run_capacity"] = services_run_capacity_stub
    setattr(sys.modules["services"], "run_capacity", services_run_capacity_stub)

if "agent" not in sys.modules:
    agent_stub = types.ModuleType("agent")
    agent_stub.__path__ = [AGENT_DIR]
    sys.modules["agent"] = agent_stub
elif not hasattr(sys.modules["agent"], "__path__"):
    sys.modules["agent"].__path__ = [AGENT_DIR]
elif AGENT_DIR not in sys.modules["agent"].__path__:
    sys.modules["agent"].__path__.append(AGENT_DIR)
if "agent.run" not in sys.modules:

    async def _dummy_run_agent(*_args, **_kwargs):
        if False:
            yield {}
        return

    agent_run_stub = types.ModuleType("agent.run")
    agent_run_stub.run_agent = _dummy_run_agent
    sys.modules["agent.run"] = agent_run_stub
    setattr(sys.modules["agent"], "run", agent_run_stub)

if "agent.tools" not in sys.modules:
    agent_tools_stub = types.ModuleType("agent.tools")
    agent_tools_stub.__path__ = [AGENT_TOOLS_DIR]
    sys.modules["agent.tools"] = agent_tools_stub
    setattr(sys.modules["agent"], "tools", agent_tools_stub)
elif not hasattr(sys.modules["agent.tools"], "__path__"):
    sys.modules["agent.tools"].__path__ = [AGENT_TOOLS_DIR]
elif AGENT_TOOLS_DIR not in sys.modules["agent.tools"].__path__:
    sys.modules["agent.tools"].__path__.append(AGENT_TOOLS_DIR)

if "agent.tools.sandbox_code_tool" not in sys.modules:
    sandbox_code_stub = types.ModuleType("agent.tools.sandbox_code_tool")
    sandbox_code_stub.SandboxCodeTool = type("SandboxCodeTool", (), {})
    sys.modules["agent.tools.sandbox_code_tool"] = sandbox_code_stub
    setattr(sys.modules["agent.tools"], "sandbox_code_tool", sandbox_code_stub)

if "agentpress.thread_manager" not in sys.modules:
    agentpress_thread_manager_stub = types.ModuleType("agentpress.thread_manager")
    agentpress_thread_manager_stub.ThreadManager = type("ThreadManager", (), {})
    sys.modules["agentpress.thread_manager"] = agentpress_thread_manager_stub

import run_agent_background as run_agent_background_module


def test_default_generated_response_guard_allows_long_context_models() -> None:
    assert run_agent_background_module.MAX_GENERATED_RESPONSES == 200_000


class _FakeTableResult:
    def __init__(self, data):
        self.data = data


class _FakeAgentRunsTable:
    def __init__(self, rows):
        self._rows = rows
        self._filters = {}

    def select(self, *_args, **_kwargs):
        return self

    def eq(self, key, value):
        self._filters[key] = value
        return self

    async def execute(self):
        matched = [
            dict(row)
            for row in self._rows
            if all(row.get(key) == value for key, value in self._filters.items())
        ]
        return _FakeTableResult(matched)

    async def update(self, payload):
        matched = []
        for row in self._rows:
            if all(row.get(key) == value for key, value in self._filters.items()):
                row.update(payload)
                matched.append(dict(row))
        return _FakeTableResult(matched)


class _FakeMessagesTable:
    def __init__(self, inserts):
        self._inserts = inserts

    def insert(self, payload):
        self._inserts.append(dict(payload))
        return self

    async def execute(self):
        return _FakeTableResult(list(self._inserts))


class _FakeAgentRunsClient:
    def __init__(self, rows):
        self._rows = rows
        self.message_inserts = []

    def table(self, table_name):
        if table_name == "messages":
            return _FakeMessagesTable(self.message_inserts)
        assert table_name == "agent_runs"
        return _FakeAgentRunsTable(self._rows)


class _AsyncInsertMessagesTable:
    def __init__(self, inserts):
        self._inserts = inserts

    async def insert(self, payload):
        self._inserts.append(dict(payload))
        return _FakeTableResult(list(self._inserts))


class _AsyncInsertMessagesClient:
    def __init__(self):
        self.message_inserts = []

    def table(self, table_name):
        assert table_name == "messages"
        return _AsyncInsertMessagesTable(self.message_inserts)


class _UuidValidatingAsyncInsertMessagesTable(_AsyncInsertMessagesTable):
    async def insert(self, payload):
        uuid.UUID(str(payload.get("message_id") or ""))
        return await super().insert(payload)


class _UuidValidatingAsyncInsertMessagesClient(_AsyncInsertMessagesClient):
    def table(self, table_name):
        assert table_name == "messages"
        return _UuidValidatingAsyncInsertMessagesTable(self.message_inserts)


class _ProductionShapeMessagesTable(_AsyncInsertMessagesTable):
    async def insert(self, payload):
        uuid.UUID(str(payload.get("message_id") or ""))
        assert payload.get("project_id") == "project-production-shape"
        assert isinstance(payload.get("created_at"), datetime)
        assert isinstance(payload.get("updated_at"), datetime)
        return await super().insert(payload)


class _ProductionShapeMessagesClient(_AsyncInsertMessagesClient):
    def table(self, table_name):
        assert table_name == "messages"
        return _ProductionShapeMessagesTable(self.message_inserts)


class _FakeRunAgentDBConnection:
    def __init__(self, client):
        self._client = client

    @property
    async def client(self):
        return self._client

    async def initialize(self):
        return None


class _FakePubSub:
    def __init__(self, messages=None, *, idle_sleep_seconds: float = 0.005):
        self._messages = list(messages or [])
        self._idle_sleep_seconds = idle_sleep_seconds
        self.subscriptions = []
        self.unsubscribed = False
        self.closed = False

    async def subscribe(self, *channels):
        self.subscriptions.append(tuple(channels))

    async def get_message(self, ignore_subscribe_messages=True, timeout=0.5):
        if self._messages:
            return self._messages.pop(0)
        await asyncio.sleep(self._idle_sleep_seconds)
        return None

    async def unsubscribe(self):
        self.unsubscribed = True

    async def aclose(self):
        self.closed = True


class _FakeExternalControlPlaneSlot:
    def __init__(self):
        self.stop_requested = False
        self.control_plane_error = None
        self.refresh_callback = None

    def set_refresh_callback(self, callback):
        self.refresh_callback = callback


def _build_run_rows(run_id: str):
    return [{"agent_run_id": run_id, "status": "running"}]


def _extract_statuses_from_pushed_payloads(pushed_payloads: list[str]) -> list[str]:
    statuses: list[str] = []
    for raw_payload in pushed_payloads:
        payload = json.loads(raw_payload)
        if payload.get("type") == "status":
            statuses.append(str(payload.get("status") or ""))
    return statuses


def _install_run_agent_actor_test_harness(
    monkeypatch,
    *,
    run_id: str,
    run_agent_impl,
    acquire_result: dict | None = None,
    refresh_outcomes: list[dict | Exception] | None = None,
    pubsub: _FakePubSub | None = None,
    force_qwen_guards: bool = False,
    refresh_interval_seconds: float = 9999.0,
):
    rows = _build_run_rows(run_id)
    client = _FakeAgentRunsClient(rows)
    pushed_payloads: list[str] = []
    pushed_response_keys: list[str] = []
    mirrored_payloads: list[str] = []
    mirrored_response_keys: list[str] = []
    acquire_calls: list[tuple[str, object, str, bool]] = []
    refresh_calls: list[tuple[str, object, str]] = []
    release_calls: list[tuple[str, str]] = []
    refresh_queue = list(refresh_outcomes or [])
    fake_pubsub = pubsub or _FakePubSub()

    class _DummySpan:
        def end(self, *args, **kwargs):
            return None

    class _DummyTrace:
        def span(self, *args, **kwargs):
            return _DummySpan()

    async def _noop_async(*_args, **_kwargs):
        return None

    async def _redis_set(*_args, **_kwargs):
        return True

    async def _redis_get(*_args, **_kwargs):
        return None

    async def _redis_delete(*_args, **_kwargs):
        return 1

    async def _redis_expire(*_args, **_kwargs):
        return 1

    async def _redis_publish(*_args, **_kwargs):
        return 1

    async def _redis_lrange(*_args, **_kwargs):
        return list(pushed_payloads)

    async def _redis_create_pubsub():
        return fake_pubsub

    async def _fake_push_response(
        _response_list_key: str,
        _response_channel: str,
        payload_json: str,
        *,
        agent_run_id: str,
    ):
        pushed_response_keys.append(_response_list_key)
        pushed_payloads.append(payload_json)
        return True

    async def _fake_mirror_response(
        _response_list_key: str,
        payload_json: str,
        *,
        agent_run_id: str,
    ):
        mirrored_response_keys.append(_response_list_key)
        mirrored_payloads.append(payload_json)
        return True

    async def _fake_refresh_run_lock(_agent_run_id: str, _owner_token: str):
        return "refreshed"

    async def _fake_acquire(
        *, agent_run_id: str, mode, owner_token: str, allow_takeover: bool = False
    ):
        acquire_calls.append((agent_run_id, mode, owner_token, allow_takeover))
        if acquire_result is not None:
            return dict(acquire_result)
        return {
            "enabled": True,
            "budget": 16,
            "in_use": 1,
            "remaining": 15,
            "requested_cost": 1,
            "can_admit": True,
            "capacity_kind": "regular",
            "acquired": True,
            "refreshed": False,
            "taken_over": False,
            "owner_conflict": False,
            "lease_ttl_seconds": 90,
        }

    async def _fake_refresh(*, agent_run_id: str, mode, owner_token: str):
        refresh_calls.append((agent_run_id, mode, owner_token))
        if refresh_queue:
            outcome = refresh_queue.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return dict(outcome)
        return {
            "enabled": True,
            "budget": 16,
            "in_use": 1,
            "remaining": 15,
            "requested_cost": 1,
            "can_admit": True,
            "capacity_kind": "regular",
            "acquired": True,
            "refreshed": True,
            "taken_over": False,
            "owner_conflict": False,
            "lease_ttl_seconds": 90,
        }

    async def _fake_release(*, agent_run_id: str, owner_token: str):
        release_calls.append((agent_run_id, owner_token))
        return {
            "enabled": True,
            "budget": 16,
            "in_use": 0,
            "remaining": 16,
            "released": True,
            "released_cost": 1,
            "owner_conflict": False,
        }

    monkeypatch.setattr(run_agent_background_module, "initialize", _noop_async)
    monkeypatch.setattr(
        run_agent_background_module.uuid, "uuid4", lambda: "lease-owner"
    )
    monkeypatch.setattr(
        run_agent_background_module,
        "db",
        _FakeRunAgentDBConnection(client),
    )
    monkeypatch.setattr(run_agent_background_module, "run_agent", run_agent_impl)
    monkeypatch.setattr(
        run_agent_background_module,
        "is_server_concurrency_budget_enabled",
        lambda: True,
    )
    monkeypatch.setattr(
        run_agent_background_module,
        "ACTIVE_RUN_KEY_REFRESH_INTERVAL_SECONDS",
        refresh_interval_seconds,
    )
    monkeypatch.setattr(
        run_agent_background_module, "SANDBOX_SET_TIMEOUT_ENABLED", False
    )
    monkeypatch.setattr(
        run_agent_background_module, "_refresh_redis_run_lock", _fake_refresh_run_lock
    )
    monkeypatch.setattr(
        run_agent_background_module,
        "_schedule_project_sandbox_auto_pause",
        _noop_async,
    )
    monkeypatch.setattr(
        run_agent_background_module,
        "schedule_post_run_review",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        run_agent_background_module,
        "_cleanup_redis_run_lock",
        _noop_async,
    )
    monkeypatch.setattr(
        run_agent_background_module,
        "_maybe_enable_qwen_runtime_guards",
        (
            (lambda enabled, *, agent_run_id: True)
            if force_qwen_guards
            else (lambda enabled, *, agent_run_id: enabled)
        ),
    )
    monkeypatch.setattr(run_agent_background_module, "QWEN_STREAM_POLL_SECONDS", 0.01)
    monkeypatch.setattr(
        run_agent_background_module,
        "_push_response_with_retry",
        _fake_push_response,
    )
    monkeypatch.setattr(
        run_agent_background_module,
        "_mirror_response_without_notify_with_retry",
        _fake_mirror_response,
        raising=False,
    )
    monkeypatch.setattr(
        run_agent_background_module.redis,
        "set",
        _redis_set,
    )
    monkeypatch.setattr(
        run_agent_background_module.redis,
        "get",
        _redis_get,
    )
    monkeypatch.setattr(
        run_agent_background_module.redis,
        "delete",
        _redis_delete,
    )
    monkeypatch.setattr(
        run_agent_background_module.redis,
        "expire",
        _redis_expire,
    )
    monkeypatch.setattr(
        run_agent_background_module.redis,
        "publish",
        _redis_publish,
    )
    monkeypatch.setattr(
        run_agent_background_module.redis,
        "lrange",
        _redis_lrange,
    )
    monkeypatch.setattr(
        run_agent_background_module.redis,
        "create_pubsub",
        _redis_create_pubsub,
        raising=False,
    )
    monkeypatch.setattr(
        run_agent_background_module,
        "try_acquire_run_capacity_lease",
        _fake_acquire,
    )
    monkeypatch.setattr(
        run_agent_background_module,
        "refresh_run_capacity_lease",
        _fake_refresh,
    )
    monkeypatch.setattr(
        run_agent_background_module,
        "release_run_capacity_lease",
        _fake_release,
    )
    monkeypatch.setattr(
        run_agent_background_module.langfuse,
        "trace",
        lambda *args, **kwargs: _DummyTrace(),
    )

    return types.SimpleNamespace(
        client=client,
        rows=rows,
        pushed_payloads=pushed_payloads,
        pushed_response_keys=pushed_response_keys,
        mirrored_payloads=mirrored_payloads,
        mirrored_response_keys=mirrored_response_keys,
        acquire_calls=acquire_calls,
        refresh_calls=refresh_calls,
        release_calls=release_calls,
        pubsub=fake_pubsub,
    )


async def _invoke_run_agent_background_for_test(run_id: str) -> None:
    await run_agent_background_module.run_agent_background(
        agent_run_id=run_id,
        thread_id="thread-test",
        instance_id="instance-test",
        project_id="project-test",
        model_name="openai/gpt-4.1",
        enable_thinking=False,
        reasoning_effort=None,
        stream=True,
        enable_context_manager=False,
        agent_config={},
        is_agent_builder=False,
        target_agent_id=None,
        request_id="request-test",
        resume_strategy="auto",
        resume_window_minutes=1440,
        shadow_clone_mode="off",
        shadow_clone_main_model=None,
        shadow_clone_subagent_model=None,
    )


async def _simple_completed_run_agent(*_args, **_kwargs):
    yield {"type": "status", "status": "completed", "message": "done"}


def _build_write_chunk_response(
    arguments: str,
    *,
    tool_call_id: str = "call-1",
    args_source: str | None = None,
) -> dict:
    trace_payload = {}
    if args_source:
        trace_payload["trace_args_source"] = args_source
    return {
        "type": "assistant",
        "metadata": json.dumps(
            {
                "stream_status": "tool_call_chunk",
                "tool_calls": [
                    {
                        "id": tool_call_id,
                        "index": 0,
                        "type": "function",
                        "function": {
                            "name": "write_file",
                            "arguments": arguments,
                        },
                        "trace": trace_payload,
                    }
                ],
            },
            ensure_ascii=False,
        ),
    }


def _build_write_status_response(status_type: str) -> dict:
    return {
        "type": "status",
        "content": json.dumps(
            {
                "function_name": "write_file",
                "status_type": status_type,
                "arguments": json.dumps(
                    {"path": "/workspace/demo.py"}, ensure_ascii=False
                ),
            },
            ensure_ascii=False,
        ),
    }


@pytest.mark.asyncio
async def test_push_response_with_retry_succeeds_after_retry(monkeypatch):
    monkeypatch.setattr(run_agent_background_module, "REDIS_WRITE_RETRY_ATTEMPTS", 1)
    monkeypatch.setattr(run_agent_background_module, "REDIS_WRITE_TIMEOUT_SECONDS", 0.5)
    monkeypatch.setattr(
        run_agent_background_module,
        "REDIS_RESPONSE_LIST_INITIAL_TTL",
        172800,
    )

    call_count = {"eval_script": 0}
    eval_calls = []

    async def _flaky_eval_script(
        _script: str,
        keys,
        args,
        timeout: float | None = None,
    ):
        call_count["eval_script"] += 1
        eval_calls.append((list(keys), list(args), timeout))
        if call_count["eval_script"] == 1:
            raise TimeoutError("temporary redis timeout")
        return 1

    monkeypatch.setattr(
        run_agent_background_module.redis,
        "eval_script",
        _flaky_eval_script,
    )

    await run_agent_background_module._push_response_with_retry(
        "agent_run:run-1:responses",
        "agent_run:run-1:new_response",
        "{}",
        agent_run_id="run-1",
    )

    assert call_count["eval_script"] == 2
    assert eval_calls[-1] == (
        ["agent_run:run-1:responses", "agent_run:run-1:new_response"],
        ["{}", "172800", "new"],
        0.5,
    )


@pytest.mark.asyncio
async def test_push_response_with_retry_raises_after_timeout(monkeypatch):
    monkeypatch.setattr(run_agent_background_module, "REDIS_WRITE_RETRY_ATTEMPTS", 0)
    monkeypatch.setattr(
        run_agent_background_module, "REDIS_WRITE_TIMEOUT_SECONDS", 0.01
    )

    async def _slow_eval_script(
        _script: str,
        keys,
        args,
        timeout: float | None = None,
    ):
        await asyncio.sleep(0.05)
        return 1

    monkeypatch.setattr(
        run_agent_background_module.redis,
        "eval_script",
        _slow_eval_script,
    )

    with pytest.raises(RuntimeError) as exc_info:
        await run_agent_background_module._push_response_with_retry(
            "agent_run:run-2:responses",
            "agent_run:run-2:new_response",
            "{}",
            agent_run_id="run-2",
        )

    assert "run-2" in str(exc_info.value)
    assert "after 1 attempts" in str(exc_info.value)


@pytest.mark.asyncio
async def test_push_response_with_retry_passes_configured_timeout_to_redis_helpers(
    monkeypatch,
):
    monkeypatch.setattr(run_agent_background_module, "REDIS_WRITE_RETRY_ATTEMPTS", 0)
    monkeypatch.setattr(
        run_agent_background_module, "REDIS_WRITE_TIMEOUT_SECONDS", 4.25
    )
    monkeypatch.setattr(
        run_agent_background_module,
        "REDIS_RESPONSE_LIST_INITIAL_TTL",
        86400,
    )

    helper_calls = []

    async def _eval_script(
        _script: str,
        keys,
        args,
        timeout: float | None = None,
    ):
        helper_calls.append((list(keys), list(args), timeout))
        return 1

    monkeypatch.setattr(
        run_agent_background_module.redis,
        "eval_script",
        _eval_script,
    )

    await run_agent_background_module._push_response_with_retry(
        "agent_run:run-timeout:responses",
        "agent_run:run-timeout:new_response",
        "{}",
        agent_run_id="run-timeout",
    )

    assert helper_calls == [
        (
            ["agent_run:run-timeout:responses", "agent_run:run-timeout:new_response"],
            ["{}", "86400", "new"],
            4.25,
        )
    ]


@pytest.mark.asyncio
async def test_execute_regular_run_kernel_uses_external_control_plane_slot_without_pubsub(
    monkeypatch,
):
    run_id = "run-control-plane"
    control_plane_slot = _FakeExternalControlPlaneSlot()
    stop_trip_started = asyncio.Event()

    async def _looping_run_agent(*_args, **_kwargs):
        stop_trip_started.set()
        while True:
            await asyncio.sleep(0.01)
            yield {"type": "assistant", "content": "tick"}

    harness = _install_run_agent_actor_test_harness(
        monkeypatch,
        run_id=run_id,
        run_agent_impl=_looping_run_agent,
    )

    async def _unexpected_create_pubsub():
        raise AssertionError(
            "pubsub should not be opened when an external control plane slot is supplied"
        )

    monkeypatch.setattr(
        run_agent_background_module.redis,
        "create_pubsub",
        _unexpected_create_pubsub,
        raising=False,
    )

    async def _trip_stop():
        await stop_trip_started.wait()
        await asyncio.sleep(0.03)
        control_plane_slot.stop_requested = True

    stop_task = asyncio.create_task(_trip_stop())
    try:
        await run_agent_background_module.execute_regular_run_kernel(
            agent_run_id=run_id,
            thread_id="thread-test",
            instance_id="instance-test",
            project_id="project-test",
            model_name="openai/gpt-4.1",
            enable_thinking=False,
            reasoning_effort=None,
            stream=True,
            enable_context_manager=False,
            agent_config={},
            is_agent_builder=False,
            target_agent_id=None,
            request_id="request-test",
            resume_strategy="auto",
            resume_window_minutes=1440,
            shadow_clone_mode="off",
            shadow_clone_main_model=None,
            shadow_clone_subagent_model=None,
            owner_token="sup-1:slot-1",
            control_plane_slot=control_plane_slot,
        )
    finally:
        stop_task.cancel()
        await asyncio.gather(stop_task, return_exceptions=True)

    assert callable(control_plane_slot.refresh_callback)
    assert (
        _extract_statuses_from_pushed_payloads(harness.pushed_payloads)[-1] == "stopped"
    )
    assert harness.rows[0]["status"] == "stopped"


@pytest.mark.asyncio
async def test_execute_regular_run_kernel_persists_shadow_clone_v2_final_assistant_message_to_thread_messages(
    monkeypatch,
):
    run_id = "run-v2-final-assistant"
    thread_id = "thread-v2-final-assistant"
    final_content = {
        "role": "assistant",
        "content": "MAIN_CHAT_PANEL_VISIBLE_FINAL_OUTPUT",
    }
    final_metadata = {
        "stream_status": "complete",
        "agent_run_id": run_id,
        "shadow_clone_mode": "v2",
        "activity_owner": "main_agent",
        "phase_reason": "shadow_clone_v2_final_output",
    }

    async def _v2_run_agent(*_args, **_kwargs):
        yield {
            "sequence": 7,
            "message_id": "shadow-clone-v2-final-run-v2-final-assistant",
            "thread_id": thread_id,
            "project_id": "project-test",
            "type": "assistant",
            "role": "assistant",
            "is_llm_message": True,
            "content": json.dumps(final_content, ensure_ascii=False),
            "metadata": json.dumps(final_metadata, ensure_ascii=False),
            "created_at": "2026-06-03T10:00:00+00:00",
            "updated_at": "2026-06-03T10:00:00+00:00",
        }
        yield {"type": "status", "status": "completed", "message": "done"}

    harness = _install_run_agent_actor_test_harness(
        monkeypatch,
        run_id=run_id,
        run_agent_impl=_v2_run_agent,
    )

    await run_agent_background_module.execute_regular_run_kernel(
        agent_run_id=run_id,
        thread_id=thread_id,
        instance_id="instance-test",
        project_id="project-test",
        model_name="openai/gpt-4.1",
        enable_thinking=False,
        reasoning_effort=None,
        stream=True,
        enable_context_manager=False,
        agent_config={},
        is_agent_builder=False,
        target_agent_id=None,
        request_id="request-test",
        resume_strategy="auto",
        resume_window_minutes=1440,
        shadow_clone_mode="v2",
        shadow_clone_main_model=None,
        shadow_clone_subagent_model=None,
    )

    assert harness.rows[0]["status"] == "completed"
    assert len(harness.client.message_inserts) == 1
    inserted = harness.client.message_inserts[0]
    uuid.UUID(inserted["message_id"])
    assert inserted["message_id"] != "shadow-clone-v2-final-run-v2-final-assistant"
    assert inserted["thread_id"] == thread_id
    assert inserted["project_id"] == "project-test"
    assert inserted["type"] == "assistant"
    assert inserted["role"] == "assistant"
    assert inserted["is_llm_message"] is True
    assert inserted["content"] == json.dumps(final_content, ensure_ascii=False)
    inserted_metadata = json.loads(inserted["metadata"])
    assert inserted_metadata == {
        **final_metadata,
        "shadow_clone_v2_source_message_id": (
            "shadow-clone-v2-final-run-v2-final-assistant"
        ),
    }
    assert inserted["created_at"].isoformat() == "2026-06-03T10:00:00+00:00"
    assert inserted["updated_at"].isoformat() == "2026-06-03T10:00:00+00:00"


@pytest.mark.asyncio
async def test_execute_regular_run_kernel_persists_shadow_clone_v2_planning_assistant_message_to_thread_messages(
    monkeypatch,
):
    run_id = "run-v2-planning-assistant"
    thread_id = "thread-v2-planning-assistant"
    planning_content = {
        "role": "assistant",
        "content": "MAIN_PLANNING_NATURAL_LANGUAGE_OK visible after refresh",
    }
    planning_metadata = {
        "stream_status": "complete",
        "agent_run_id": run_id,
        "shadow_clone_mode": "v2",
        "activity_owner": "main_agent",
        "ui_phase": "planning",
        "phase_reason": "shadow_clone_v2_main_agent_planning_output",
    }

    async def _v2_run_agent(*_args, **_kwargs):
        yield {
            "sequence": 3,
            "message_id": "shadow-clone-v2-planning-run-v2-planning-assistant",
            "thread_id": thread_id,
            "project_id": "project-test",
            "type": "assistant",
            "role": "assistant",
            "is_llm_message": True,
            "content": json.dumps(planning_content, ensure_ascii=False),
            "metadata": json.dumps(planning_metadata, ensure_ascii=False),
            "created_at": "2026-06-03T09:00:00+00:00",
            "updated_at": "2026-06-03T09:00:00+00:00",
        }
        yield {"type": "status", "status": "completed", "message": "done"}

    harness = _install_run_agent_actor_test_harness(
        monkeypatch,
        run_id=run_id,
        run_agent_impl=_v2_run_agent,
    )

    await run_agent_background_module.execute_regular_run_kernel(
        agent_run_id=run_id,
        thread_id=thread_id,
        instance_id="instance-test",
        project_id="project-test",
        model_name="openai/gpt-4.1",
        enable_thinking=False,
        reasoning_effort=None,
        stream=True,
        enable_context_manager=False,
        agent_config={},
        is_agent_builder=False,
        target_agent_id=None,
        request_id="request-test",
        resume_strategy="auto",
        resume_window_minutes=1440,
        shadow_clone_mode="v2",
        shadow_clone_main_model=None,
        shadow_clone_subagent_model=None,
    )

    assert harness.rows[0]["status"] == "completed"
    assert len(harness.client.message_inserts) == 1
    inserted = harness.client.message_inserts[0]
    uuid.UUID(inserted["message_id"])
    assert inserted["message_id"] != "shadow-clone-v2-planning-run-v2-planning-assistant"
    assert inserted["thread_id"] == thread_id
    assert inserted["project_id"] == "project-test"
    assert inserted["type"] == "assistant"
    assert inserted["role"] == "assistant"
    assert inserted["is_llm_message"] is True
    assert inserted["content"] == json.dumps(planning_content, ensure_ascii=False)
    inserted_metadata = json.loads(inserted["metadata"])
    assert inserted_metadata == {
        **planning_metadata,
        "shadow_clone_v2_source_message_id": (
            "shadow-clone-v2-planning-run-v2-planning-assistant"
        ),
    }
    assert inserted["created_at"].isoformat() == "2026-06-03T09:00:00+00:00"
    assert inserted["updated_at"].isoformat() == "2026-06-03T09:00:00+00:00"


@pytest.mark.asyncio
async def test_execute_regular_run_kernel_pushes_shadow_clone_v2_opening_subagent_activity_to_response_list(
    monkeypatch,
):
    run_id = "run-v2-opening-subagent-activity"
    thread_id = "thread-v2-opening-subagent-activity"
    opening_activity = {
        "type": "subagent_activity",
        "status": "running",
        "source": "shadow_clone_v2",
        "shadow_clone_mode": "v2",
        "thread_run_id": "thread-run-v2-opening",
        "agent_run_id": run_id,
        "thread_id": thread_id,
        "project_id": "project-test",
        "ui_phase": "subagents_running",
        "activity_owner": "shadow_clone",
        "phase_reason": "shadow_clone_v2_subagent_activity",
        "subtask_id": "realtime-agent-1-task",
        "sequence": 21,
        "role": "writer",
        "agent_name": "realtime-agent-1",
        "message_type": "assistant",
        "content": {
            "role": "assistant",
            "content": (
                "SUB_STREAM_MARKER_realtime_agent_1_1780613951313 "
                "starting now."
            ),
        },
        "metadata": {
            "stream_status": "chunk",
            "opening_progress": True,
            "source": "shadow_clone_v2",
            "subtask_id": "realtime-agent-1-task",
            "agent_name": "realtime-agent-1",
        },
        "created_at": "2026-06-04T10:00:00+00:00",
        "updated_at": "2026-06-04T10:00:00+00:00",
    }

    async def _v2_run_agent(*_args, **_kwargs):
        yield opening_activity
        yield {"type": "status", "status": "completed", "message": "done"}

    harness = _install_run_agent_actor_test_harness(
        monkeypatch,
        run_id=run_id,
        run_agent_impl=_v2_run_agent,
    )

    await run_agent_background_module.execute_regular_run_kernel(
        agent_run_id=run_id,
        thread_id=thread_id,
        instance_id="instance-test",
        project_id="project-test",
        model_name="openai/gpt-4.1",
        enable_thinking=False,
        reasoning_effort=None,
        stream=True,
        enable_context_manager=False,
        agent_config={},
        is_agent_builder=False,
        target_agent_id=None,
        request_id="request-test",
        resume_strategy="auto",
        resume_window_minutes=1440,
        shadow_clone_mode="v2",
        shadow_clone_main_model=None,
        shadow_clone_subagent_model=None,
    )

    pushed_payloads = [json.loads(raw) for raw in harness.pushed_payloads]
    pushed_opening = [
        payload
        for payload in pushed_payloads
        if payload.get("type") == "subagent_activity"
        and payload.get("metadata", {}).get("opening_progress") is True
    ]
    assert pushed_opening == [opening_activity]
    assert harness.pushed_response_keys[0] == (
        f"agent_run:{run_id}:responses"
    )
    assert "SUB_STREAM_MARKER_realtime_agent_1_1780613951313" in (
        pushed_opening[0]["content"]["content"]
    )


@pytest.mark.asyncio
async def test_persist_shadow_clone_v2_main_agent_assistant_message_supports_async_insert_client():
    client = _AsyncInsertMessagesClient()
    metadata = {
        "stream_status": "complete",
        "activity_owner": "main_agent",
        "phase_reason": "shadow_clone_v2_main_agent_planning_output",
    }

    await run_agent_background_module._persist_shadow_clone_v2_main_agent_assistant_message(
        client,
        {
            "message_id": "shadow-clone-v2-planning-async-insert",
            "thread_id": "thread-async-insert",
            "project_id": "project-async-insert",
            "type": "assistant",
            "role": "assistant",
            "is_llm_message": True,
            "content": "MAIN_PLANNING_NATURAL_LANGUAGE_OK async insert client",
            "metadata": json.dumps(metadata, ensure_ascii=False),
            "created_at": "2026-06-04T10:00:00+00:00",
        },
    )

    assert len(client.message_inserts) == 1
    inserted = client.message_inserts[0]
    uuid.UUID(inserted["message_id"])
    assert inserted["message_id"] != "shadow-clone-v2-planning-async-insert"
    assert inserted["thread_id"] == "thread-async-insert"
    assert inserted["project_id"] == "project-async-insert"
    assert inserted["type"] == "assistant"
    assert inserted["role"] == "assistant"
    assert inserted["is_llm_message"] is True
    assert inserted["content"] == "MAIN_PLANNING_NATURAL_LANGUAGE_OK async insert client"
    inserted_metadata = json.loads(inserted["metadata"])
    assert inserted_metadata == {
        **metadata,
        "shadow_clone_v2_source_message_id": "shadow-clone-v2-planning-async-insert",
    }
    assert inserted["created_at"].isoformat() == "2026-06-04T10:00:00+00:00"
    assert inserted["updated_at"].isoformat() == "2026-06-04T10:00:00+00:00"


@pytest.mark.asyncio
async def test_persist_shadow_clone_v2_main_agent_assistant_message_maps_synthetic_message_id_to_uuid():
    client = _UuidValidatingAsyncInsertMessagesClient()
    synthetic_message_id = (
        "shadow-clone-v2-planning-85b3a26d-72d5-4af4-87f8-6bbca62e561c-"
        "a64bac2fb358"
    )

    await run_agent_background_module._persist_shadow_clone_v2_main_agent_assistant_message(
        client,
        {
            "message_id": synthetic_message_id,
            "thread_id": "thread-uuid-mapped",
            "project_id": "project-uuid-mapped",
            "type": "assistant",
            "role": "assistant",
            "is_llm_message": True,
            "content": "MAIN_PLANNING_NATURAL_LANGUAGE_OK uuid mapped",
            "metadata": json.dumps(
                {
                    "stream_status": "complete",
                    "activity_owner": "main_agent",
                    "phase_reason": "shadow_clone_v2_main_agent_planning_output",
                },
                ensure_ascii=False,
            ),
            "created_at": "2026-06-04T10:00:00+00:00",
        },
    )

    assert len(client.message_inserts) == 1
    inserted = client.message_inserts[0]
    uuid.UUID(inserted["message_id"])
    assert inserted["message_id"] != synthetic_message_id
    metadata = json.loads(inserted["metadata"])
    assert metadata["shadow_clone_v2_source_message_id"] == synthetic_message_id


@pytest.mark.asyncio
async def test_persist_shadow_clone_v2_main_agent_assistant_message_uses_production_insert_shape():
    client = _ProductionShapeMessagesClient()

    await run_agent_background_module._persist_shadow_clone_v2_main_agent_assistant_message(
        client,
        {
            "message_id": "shadow-clone-v2-final-production-shape",
            "thread_id": "thread-production-shape",
            "project_id": "project-production-shape",
            "type": "assistant",
            "role": "assistant",
            "is_llm_message": True,
            "content": "MAIN_FINAL_NATURAL_LANGUAGE_OK production shape",
            "metadata": json.dumps(
                {
                    "stream_status": "complete",
                    "activity_owner": "main_agent",
                    "phase_reason": "shadow_clone_v2_final_output",
                },
                ensure_ascii=False,
            ),
            "created_at": "2026-06-04T10:00:00+00:00",
            "updated_at": "2026-06-04T10:00:01+00:00",
        },
    )

    inserted = client.message_inserts[0]
    assert inserted["created_at"].isoformat() == "2026-06-04T10:00:00+00:00"
    assert inserted["updated_at"].isoformat() == "2026-06-04T10:00:01+00:00"


@pytest.mark.asyncio
async def test_execute_regular_run_kernel_uses_load_harness_stub_without_calling_run_agent(
    monkeypatch,
):
    run_id = "run-harness-stub"
    artifact_calls: list[tuple[str, str, str]] = []

    async def _unexpected_run_agent(*_args, **_kwargs):
        raise AssertionError("run_agent should not be called in load-harness stub mode")

    async def _fake_persist_artifact(**kwargs):
        artifact_calls.append(
            (
                kwargs["agent_run_id"],
                kwargs["project_id"],
                kwargs["path"],
            )
        )
        return object()

    async def _unexpected_timeout_lease_loop(*_args, **_kwargs):
        raise AssertionError(
            "sandbox timeout lease loop must not start in load-harness stub mode"
        )

    harness = _install_run_agent_actor_test_harness(
        monkeypatch,
        run_id=run_id,
        run_agent_impl=_unexpected_run_agent,
    )
    monkeypatch.setenv("REGULAR_LOAD_HARNESS_STUB_MODE", "true")
    monkeypatch.setattr(
        run_agent_background_module.workspace_artifacts,
        "persist_artifact",
        _fake_persist_artifact,
        raising=False,
    )
    monkeypatch.setattr(
        run_agent_background_module,
        "_sandbox_timeout_lease_loop",
        _unexpected_timeout_lease_loop,
    )
    monkeypatch.setattr(
        run_agent_background_module,
        "_is_current_attempt_epoch",
        lambda *_args, **_kwargs: True,
    )

    final_status = await run_agent_background_module.execute_regular_run_kernel(
        client=harness.client,
        agent_run_id=run_id,
        thread_id="thread-test",
        instance_id="instance-test",
        project_id="project-test",
        model_name="openrouter/minimax/minimax-m2.5",
        enable_thinking=False,
        reasoning_effort=None,
        stream=True,
        enable_context_manager=False,
        agent_config={},
        is_agent_builder=False,
        target_agent_id=None,
        request_id="request-test",
        resume_strategy="auto",
        resume_window_minutes=1440,
        shadow_clone_mode="off",
        shadow_clone_main_model=None,
        shadow_clone_subagent_model=None,
        owner_token="sup-1:slot-1",
        attempt_id="attempt-1",
        execution_epoch=1,
        run_metadata={
            "regular_load_harness": {
                "enabled": True,
                "batch_id": "batch-1",
                "target_tier": 25,
                "active_duration_seconds": 0.03,
                "chunk_interval_seconds": 0.01,
                "emit_stub_response": True,
                "artifact_path": "/workspace/load-harness/result.txt",
                "artifact_content": "ok",
            }
        },
    )

    assert final_status == "completed"
    assert artifact_calls == [
        ("run-harness-stub", "project-test", "/workspace/load-harness/result.txt")
    ]
    assert (
        _extract_statuses_from_pushed_payloads(harness.pushed_payloads)[-1]
        == "completed"
    )


@pytest.mark.asyncio
async def test_execute_claimed_regular_run_from_pool_passes_harness_user_message_override_from_metadata(
    monkeypatch,
):
    captured = {}

    async def _fake_execute_regular_run_kernel(**kwargs):
        captured.update(kwargs)
        return "completed"

    monkeypatch.setattr(
        run_agent_background_module,
        "execute_regular_run_kernel",
        _fake_execute_regular_run_kernel,
    )

    status = await run_agent_background_module._execute_claimed_regular_run_from_pool(
        client=object(),
        owner_token="sup-1:slot-1",
        run_row={
            "agent_run_id": "run-1",
            "thread_id": "thread-1",
            "project_id": "project-1",
            "metadata": {
                "model_name": "openai/gpt-4.1",
                "regular_load_harness": {
                    "enabled": True,
                    "user_message_override": "Reply with LOAD-HARNESS-OK and nothing else.",
                },
            },
        },
    )

    assert status == "completed"
    assert (
        captured["user_message_override"]
        == "Reply with LOAD-HARNESS-OK and nothing else."
    )


@pytest.mark.asyncio
async def test_execute_regular_run_kernel_passes_harness_user_message_override_to_run_agent_when_stub_mode_is_off(
    monkeypatch,
):
    run_id = "run-harness-real"
    captured_run_agent_kwargs = {}

    async def _capturing_run_agent(*_args, **kwargs):
        captured_run_agent_kwargs.update(kwargs)
        yield {
            "type": "status",
            "status": "completed",
            "message": "done",
        }

    harness = _install_run_agent_actor_test_harness(
        monkeypatch,
        run_id=run_id,
        run_agent_impl=_capturing_run_agent,
    )
    monkeypatch.delenv("REGULAR_LOAD_HARNESS_STUB_MODE", raising=False)
    monkeypatch.setattr(
        run_agent_background_module,
        "_is_current_attempt_epoch",
        lambda *_args, **_kwargs: True,
    )

    final_status = await run_agent_background_module.execute_regular_run_kernel(
        client=harness.client,
        agent_run_id=run_id,
        thread_id="thread-test",
        instance_id="instance-test",
        project_id="project-test",
        model_name="openrouter/minimax/minimax-m2.5",
        enable_thinking=False,
        reasoning_effort=None,
        stream=True,
        enable_context_manager=False,
        agent_config={},
        is_agent_builder=False,
        target_agent_id=None,
        request_id="request-test",
        resume_strategy="auto",
        resume_window_minutes=1440,
        shadow_clone_mode="off",
        shadow_clone_main_model=None,
        shadow_clone_subagent_model=None,
        owner_token="sup-1:slot-1",
        attempt_id="attempt-1",
        execution_epoch=1,
        run_metadata={
            "regular_load_harness": {
                "enabled": True,
                "execution_profile": "real_sandbox_write_once",
                "user_message_override": (
                    "Use the sandbox to create /workspace/load-harness/proof.txt "
                    "with the text ok, then reply DONE."
                ),
            }
        },
    )

    assert final_status == "completed"
    assert (
        captured_run_agent_kwargs["user_message_override"]
        == "Use the sandbox to create /workspace/load-harness/proof.txt "
        "with the text ok, then reply DONE."
    )


@pytest.mark.asyncio
async def test_execute_regular_run_kernel_aborts_real_harness_generated_response_loop(
    monkeypatch,
):
    run_id = "run-harness-response-loop"
    generator_closed = {"value": False}

    async def _looping_run_agent(*_args, **_kwargs):
        try:
            while True:
                yield {"type": "assistant", "content": "tick"}
        finally:
            generator_closed["value"] = True

    harness = _install_run_agent_actor_test_harness(
        monkeypatch,
        run_id=run_id,
        run_agent_impl=_looping_run_agent,
    )
    monkeypatch.delenv("REGULAR_LOAD_HARNESS_STUB_MODE", raising=False)
    monkeypatch.setattr(
        run_agent_background_module,
        "_is_current_attempt_epoch",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        run_agent_background_module,
        "MAX_GENERATED_RESPONSES",
        3,
        raising=False,
    )

    final_status = await run_agent_background_module.execute_regular_run_kernel(
        client=harness.client,
        agent_run_id=run_id,
        thread_id="thread-test",
        instance_id="instance-test",
        project_id="project-test",
        model_name="deepseek-v4-pro",
        enable_thinking=True,
        reasoning_effort="max",
        stream=True,
        enable_context_manager=False,
        agent_config={},
        is_agent_builder=False,
        target_agent_id=None,
        request_id="request-test",
        resume_strategy="auto",
        resume_window_minutes=1440,
        shadow_clone_mode="off",
        shadow_clone_main_model=None,
        shadow_clone_subagent_model=None,
        owner_token="sup-1:slot-1",
        control_plane_slot=_FakeExternalControlPlaneSlot(),
        run_metadata={
            "regular_load_harness": {
                "enabled": True,
                "execution_profile": "real_provider_text",
                "active_duration_seconds": 0.0,
                "user_message_override": "Loop forever unless guarded.",
            }
        },
    )

    assert final_status == "failed"
    assert generator_closed["value"] is True
    assert len(harness.pushed_payloads) <= 5
    assert any(
        "generated response count" in payload for payload in harness.pushed_payloads
    ), harness.pushed_payloads


@pytest.mark.asyncio
async def test_execute_regular_run_kernel_allows_eval_metadata_generated_response_override(
    monkeypatch,
):
    run_id = "run-harness-response-override"
    yielded = {"count": 0}

    async def _finite_run_agent(*_args, **_kwargs):
        for index in range(5):
            yielded["count"] += 1
            yield {"type": "assistant", "content": f"tick {index}"}
        yield {"type": "status", "status": "completed", "message": "done"}

    harness = _install_run_agent_actor_test_harness(
        monkeypatch,
        run_id=run_id,
        run_agent_impl=_finite_run_agent,
    )
    monkeypatch.delenv("REGULAR_LOAD_HARNESS_STUB_MODE", raising=False)
    monkeypatch.setattr(
        run_agent_background_module,
        "_is_current_attempt_epoch",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        run_agent_background_module,
        "MAX_GENERATED_RESPONSES",
        3,
        raising=False,
    )

    final_status = await run_agent_background_module.execute_regular_run_kernel(
        client=harness.client,
        agent_run_id=run_id,
        thread_id="thread-test",
        instance_id="instance-test",
        project_id="project-test",
        model_name="deepseek-v4-pro",
        enable_thinking=True,
        reasoning_effort="max",
        stream=True,
        enable_context_manager=False,
        agent_config={},
        is_agent_builder=False,
        target_agent_id=None,
        request_id="request-test",
        resume_strategy="auto",
        resume_window_minutes=1440,
        shadow_clone_mode="off",
        shadow_clone_main_model=None,
        shadow_clone_subagent_model=None,
        owner_token="sup-1:slot-1",
        control_plane_slot=_FakeExternalControlPlaneSlot(),
        run_metadata={
            "eval_max_generated_responses": 8,
            "regular_load_harness": {
                "enabled": True,
                "execution_profile": "real_provider_text",
                "active_duration_seconds": 0.0,
                "user_message_override": "Emit more chunks than the production default.",
            },
        },
    )

    assert final_status == "completed"
    assert yielded["count"] == 5
    assert not any(
        "generated response count" in payload for payload in harness.pushed_payloads
    ), harness.pushed_payloads


@pytest.mark.asyncio
async def test_execute_regular_run_kernel_holds_real_harness_completion_until_active_duration_elapsed(
    monkeypatch,
):
    run_id = "run-harness-hold"
    sleep_calls: list[float] = []
    fake_now = {"value": 0.0}

    async def _fast_completed_run_agent(*_args, **_kwargs):
        yield {
            "type": "status",
            "status": "completed",
            "message": "done",
        }

    async def _fake_sleep(seconds: float):
        sleep_calls.append(seconds)
        fake_now["value"] += seconds

    harness = _install_run_agent_actor_test_harness(
        monkeypatch,
        run_id=run_id,
        run_agent_impl=_fast_completed_run_agent,
    )
    monkeypatch.delenv("REGULAR_LOAD_HARNESS_STUB_MODE", raising=False)
    monkeypatch.setattr(
        run_agent_background_module,
        "_is_current_attempt_epoch",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(run_agent_background_module.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(
        run_agent_background_module.time,
        "monotonic",
        lambda: fake_now["value"],
    )

    final_status = await run_agent_background_module.execute_regular_run_kernel(
        client=harness.client,
        agent_run_id=run_id,
        thread_id="thread-test",
        instance_id="instance-test",
        project_id="project-test",
        model_name="openrouter/minimax/minimax-m2.5",
        enable_thinking=False,
        reasoning_effort=None,
        stream=True,
        enable_context_manager=False,
        agent_config={},
        is_agent_builder=False,
        target_agent_id=None,
        request_id="request-test",
        resume_strategy="auto",
        resume_window_minutes=1440,
        shadow_clone_mode="off",
        shadow_clone_main_model=None,
        shadow_clone_subagent_model=None,
        owner_token="sup-1:slot-1",
        attempt_id="attempt-1",
        execution_epoch=1,
        control_plane_slot=_FakeExternalControlPlaneSlot(),
        run_metadata={
            "regular_load_harness": {
                "enabled": True,
                "execution_profile": "real_provider_text",
                "active_duration_seconds": 0.5,
                "user_message_override": "Reply with LOAD-HARNESS-OK and nothing else.",
            }
        },
    )

    assert final_status == "completed"
    assert sleep_calls
    assert sum(sleep_calls) >= 0.5


@pytest.mark.asyncio
async def test_execute_regular_run_kernel_still_honors_stop_during_real_harness_active_window(
    monkeypatch,
):
    run_id = "run-harness-hold-stop"
    sleep_calls: list[float] = []
    fake_now = {"value": 0.0}
    control_plane_slot = _FakeExternalControlPlaneSlot()

    async def _fast_completed_run_agent(*_args, **_kwargs):
        yield {
            "type": "status",
            "status": "completed",
            "message": "done",
        }

    async def _fake_sleep(seconds: float):
        sleep_calls.append(seconds)
        fake_now["value"] += seconds
        control_plane_slot.stop_requested = True

    harness = _install_run_agent_actor_test_harness(
        monkeypatch,
        run_id=run_id,
        run_agent_impl=_fast_completed_run_agent,
    )
    monkeypatch.delenv("REGULAR_LOAD_HARNESS_STUB_MODE", raising=False)
    monkeypatch.setattr(
        run_agent_background_module,
        "_is_current_attempt_epoch",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(run_agent_background_module.asyncio, "sleep", _fake_sleep)
    monkeypatch.setattr(
        run_agent_background_module.time,
        "monotonic",
        lambda: fake_now["value"],
    )

    final_status = await run_agent_background_module.execute_regular_run_kernel(
        client=harness.client,
        agent_run_id=run_id,
        thread_id="thread-test",
        instance_id="instance-test",
        project_id="project-test",
        model_name="openai/gpt-4.1",
        enable_thinking=False,
        reasoning_effort=None,
        stream=True,
        enable_context_manager=False,
        agent_config={},
        is_agent_builder=False,
        target_agent_id=None,
        request_id="request-test",
        resume_strategy="auto",
        resume_window_minutes=1440,
        shadow_clone_mode="off",
        shadow_clone_main_model=None,
        shadow_clone_subagent_model=None,
        owner_token="sup-1:slot-1",
        attempt_id="attempt-1",
        execution_epoch=1,
        control_plane_slot=control_plane_slot,
        run_metadata={
            "regular_load_harness": {
                "enabled": True,
                "execution_profile": "real_provider_text",
                "active_duration_seconds": 0.5,
                "user_message_override": "Reply with LOAD-HARNESS-OK and nothing else.",
            }
        },
    )

    assert final_status == "stopped"
    assert sleep_calls


@pytest.mark.asyncio
async def test_cleanup_redis_response_list_uses_configured_terminal_ttl(monkeypatch):
    monkeypatch.setattr(
        run_agent_background_module,
        "REDIS_RESPONSE_LIST_TTL",
        4321,
    )

    expire_calls = []

    async def _expire(key: str, seconds: int, *, timeout: float | None = None):
        expire_calls.append((key, seconds, timeout))
        return 1

    monkeypatch.setattr(run_agent_background_module.redis, "expire", _expire)

    await run_agent_background_module._cleanup_redis_response_list("run-cleanup")

    assert expire_calls == [("agent_run:run-cleanup:responses", 4321, None)]


@pytest.mark.asyncio
async def test_refresh_redis_run_lock_uses_short_run_lock_ttl(monkeypatch):
    monkeypatch.setattr(run_agent_background_module, "RUN_LOCK_TTL_SECONDS", 300)

    eval_calls = []

    async def _eval_script(_script: str, keys, args, *, timeout: float | None = None):
        eval_calls.append((list(keys), list(args), timeout))
        return 1

    monkeypatch.setattr(run_agent_background_module.redis, "eval_script", _eval_script)

    result = await run_agent_background_module._refresh_redis_run_lock(
        "run-lock",
        "owner-token",
    )

    assert result == "refreshed"
    assert eval_calls == [(["agent_run_lock:run-lock"], ["owner-token", "300"], None)]


@pytest.mark.asyncio
async def test_take_over_redis_run_lock_uses_compare_and_swap(monkeypatch):
    monkeypatch.setattr(run_agent_background_module, "RUN_LOCK_TTL_SECONDS", 300)

    eval_calls = []

    async def _eval_script(_script: str, keys, args, *, timeout: float | None = None):
        eval_calls.append((list(keys), list(args), timeout))
        return 1

    monkeypatch.setattr(run_agent_background_module.redis, "eval_script", _eval_script)

    result = await run_agent_background_module._take_over_redis_run_lock(
        "run-lock",
        expected_owner_token="stale-owner",
        new_owner_token="new-owner",
    )

    assert result == "taken_over"
    assert eval_calls == [
        (["agent_run_lock:run-lock"], ["stale-owner", "new-owner", "300"], None)
    ]


@pytest.mark.asyncio
async def test_get_final_responses_best_effort_returns_fallback_on_replay_timeout(
    monkeypatch,
):
    fallback_responses = ['{"type":"status","status":"completed"}']

    async def _timeout_lrange(
        _key: str,
        _start: int,
        _end: int,
        *,
        timeout: float | None = None,
    ):
        raise asyncio.TimeoutError("redis replay timeout")

    monkeypatch.setattr(run_agent_background_module.redis, "lrange", _timeout_lrange)

    responses = await run_agent_background_module._get_final_responses_best_effort(
        "agent_run:run-replay:responses",
        agent_run_id="run-replay",
        fallback_responses=fallback_responses,
    )

    assert responses == fallback_responses


@pytest.mark.asyncio
async def test_get_final_responses_best_effort_replays_redis_when_fallback_missing(
    monkeypatch,
):
    replayed_responses = ['{"type":"status","status":"completed"}']

    async def _replay_lrange(_key: str, _start: int, _end: int):
        return replayed_responses

    monkeypatch.setattr(run_agent_background_module.redis, "lrange", _replay_lrange)

    responses = await run_agent_background_module._get_final_responses_best_effort(
        "agent_run:run-replay:responses",
        agent_run_id="run-replay",
        fallback_responses=[],
    )

    assert responses == replayed_responses


@pytest.mark.asyncio
async def test_get_final_responses_best_effort_skips_redis_replay_when_fallback_present(
    monkeypatch,
):
    fallback_responses = [
        '{"type":"assistant","content":"hello"}',
        '{"type":"status","status":"completed"}',
    ]

    call_count = {"lrange": 0}

    async def _unexpected_lrange(*_args, **_kwargs):
        call_count["lrange"] += 1
        raise AssertionError(
            "redis replay should be skipped when fallback responses are present"
        )

    monkeypatch.setattr(run_agent_background_module.redis, "lrange", _unexpected_lrange)

    responses = await run_agent_background_module._get_final_responses_best_effort(
        "agent_run:run-local:responses",
        agent_run_id="run-local",
        fallback_responses=fallback_responses,
    )

    assert call_count["lrange"] == 0
    assert responses == fallback_responses


def test_qwen_model_detection_and_sandbox_error_detection():
    assert run_agent_background_module._is_qwen_35_model("dashscope/qwen3.5-plus")
    assert not run_agent_background_module._is_qwen_35_model("openai/gpt-4.1")

    payload = {
        "type": "status",
        "message": "write_file failed: The sandbox was not found",
    }
    assert run_agent_background_module._response_contains_sandbox_not_found(payload)


def test_terminal_status_normalization_and_priority_merge():
    assert run_agent_background_module._normalize_terminal_status("error") == "failed"
    assert (
        run_agent_background_module._normalize_terminal_status("completed")
        == "completed"
    )
    assert (
        run_agent_background_module._normalize_terminal_status("unknown") == "running"
    )

    assert (
        run_agent_background_module._merge_terminal_status(
            "failed",
            "completed",
        )
        == "failed"
    )
    assert (
        run_agent_background_module._merge_terminal_status(
            "running",
            "completed",
            error_present=True,
        )
        == "failed"
    )
    assert (
        run_agent_background_module._merge_terminal_status(
            "stopped",
            "completed",
        )
        == "stopped"
    )


def test_finalize_local_terminal_status_prefers_stop_over_late_completion():
    assert (
        run_agent_background_module._finalize_local_terminal_status(
            "running",
            external_stop_requested=True,
            control_plane_failure=False,
            error_message=None,
            saw_error_status_message=False,
        )
        == "stopped"
    )
    assert (
        run_agent_background_module._finalize_local_terminal_status(
            "completed",
            external_stop_requested=True,
            control_plane_failure=False,
            error_message=None,
            saw_error_status_message=False,
        )
        == "stopped"
    )
    assert (
        run_agent_background_module._finalize_local_terminal_status(
            "running",
            external_stop_requested=True,
            control_plane_failure=False,
            error_message="worker failed",
            saw_error_status_message=False,
        )
        == "stopped"
    )
    assert (
        run_agent_background_module._finalize_local_terminal_status(
            "failed",
            external_stop_requested=True,
            control_plane_failure=False,
            error_message="worker failed",
            saw_error_status_message=False,
        )
        == "failed"
    )


def test_finalize_local_terminal_status_preserves_internal_control_plane_failure():
    assert (
        run_agent_background_module._finalize_local_terminal_status(
            "running",
            external_stop_requested=False,
            control_plane_failure=True,
            error_message=None,
            saw_error_status_message=False,
        )
        == "failed"
    )


@pytest.mark.asyncio
async def test_merge_with_persisted_agent_run_status_preserves_external_stop():
    rows = [{"agent_run_id": "run-merge-stop", "status": "stopped"}]
    client = _FakeAgentRunsClient(rows)

    status = await run_agent_background_module._merge_with_persisted_agent_run_status(
        client,
        "run-merge-stop",
        "completed",
    )

    assert status == "stopped"


@pytest.mark.asyncio
async def test_merge_with_persisted_agent_run_status_preserves_external_stop_against_late_failed_cleanup():
    rows = [{"agent_run_id": "run-merge-stop-failed", "status": "stopped"}]
    client = _FakeAgentRunsClient(rows)

    status = await run_agent_background_module._merge_with_persisted_agent_run_status(
        client,
        "run-merge-stop-failed",
        "failed",
        error_present=True,
    )

    assert status == "stopped"


@pytest.mark.asyncio
async def test_persist_and_resolve_final_agent_run_status_uses_latest_persisted_truth(
    monkeypatch,
):
    rows = [{"agent_run_id": "run-authoritative-stop", "status": "running"}]
    client = _FakeAgentRunsClient(rows)

    async def _fake_update_status(
        _client,
        _run_id,
        _status,
        error=None,
        attempt_id=None,
        execution_epoch=None,
    ):
        rows[0]["status"] = "stopped"
        return True

    monkeypatch.setattr(
        run_agent_background_module,
        "update_agent_run_status",
        _fake_update_status,
    )

    resolved_status = (
        await run_agent_background_module._persist_and_resolve_final_agent_run_status(
            client,
            "run-authoritative-stop",
            "completed",
            error_message=None,
        )
    )

    assert resolved_status == "stopped"


@pytest.mark.asyncio
async def test_persist_and_resolve_final_agent_run_status_force_updates_when_initial_write_returns_false(
    monkeypatch,
):
    rows = [{"agent_run_id": "run-force-complete", "status": "running"}]
    client = _FakeAgentRunsClient(rows)

    async def _fake_update_status(
        _client,
        _run_id,
        _status,
        error=None,
        attempt_id=None,
        execution_epoch=None,
    ):
        return False

    monkeypatch.setattr(
        run_agent_background_module,
        "update_agent_run_status",
        _fake_update_status,
    )

    resolved_status = (
        await run_agent_background_module._persist_and_resolve_final_agent_run_status(
            client,
            "run-force-complete",
            "completed",
            error_message=None,
        )
    )

    assert resolved_status == "completed"
    assert rows[0]["status"] == "completed"


@pytest.mark.asyncio
async def test_persist_and_resolve_final_agent_run_status_does_not_force_weaker_status_over_stopped(
    monkeypatch,
):
    rows = [{"agent_run_id": "run-force-weaker", "status": "stopped"}]
    client = _FakeAgentRunsClient(rows)

    async def _fake_update_status(
        _client,
        _run_id,
        _status,
        error=None,
        attempt_id=None,
        execution_epoch=None,
    ):
        return False

    monkeypatch.setattr(
        run_agent_background_module,
        "update_agent_run_status",
        _fake_update_status,
    )

    resolved_status = (
        await run_agent_background_module._persist_and_resolve_final_agent_run_status(
            client,
            "run-force-weaker",
            "completed",
            error_message=None,
        )
    )

    assert resolved_status == "stopped"
    assert rows[0]["status"] == "stopped"


@pytest.mark.asyncio
async def test_update_agent_run_status_preserves_stopped_against_late_completed_write():
    rows = [{"agent_run_id": "run-stop-race", "status": "running"}]
    client = _FakeAgentRunsClient(rows)

    assert await run_agent_background_module.update_agent_run_status(
        client,
        "run-stop-race",
        "stopped",
    )
    assert rows[0]["status"] == "stopped"

    assert await run_agent_background_module.update_agent_run_status(
        client,
        "run-stop-race",
        "completed",
    )
    assert rows[0]["status"] == "stopped"


@pytest.mark.asyncio
async def test_update_agent_run_status_attempt_fence_rejects_stale_terminal_write(
    monkeypatch,
):
    rows = [{"agent_run_id": "run-attempt-fence", "status": "running"}]
    client = _FakeAgentRunsClient(rows)

    async def _stale_attempt(*_args, **_kwargs):
        return False

    monkeypatch.setattr(
        run_agent_background_module,
        "_is_current_attempt_epoch",
        _stale_attempt,
        raising=False,
    )

    persisted = await run_agent_background_module.update_agent_run_status(
        client,
        "run-attempt-fence",
        "completed",
        attempt_id="attempt-1",
        execution_epoch=1,
    )

    assert persisted is False
    assert rows[0]["status"] == "running"


@pytest.mark.asyncio
async def test_update_agent_run_status_preserves_failed_against_late_completed_write():
    rows = [{"agent_run_id": "run-failed-race", "status": "running"}]
    client = _FakeAgentRunsClient(rows)

    assert await run_agent_background_module.update_agent_run_status(
        client,
        "run-failed-race",
        "failed",
        error="fatal",
    )
    assert rows[0]["status"] == "failed"

    assert await run_agent_background_module.update_agent_run_status(
        client,
        "run-failed-race",
        "completed",
    )
    assert rows[0]["status"] == "failed"


@pytest.mark.asyncio
async def test_update_agent_run_status_allows_running_to_completed():
    rows = [{"agent_run_id": "run-normal-complete", "status": "running"}]
    client = _FakeAgentRunsClient(rows)

    assert await run_agent_background_module.update_agent_run_status(
        client,
        "run-normal-complete",
        "completed",
    )
    assert rows[0]["status"] == "completed"


@pytest.mark.asyncio
async def test_update_agent_run_status_rejects_stale_attempt_epoch_write(
    monkeypatch,
):
    rows = [{"agent_run_id": "run-stale-attempt", "status": "running"}]
    client = _FakeAgentRunsClient(rows)

    async def _fake_is_current_attempt_epoch(
        _client,
        agent_run_id: str,
        *,
        attempt_id: str,
        execution_epoch: int,
    ) -> bool:
        assert agent_run_id == "run-stale-attempt"
        assert attempt_id == "attempt-1"
        assert execution_epoch == 7
        return False

    monkeypatch.setattr(
        run_agent_background_module,
        "_is_current_attempt_epoch",
        _fake_is_current_attempt_epoch,
    )

    assert (
        await run_agent_background_module.update_agent_run_status(
            client,
            "run-stale-attempt",
            "completed",
            attempt_id="attempt-1",
            execution_epoch=7,
        )
        is False
    )
    assert rows[0]["status"] == "running"


@pytest.mark.asyncio
async def test_persist_and_resolve_final_agent_run_status_does_not_force_stale_attempt_epoch_write(
    monkeypatch,
):
    rows = [{"agent_run_id": "run-stale-attempt", "status": "running"}]
    client = _FakeAgentRunsClient(rows)
    force_update_calls: list[tuple[str, str]] = []

    async def _fake_is_current_attempt_epoch(
        _client,
        _agent_run_id: str,
        *,
        attempt_id: str,
        execution_epoch: int,
    ) -> bool:
        return False

    async def _fake_force_update(
        _client,
        run_id: str,
        status: str,
        error=None,
        attempt_id=None,
        execution_epoch=None,
    ):
        force_update_calls.append((run_id, status))
        return True

    monkeypatch.setattr(
        run_agent_background_module,
        "_is_current_attempt_epoch",
        _fake_is_current_attempt_epoch,
    )
    monkeypatch.setattr(
        run_agent_background_module,
        "_force_update_agent_run_status",
        _fake_force_update,
    )

    resolved_status = (
        await run_agent_background_module._persist_and_resolve_final_agent_run_status(
            client,
            "run-stale-attempt",
            "completed",
            error_message=None,
            attempt_id="attempt-1",
            execution_epoch=7,
        )
    )

    assert resolved_status == "running"
    assert rows[0]["status"] == "running"
    assert force_update_calls == []


@pytest.mark.asyncio
async def test_update_agent_run_status_rechecks_persisted_status_between_retries(
    monkeypatch,
):
    rows = [{"agent_run_id": "run-retry-race", "status": "running"}]
    update_attempts = {"count": 0}

    class _FlakyAgentRunsTable(_FakeAgentRunsTable):
        async def update(self, payload):
            update_attempts["count"] += 1
            if update_attempts["count"] == 1:
                rows[0]["status"] = "stopped"
                raise RuntimeError("transient db error")
            return await super().update(payload)

    class _FlakyAgentRunsClient(_FakeAgentRunsClient):
        def table(self, table_name):
            assert table_name == "agent_runs"
            return _FlakyAgentRunsTable(self._rows)

    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr(run_agent_background_module.asyncio, "sleep", _no_sleep)

    client = _FlakyAgentRunsClient(rows)
    assert await run_agent_background_module.update_agent_run_status(
        client,
        "run-retry-race",
        "completed",
    )
    assert update_attempts["count"] == 1
    assert rows[0]["status"] == "stopped"


@pytest.mark.asyncio
async def test_update_agent_run_status_detects_concurrent_change_before_successful_write(
    monkeypatch,
):
    rows = [{"agent_run_id": "run-cas-race", "status": "running"}]
    update_attempts = {"count": 0}

    class _ConcurrentAgentRunsTable(_FakeAgentRunsTable):
        async def update(self, payload):
            update_attempts["count"] += 1
            if update_attempts["count"] == 1:
                rows[0]["status"] = "stopped"
            return await super().update(payload)

    class _ConcurrentAgentRunsClient(_FakeAgentRunsClient):
        def table(self, table_name):
            assert table_name == "agent_runs"
            return _ConcurrentAgentRunsTable(self._rows)

    async def _no_sleep(_seconds):
        return None

    monkeypatch.setattr(run_agent_background_module.asyncio, "sleep", _no_sleep)

    client = _ConcurrentAgentRunsClient(rows)
    assert await run_agent_background_module.update_agent_run_status(
        client,
        "run-cas-race",
        "completed",
    )
    assert update_attempts["count"] == 1
    assert rows[0]["status"] == "stopped"


@pytest.mark.asyncio
async def test_update_agent_run_status_preserves_stopped_against_late_failed_cleanup_write():
    rows = [{"agent_run_id": "run-stop-to-fail", "status": "stopped"}]
    client = _FakeAgentRunsClient(rows)

    assert await run_agent_background_module.update_agent_run_status(
        client,
        "run-stop-to-fail",
        "failed",
        error="cleanup failure",
    )
    assert rows[0]["status"] == "stopped"


def test_exception_cleanup_error_is_not_user_facing_after_stop_latches():
    assert not run_agent_background_module._should_emit_exception_error_status(
        "stopped"
    )
    assert (
        run_agent_background_module._resolve_terminal_error_message(
            "stopped",
            "cleanup failed",
        )
        is None
    )
    assert run_agent_background_module._should_emit_exception_error_status("failed")
    assert (
        run_agent_background_module._resolve_terminal_error_message(
            "failed",
            "real failure",
        )
        == "real failure"
    )


def test_control_plane_refresh_error_only_becomes_fatal_after_ttl_headroom_is_exhausted():
    assert not run_agent_background_module._control_plane_refresh_error_is_fatal(
        last_success_at=100.0,
        now_monotonic=130.0,
        ttl_seconds=90,
        refresh_interval_seconds=30.0,
    )
    assert run_agent_background_module._control_plane_refresh_error_is_fatal(
        last_success_at=100.0,
        now_monotonic=160.0,
        ttl_seconds=90,
        refresh_interval_seconds=30.0,
    )
    assert run_agent_background_module._control_plane_refresh_error_is_fatal(
        last_success_at=100.0,
        now_monotonic=101.0,
        ttl_seconds=30,
        refresh_interval_seconds=30.0,
    )


@pytest.mark.asyncio
async def test_execute_regular_run_kernel_releases_capacity_once_on_completed_terminal_status(
    monkeypatch,
):
    run_id = "run-kernel-completed"
    harness = _install_run_agent_actor_test_harness(
        monkeypatch,
        run_id=run_id,
        run_agent_impl=_simple_completed_run_agent,
    )

    final_status = await run_agent_background_module.execute_regular_run_kernel(
        client=harness.client,
        agent_run_id=run_id,
        thread_id="thread-test",
        instance_id="instance-test",
        project_id="project-test",
        model_name="openai/gpt-4.1",
        enable_thinking=False,
        reasoning_effort=None,
        stream=True,
        enable_context_manager=False,
        agent_config={},
        is_agent_builder=False,
        target_agent_id=None,
        request_id="request-test",
        resume_strategy="auto",
        resume_window_minutes=1440,
        shadow_clone_mode="off",
        shadow_clone_main_model=None,
        shadow_clone_subagent_model=None,
        owner_token="pool-owner-1",
        run_capacity_mode="off",
    )

    assert final_status == "completed"
    assert harness.acquire_calls == [(run_id, "off", "pool-owner-1", True)]
    assert harness.release_calls == [(run_id, "pool-owner-1")]
    assert harness.rows[0]["status"] == "completed"
    assert (
        _extract_statuses_from_pushed_payloads(harness.pushed_payloads)[-1]
        == "completed"
    )


@pytest.mark.asyncio
async def test_execute_regular_run_kernel_uses_attempt_id_for_capacity_lease(
    monkeypatch,
):
    run_id = "run-kernel-attempt-lease"

    async def _current_attempt_epoch(*_args, **_kwargs):
        return True

    monkeypatch.setattr(
        run_agent_background_module,
        "_is_current_attempt_epoch",
        _current_attempt_epoch,
    )
    harness = _install_run_agent_actor_test_harness(
        monkeypatch,
        run_id=run_id,
        run_agent_impl=_simple_completed_run_agent,
    )

    final_status = await run_agent_background_module.execute_regular_run_kernel(
        client=harness.client,
        agent_run_id=run_id,
        thread_id="thread-test",
        instance_id="instance-test",
        project_id="project-test",
        model_name="openai/gpt-4.1",
        enable_thinking=False,
        reasoning_effort=None,
        stream=True,
        enable_context_manager=False,
        agent_config={},
        is_agent_builder=False,
        target_agent_id=None,
        request_id="request-test",
        resume_strategy="auto",
        resume_window_minutes=1440,
        shadow_clone_mode="off",
        shadow_clone_main_model=None,
        shadow_clone_subagent_model=None,
        owner_token="pool-owner-1",
        run_capacity_mode="off",
        attempt_id="attempt-9",
        execution_epoch=3,
    )

    assert final_status == "completed"
    assert harness.acquire_calls == [("attempt-9", "off", "pool-owner-1", True)]
    assert harness.release_calls == [("attempt-9", "pool-owner-1")]
    assert harness.rows[0]["status"] == "completed"


def test_generate_trace_id_falls_back_to_stable_hex_when_client_lacks_create_trace_id(
    monkeypatch,
):
    monkeypatch.setattr(
        run_agent_background_module,
        "langfuse",
        types.SimpleNamespace(),
    )

    trace_id = run_agent_background_module.generate_trace_id(
        "033cc44c-96a5-4a63-b0fa-f71236ac2cc2"
    )

    assert len(trace_id) == 32
    assert all(c in "0123456789abcdef" for c in trace_id)


def test_start_root_span_fallback_returns_chainable_noop_trace(monkeypatch):
    monkeypatch.setattr(
        run_agent_background_module,
        "langfuse",
        types.SimpleNamespace(),
    )

    trace = run_agent_background_module.start_root_span(
        name="agent_run",
        trace_id="0" * 32,
    )

    assert trace.update(input="hello") is trace
    assert trace.update_trace(session_id="thread") is trace
    assert trace.start_observation(name="obs") is trace
    assert trace.create_event(name="done") is trace
    assert trace.end() is None


@pytest.mark.asyncio
async def test_execute_regular_run_kernel_continues_when_langfuse_start_span_fails(
    monkeypatch,
):
    run_id = "run-langfuse-fallback"
    harness = _install_run_agent_actor_test_harness(
        monkeypatch,
        run_id=run_id,
        run_agent_impl=_simple_completed_run_agent,
    )

    def _raise_start_root_span(*, name: str, trace_id: str):
        raise RuntimeError("langfuse v3 span bootstrap mismatch")

    monkeypatch.setattr(
        run_agent_background_module,
        "start_root_span",
        _raise_start_root_span,
    )

    final_status = await run_agent_background_module.execute_regular_run_kernel(
        client=harness.client,
        agent_run_id=run_id,
        thread_id="thread-test",
        instance_id="instance-test",
        project_id="project-test",
        model_name="openai/gpt-4.1",
        enable_thinking=False,
        reasoning_effort=None,
        stream=True,
        enable_context_manager=False,
        agent_config={},
        is_agent_builder=False,
        target_agent_id=None,
        request_id="request-test",
        resume_strategy="auto",
        resume_window_minutes=1440,
        shadow_clone_mode="off",
        shadow_clone_main_model=None,
        shadow_clone_subagent_model=None,
        owner_token="pool-owner-langfuse",
        run_capacity_mode="off",
    )

    assert final_status == "completed"
    assert harness.rows[0]["status"] == "completed"
    assert (
        _extract_statuses_from_pushed_payloads(harness.pushed_payloads)[-1]
        == "completed"
    )


@pytest.mark.asyncio
async def test_execute_regular_run_kernel_recovery_attempt_takes_over_stale_run_lock(
    monkeypatch,
):
    run_id = "run-kernel-recovery-takeover"

    async def _current_attempt_epoch(*_args, **_kwargs):
        return True

    monkeypatch.setattr(
        run_agent_background_module,
        "_is_current_attempt_epoch",
        _current_attempt_epoch,
    )
    harness = _install_run_agent_actor_test_harness(
        monkeypatch,
        run_id=run_id,
        run_agent_impl=_simple_completed_run_agent,
    )

    redis_set_calls = []
    takeover_calls = []

    async def _redis_set(*args, **kwargs):
        redis_set_calls.append((args, kwargs))
        return False

    async def _redis_get(*_args, **_kwargs):
        return "stale-owner"

    async def _fake_takeover(
        agent_run_id: str,
        *,
        expected_owner_token: str,
        new_owner_token: str,
    ):
        takeover_calls.append((agent_run_id, expected_owner_token, new_owner_token))
        return "taken_over"

    monkeypatch.setattr(run_agent_background_module.redis, "set", _redis_set)
    monkeypatch.setattr(run_agent_background_module.redis, "get", _redis_get)
    monkeypatch.setattr(
        run_agent_background_module,
        "_take_over_redis_run_lock",
        _fake_takeover,
    )

    final_status = await run_agent_background_module.execute_regular_run_kernel(
        client=harness.client,
        agent_run_id=run_id,
        thread_id="thread-test",
        instance_id="instance-test",
        project_id="project-test",
        model_name="openai/gpt-4.1",
        enable_thinking=False,
        reasoning_effort=None,
        stream=True,
        enable_context_manager=False,
        agent_config={},
        is_agent_builder=False,
        target_agent_id=None,
        request_id="request-test",
        resume_strategy="auto",
        resume_window_minutes=1440,
        shadow_clone_mode="off",
        shadow_clone_main_model=None,
        shadow_clone_subagent_model=None,
        owner_token="pool-owner-2",
        run_capacity_mode="off",
        attempt_id="attempt-2",
        execution_epoch=2,
    )

    assert final_status == "completed"
    assert redis_set_calls[0][0] == (
        f"agent_run_lock:{run_id}",
        "pool-owner-2",
    )
    assert redis_set_calls[0][1] == {
        "nx": True,
        "ex": run_agent_background_module.RUN_LOCK_TTL_SECONDS,
    }
    assert takeover_calls == [(run_id, "stale-owner", "pool-owner-2")]
    assert harness.rows[0]["status"] == "completed"


@pytest.mark.asyncio
async def test_execute_regular_run_kernel_retries_plain_lock_when_takeover_owner_expires(
    monkeypatch,
):
    run_id = "run-kernel-recovery-missing-owner"

    async def _current_attempt_epoch(*_args, **_kwargs):
        return True

    monkeypatch.setattr(
        run_agent_background_module,
        "_is_current_attempt_epoch",
        _current_attempt_epoch,
    )
    harness = _install_run_agent_actor_test_harness(
        monkeypatch,
        run_id=run_id,
        run_agent_impl=_simple_completed_run_agent,
    )

    redis_set_calls = []
    takeover_calls = []

    async def _redis_set(*args, **kwargs):
        redis_set_calls.append((args, kwargs))
        return len(redis_set_calls) == 2

    async def _redis_get(*_args, **_kwargs):
        return "stale-owner"

    async def _fake_takeover(
        agent_run_id: str,
        *,
        expected_owner_token: str,
        new_owner_token: str,
    ):
        takeover_calls.append((agent_run_id, expected_owner_token, new_owner_token))
        return "missing"

    monkeypatch.setattr(run_agent_background_module.redis, "set", _redis_set)
    monkeypatch.setattr(run_agent_background_module.redis, "get", _redis_get)
    monkeypatch.setattr(
        run_agent_background_module,
        "_take_over_redis_run_lock",
        _fake_takeover,
    )

    final_status = await run_agent_background_module.execute_regular_run_kernel(
        client=harness.client,
        agent_run_id=run_id,
        thread_id="thread-test",
        instance_id="instance-test",
        project_id="project-test",
        model_name="openai/gpt-4.1",
        enable_thinking=False,
        reasoning_effort=None,
        stream=True,
        enable_context_manager=False,
        agent_config={},
        is_agent_builder=False,
        target_agent_id=None,
        request_id="request-test",
        resume_strategy="auto",
        resume_window_minutes=1440,
        shadow_clone_mode="off",
        shadow_clone_main_model=None,
        shadow_clone_subagent_model=None,
        owner_token="pool-owner-3",
        run_capacity_mode="off",
        attempt_id="attempt-3",
        execution_epoch=2,
    )

    run_lock_set_calls = [
        call
        for call in redis_set_calls
        if call[0] and call[0][0] == f"agent_run_lock:{run_id}"
    ]

    assert final_status == "completed"
    assert len(run_lock_set_calls) == 2
    assert run_lock_set_calls[0][0] == (
        f"agent_run_lock:{run_id}",
        "pool-owner-3",
    )
    assert run_lock_set_calls[1][0] == (
        f"agent_run_lock:{run_id}",
        "pool-owner-3",
    )
    assert takeover_calls == [(run_id, "stale-owner", "pool-owner-3")]
    assert harness.rows[0]["status"] == "completed"


@pytest.mark.asyncio
async def test_execute_regular_run_kernel_writes_attempt_scoped_stream_and_run_scoped_mirror(
    monkeypatch,
):
    run_id = "run-kernel-public-stream"

    async def _current_attempt_epoch(*_args, **_kwargs):
        return True

    monkeypatch.setattr(
        run_agent_background_module,
        "_is_current_attempt_epoch",
        _current_attempt_epoch,
    )
    harness = _install_run_agent_actor_test_harness(
        monkeypatch,
        run_id=run_id,
        run_agent_impl=_simple_completed_run_agent,
    )

    final_status = await run_agent_background_module.execute_regular_run_kernel(
        client=harness.client,
        agent_run_id=run_id,
        thread_id="thread-test",
        instance_id="instance-test",
        project_id="project-test",
        model_name="openai/gpt-4.1",
        enable_thinking=False,
        reasoning_effort=None,
        stream=True,
        enable_context_manager=False,
        agent_config={},
        is_agent_builder=False,
        target_agent_id=None,
        request_id="request-test",
        resume_strategy="auto",
        resume_window_minutes=1440,
        shadow_clone_mode="off",
        shadow_clone_main_model=None,
        shadow_clone_subagent_model=None,
        owner_token="pool-owner-public",
        run_capacity_mode="off",
        attempt_id="attempt-public-1",
        execution_epoch=4,
    )

    assert final_status == "completed"
    assert harness.pushed_response_keys[0] == f"agent_run:{run_id}:epoch:4:responses"
    assert harness.mirrored_response_keys[0] == f"agent_run:{run_id}:responses"


@pytest.mark.asyncio
async def test_run_agent_background_does_not_release_capacity_when_admission_is_denied(
    monkeypatch,
):
    run_id = "run-capacity-denied"
    run_agent_called = False

    async def _unexpected_run_agent(*_args, **_kwargs):
        nonlocal run_agent_called
        run_agent_called = True
        if False:
            yield {}

    harness = _install_run_agent_actor_test_harness(
        monkeypatch,
        run_id=run_id,
        run_agent_impl=_unexpected_run_agent,
        acquire_result={
            "enabled": True,
            "budget": 16,
            "in_use": 16,
            "remaining": 0,
            "requested_cost": 1,
            "can_admit": False,
            "capacity_kind": "regular",
            "acquired": False,
            "refreshed": False,
            "lease_ttl_seconds": 90,
        },
    )

    await _invoke_run_agent_background_for_test(run_id)

    assert run_agent_called is False
    assert harness.acquire_calls == [(run_id, "off", "instance-test:lease-owner", True)]
    assert harness.release_calls == []
    assert harness.rows[0]["status"] == "failed"
    assert (
        _extract_statuses_from_pushed_payloads(harness.pushed_payloads)[-1] == "error"
    )


@pytest.mark.asyncio
async def test_run_agent_background_releases_capacity_once_on_completed_terminal_status(
    monkeypatch,
):
    run_id = "run-capacity-completed"

    async def _completed_run_agent(*_args, **_kwargs):
        yield {"type": "status", "status": "completed", "message": "done"}

    harness = _install_run_agent_actor_test_harness(
        monkeypatch,
        run_id=run_id,
        run_agent_impl=_completed_run_agent,
    )

    await _invoke_run_agent_background_for_test(run_id)

    assert harness.acquire_calls == [(run_id, "off", "instance-test:lease-owner", True)]
    assert harness.release_calls == [(run_id, "instance-test:lease-owner")]
    assert harness.rows[0]["status"] == "completed"
    assert (
        _extract_statuses_from_pushed_payloads(harness.pushed_payloads)[-1]
        == "completed"
    )


@pytest.mark.asyncio
async def test_run_agent_background_releases_capacity_once_on_failed_terminal_status(
    monkeypatch,
):
    run_id = "run-capacity-failed"

    async def _failed_run_agent(*_args, **_kwargs):
        yield {"type": "status", "status": "failed", "message": "boom"}

    harness = _install_run_agent_actor_test_harness(
        monkeypatch,
        run_id=run_id,
        run_agent_impl=_failed_run_agent,
    )

    await _invoke_run_agent_background_for_test(run_id)

    assert harness.acquire_calls == [(run_id, "off", "instance-test:lease-owner", True)]
    assert harness.release_calls == [(run_id, "instance-test:lease-owner")]
    assert harness.rows[0]["status"] == "failed"
    assert (
        _extract_statuses_from_pushed_payloads(harness.pushed_payloads)[-1] == "failed"
    )


@pytest.mark.asyncio
async def test_run_agent_background_releases_capacity_once_on_external_stop(
    monkeypatch,
):
    run_id = "run-capacity-stopped"

    async def _streaming_run_agent(*_args, **_kwargs):
        while True:
            await asyncio.sleep(0.05)
            yield {"type": "assistant", "content": "tick"}

    harness = _install_run_agent_actor_test_harness(
        monkeypatch,
        run_id=run_id,
        run_agent_impl=_streaming_run_agent,
        pubsub=_FakePubSub(
            messages=[{"type": "message", "data": "STOP"}],
            idle_sleep_seconds=0.001,
        ),
        force_qwen_guards=True,
        refresh_interval_seconds=9999.0,
    )

    await _invoke_run_agent_background_for_test(run_id)

    assert harness.acquire_calls == [(run_id, "off", "instance-test:lease-owner", True)]
    assert harness.release_calls == [(run_id, "instance-test:lease-owner")]
    assert harness.rows[0]["status"] == "stopped"
    assert (
        _extract_statuses_from_pushed_payloads(harness.pushed_payloads)[-1] == "stopped"
    )


@pytest.mark.asyncio
async def test_run_agent_background_tolerates_transient_capacity_refresh_error_within_ttl_headroom(
    monkeypatch,
):
    run_id = "run-capacity-refresh-error"

    async def _slow_complete_run_agent(*_args, **_kwargs):
        await asyncio.sleep(0.15)
        yield {"type": "status", "status": "completed", "message": "done"}

    harness = _install_run_agent_actor_test_harness(
        monkeypatch,
        run_id=run_id,
        run_agent_impl=_slow_complete_run_agent,
        refresh_outcomes=[
            RuntimeError("temporary redis outage"),
            {
                "enabled": True,
                "budget": 16,
                "in_use": 1,
                "remaining": 15,
                "requested_cost": 1,
                "can_admit": True,
                "capacity_kind": "regular",
                "acquired": True,
                "refreshed": True,
                "lease_ttl_seconds": 90,
            },
        ],
        force_qwen_guards=True,
        refresh_interval_seconds=0.0,
    )

    await _invoke_run_agent_background_for_test(run_id)

    assert harness.acquire_calls == [(run_id, "off", "instance-test:lease-owner", True)]
    assert len(harness.refresh_calls) >= 2
    assert {owner_token for _run_id, _mode, owner_token in harness.refresh_calls} == {
        "instance-test:lease-owner"
    }
    assert harness.release_calls == [(run_id, "instance-test:lease-owner")]
    assert harness.rows[0]["status"] == "completed"
    assert (
        _extract_statuses_from_pushed_payloads(harness.pushed_payloads)[-1]
        == "completed"
    )


@pytest.mark.asyncio
async def test_run_agent_background_fails_when_capacity_refresh_loses_owner(
    monkeypatch,
):
    run_id = "run-capacity-owner-conflict"

    async def _slow_complete_run_agent(*_args, **_kwargs):
        await asyncio.sleep(0.15)
        yield {"type": "status", "status": "completed", "message": "done"}

    harness = _install_run_agent_actor_test_harness(
        monkeypatch,
        run_id=run_id,
        run_agent_impl=_slow_complete_run_agent,
        refresh_outcomes=[
            {
                "enabled": True,
                "budget": 16,
                "in_use": 1,
                "remaining": 15,
                "requested_cost": 1,
                "can_admit": True,
                "capacity_kind": "regular",
                "acquired": False,
                "refreshed": False,
                "taken_over": False,
                "owner_conflict": True,
                "lease_ttl_seconds": 90,
            }
        ],
        force_qwen_guards=True,
        refresh_interval_seconds=0.0,
    )

    await _invoke_run_agent_background_for_test(run_id)

    assert harness.acquire_calls == [(run_id, "off", "instance-test:lease-owner", True)]
    assert len(harness.refresh_calls) >= 1
    assert harness.release_calls == [(run_id, "instance-test:lease-owner")]
    assert harness.rows[0]["status"] == "failed"
    assert (
        _extract_statuses_from_pushed_payloads(harness.pushed_payloads)[-1] == "error"
    )


@pytest.mark.asyncio
async def test_run_agent_background_keeps_stopped_terminal_truth_when_capacity_refresh_breaks_after_external_stop(
    monkeypatch,
):
    run_id = "run-stop-then-capacity-conflict"

    async def _streaming_run_agent(*_args, **_kwargs):
        while True:
            await asyncio.sleep(0.05)
            yield {"type": "assistant", "content": "tick"}

    harness = _install_run_agent_actor_test_harness(
        monkeypatch,
        run_id=run_id,
        run_agent_impl=_streaming_run_agent,
        pubsub=_FakePubSub(
            messages=[{"type": "message", "data": "STOP"}],
            idle_sleep_seconds=0.001,
        ),
        refresh_outcomes=[
            {
                "enabled": True,
                "budget": 16,
                "in_use": 1,
                "remaining": 15,
                "requested_cost": 1,
                "can_admit": True,
                "capacity_kind": "regular",
                "acquired": False,
                "refreshed": False,
                "taken_over": False,
                "owner_conflict": True,
                "lease_ttl_seconds": 90,
            }
        ],
        force_qwen_guards=True,
        refresh_interval_seconds=0.0,
    )

    await _invoke_run_agent_background_for_test(run_id)

    assert harness.acquire_calls == [(run_id, "off", "instance-test:lease-owner", True)]
    assert len(harness.refresh_calls) >= 1
    assert harness.release_calls == [(run_id, "instance-test:lease-owner")]
    assert harness.rows[0]["status"] == "stopped"
    assert (
        _extract_statuses_from_pushed_payloads(harness.pushed_payloads)[-1] == "stopped"
    )


@pytest.mark.asyncio
async def test_run_agent_background_skips_execution_when_run_is_already_stopped_before_start(
    monkeypatch,
):
    run_id = "run-prestopped"
    run_agent_calls = []

    async def _unexpected_run_agent(*_args, **_kwargs):
        run_agent_calls.append("called")
        raise AssertionError("run_agent should not execute for a pre-stopped run")
        yield  # pragma: no cover

    harness = _install_run_agent_actor_test_harness(
        monkeypatch,
        run_id=run_id,
        run_agent_impl=_unexpected_run_agent,
        force_qwen_guards=True,
    )
    harness.rows[0]["status"] = "stopped"

    await _invoke_run_agent_background_for_test(run_id)

    assert run_agent_calls == []
    assert harness.acquire_calls == [(run_id, "off", "instance-test:lease-owner", True)]
    assert harness.refresh_calls == []
    assert harness.release_calls == [(run_id, "instance-test:lease-owner")]
    assert harness.rows[0]["status"] == "stopped"
    assert harness.pushed_payloads == []


def test_maybe_enable_qwen_runtime_guards_uses_resolved_context(monkeypatch):
    monkeypatch.setattr(
        run_agent_background_module,
        "get_agent_model_context",
        lambda: "qwen3.5-plus",
    )

    assert run_agent_background_module._maybe_enable_qwen_runtime_guards(
        False,
        agent_run_id="run-ctx",
    )


def test_maybe_enable_qwen_runtime_guards_keeps_false_when_context_non_qwen(
    monkeypatch,
):
    monkeypatch.setattr(
        run_agent_background_module,
        "get_agent_model_context",
        lambda: "openrouter/z-ai/glm-4.7",
    )

    assert not run_agent_background_module._maybe_enable_qwen_runtime_guards(
        False,
        agent_run_id="run-ctx",
    )


def test_maybe_enable_qwen_runtime_guards_enables_for_openrouter_qwen_36_plus(
    monkeypatch,
):
    monkeypatch.setattr(
        run_agent_background_module,
        "get_agent_model_context",
        lambda: "openrouter/qwen/qwen3.6-plus",
    )

    assert run_agent_background_module._maybe_enable_qwen_runtime_guards(
        False,
        agent_run_id="run-ctx",
    )


def test_maybe_enable_qwen_runtime_guards_stays_disabled_for_glm_5_1_context(
    monkeypatch,
):
    monkeypatch.setattr(
        run_agent_background_module,
        "get_agent_model_context",
        lambda: "openrouter/z-ai/glm-5.1",
    )

    assert not run_agent_background_module._maybe_enable_qwen_runtime_guards(
        False,
        agent_run_id="run-ctx",
    )


def test_extract_write_file_tool_call_chunk():
    raw_arguments = '{"file_path":"/workspace/demo.py","content":"print(1)"}'
    payload = _build_write_chunk_response(raw_arguments)

    extracted = run_agent_background_module._extract_write_file_tool_call_chunk(payload)

    assert extracted is not None
    assert extracted["tool_call_id"] == "call-1"
    assert extracted["arguments_length"] == len(raw_arguments)
    assert extracted["arguments_source"] == "parsed_input"
    assert extracted["path"] == "/workspace/demo.py"
    assert extracted["content"] == "print(1)"


def test_extract_write_file_tool_call_chunk_reads_trace_source() -> None:
    raw_arguments = '{"path":"/workspace/demo.py","content":"print(1)"}'
    payload = _build_write_chunk_response(
        raw_arguments,
        args_source="raw_preview",
    )

    extracted = run_agent_background_module._extract_write_file_tool_call_chunk(payload)

    assert extracted is not None
    assert extracted["arguments_source"] == "raw_preview"


def test_extract_write_file_tool_call_chunk_drops_unknown_keys():
    raw_arguments = json.dumps(
        {
            "path": "/workspace/demo.py",
            "content": '<a href="https://example.com">demo</a>',
            'href="https': "",
            "unexpected_field": "ignore-me",
            "append": True,
        },
        ensure_ascii=False,
    )
    payload = _build_write_chunk_response(raw_arguments)

    extracted = run_agent_background_module._extract_write_file_tool_call_chunk(payload)

    assert extracted is not None
    assert extracted["path"] == "/workspace/demo.py"
    assert extracted["content"] == '<a href="https://example.com">demo</a>'
    assert extracted["content_length_bytes"] == len(
        '<a href="https://example.com">demo</a>'.encode("utf-8")
    )


def test_extract_write_file_tool_call_chunk_drops_implausible_path_fragment():
    payload = _build_write_chunk_response('{"path":"constitutional","content":"hello"}')

    extracted = run_agent_background_module._extract_write_file_tool_call_chunk(payload)

    assert extracted is not None
    assert extracted["path"] is None
    assert extracted["path_hint"] == "constitutional"
    assert extracted["content"] == "hello"


def test_extract_write_file_tool_call_chunk_reads_non_first_tool_call():
    payload = {
        "type": "assistant",
        "metadata": json.dumps(
            {
                "stream_status": "tool_call_chunk",
                "tool_calls": [
                    {
                        "id": "call-read",
                        "index": 0,
                        "type": "function",
                        "function": {
                            "name": "read_file",
                            "arguments": '{"path":"/workspace/demo.py"}',
                        },
                    },
                    {
                        "id": "call-write",
                        "index": 1,
                        "type": "function",
                        "function": {
                            "name": "write_file",
                            "arguments": '{"path":"/workspace/demo.py","content":"print(1)"}',
                        },
                    },
                ],
            },
            ensure_ascii=False,
        ),
    }

    extracted = run_agent_background_module._extract_write_file_tool_call_chunk(payload)

    assert extracted is not None
    assert extracted["tool_call_id"] == "call-write"
    assert extracted["path"] == "/workspace/demo.py"
    assert extracted["content"] == "print(1)"


def test_extract_write_file_tool_call_chunk_prefers_richer_write_payload():
    payload = {
        "type": "assistant",
        "metadata": json.dumps(
            {
                "stream_status": "tool_call_chunk",
                "tool_calls": [
                    {
                        "id": "call-write-1",
                        "index": 0,
                        "type": "function",
                        "function": {
                            "name": "write_file",
                            "arguments": '{"path":"/workspace/demo.py"}',
                        },
                    },
                    {
                        "id": "call-write-2",
                        "index": 1,
                        "type": "function",
                        "function": {
                            "name": "write_file",
                            "arguments": '{"path":"/workspace/demo.py","content":"print(1)"}',
                        },
                    },
                ],
            },
            ensure_ascii=False,
        ),
    }

    extracted = run_agent_background_module._extract_write_file_tool_call_chunk(payload)

    assert extracted is not None
    assert extracted["tool_call_id"] == "call-write-2"
    assert extracted["content"] == "print(1)"
    assert extracted["content_length_bytes"] > 0


def test_check_qwen_write_arg_guard_chunk_limit_is_warning_only(monkeypatch):
    monkeypatch.setattr(run_agent_background_module, "QWEN_WRITE_ARG_MAX_CHUNKS", 1)
    monkeypatch.setattr(
        run_agent_background_module, "QWEN_WRITE_ARG_WARN_CHUNK_INTERVAL", 1
    )
    monkeypatch.setattr(
        run_agent_background_module, "QWEN_WRITE_ARG_MAX_SECONDS", 999.0
    )
    monkeypatch.setattr(
        run_agent_background_module, "QWEN_WRITE_ARG_STALL_SECONDS", 999.0
    )
    monkeypatch.setattr(
        run_agent_background_module, "QWEN_WRITE_NO_GROWTH_MAX_CHUNKS", 999
    )

    state = {}
    first = _build_write_chunk_response('{"content":"a"}')
    second = _build_write_chunk_response('{"content":"ab"}')

    assert (
        run_agent_background_module._check_qwen_write_arg_guard(
            first,
            state,
            now_monotonic=10.0,
        )
        is None
    )

    assert (
        run_agent_background_module._check_qwen_write_arg_guard(
            second,
            state,
            now_monotonic=10.1,
        )
        is None
    )
    assert state["chunk_limit_warning_logged"] is True


def test_check_qwen_write_arg_guard_triggers_stall_after_no_growth(monkeypatch):
    monkeypatch.setattr(
        run_agent_background_module, "QWEN_WRITE_ARG_MAX_SECONDS", 999.0
    )
    monkeypatch.setattr(
        run_agent_background_module, "QWEN_WRITE_ARG_STALL_SECONDS", 1.0
    )
    monkeypatch.setattr(
        run_agent_background_module,
        "QWEN_WRITE_ARG_STALL_WITH_CONTENT_SECONDS",
        1.0,
    )
    monkeypatch.setattr(
        run_agent_background_module, "QWEN_WRITE_NO_GROWTH_MAX_CHUNKS", 2
    )

    state = {}
    first = _build_write_chunk_response('{"content":"hello"}')
    second = _build_write_chunk_response('{"content":"hello"}')
    third = _build_write_chunk_response('{"content":"hello"}')

    assert (
        run_agent_background_module._check_qwen_write_arg_guard(
            first,
            state,
            now_monotonic=10.0,
        )
        is None
    )
    assert (
        run_agent_background_module._check_qwen_write_arg_guard(
            second,
            state,
            now_monotonic=11.2,
        )
        is None
    )

    guard_error = run_agent_background_module._check_qwen_write_arg_guard(
        third,
        state,
        now_monotonic=11.3,
    )
    assert guard_error is not None
    assert "stalled without effective progress" in guard_error


def test_check_qwen_write_arg_guard_classifies_unreconstructable_short_args(
    monkeypatch,
):
    monkeypatch.setattr(
        run_agent_background_module, "QWEN_WRITE_ARG_MAX_SECONDS", 999.0
    )
    monkeypatch.setattr(
        run_agent_background_module, "QWEN_WRITE_ARG_STALL_SECONDS", 0.5
    )
    monkeypatch.setattr(
        run_agent_background_module, "QWEN_WRITE_NO_GROWTH_MAX_CHUNKS", 2
    )
    monkeypatch.setattr(
        run_agent_background_module, "QWEN_WRITE_SHORT_ARGS_MAX_BYTES", 32
    )
    monkeypatch.setattr(
        run_agent_background_module, "QWEN_WRITE_SHORT_ARGS_STREAK_THRESHOLD", 2
    )
    monkeypatch.setattr(
        run_agent_background_module, "QWEN_WRITE_SHORT_ARGS_MIN_SECONDS", 0.1
    )

    state = {}
    first = _build_write_chunk_response('{"x":1}')
    second = _build_write_chunk_response('{"x":1}')
    third = _build_write_chunk_response('{"x":1}')

    assert (
        run_agent_background_module._check_qwen_write_arg_guard(
            first,
            state,
            now_monotonic=10.0,
        )
        is None
    )
    guard_error = run_agent_background_module._check_qwen_write_arg_guard(
        second,
        state,
        now_monotonic=10.8,
    )
    assert guard_error is not None
    assert "unreconstructable" in guard_error


def test_check_qwen_write_arg_guard_tracks_path_only_streak(monkeypatch):
    monkeypatch.setattr(
        run_agent_background_module, "QWEN_WRITE_ARG_MAX_SECONDS", 999.0
    )
    monkeypatch.setattr(
        run_agent_background_module, "QWEN_WRITE_ARG_STALL_SECONDS", 999.0
    )
    monkeypatch.setattr(
        run_agent_background_module, "QWEN_WRITE_NO_GROWTH_MAX_CHUNKS", 999
    )

    state = {}
    chunk = _build_write_chunk_response(
        '{"path":"constitutional-convention/index.html"}'
    )
    assert (
        run_agent_background_module._check_qwen_write_arg_guard(
            chunk,
            state,
            now_monotonic=10.0,
        )
        is None
    )
    assert state["path_only_streak"] == 1

    assert (
        run_agent_background_module._check_qwen_write_arg_guard(
            chunk,
            state,
            now_monotonic=10.2,
        )
        is None
    )
    assert state["path_only_streak"] == 2
    assert state["content_length_bytes"] == 0


def test_check_qwen_write_arg_guard_treats_large_argument_growth_as_progress(
    monkeypatch,
):
    monkeypatch.setattr(
        run_agent_background_module, "QWEN_WRITE_ARG_MAX_SECONDS", 999.0
    )
    monkeypatch.setattr(
        run_agent_background_module, "QWEN_WRITE_ARG_STALL_SECONDS", 0.5
    )
    monkeypatch.setattr(
        run_agent_background_module, "QWEN_WRITE_NO_GROWTH_MAX_CHUNKS", 2
    )
    monkeypatch.setattr(
        run_agent_background_module,
        "QWEN_WRITE_ARG_ONLY_GROWTH_MIN_BYTES",
        4,
    )

    state = {}
    first = _build_write_chunk_response('{"path":"us"}')
    second = _build_write_chunk_response('{"path":"us","content":"')

    assert (
        run_agent_background_module._check_qwen_write_arg_guard(
            first,
            state,
            now_monotonic=10.0,
        )
        is None
    )
    assert state["no_growth_streak"] == 0

    assert (
        run_agent_background_module._check_qwen_write_arg_guard(
            second,
            state,
            now_monotonic=10.2,
        )
        is None
    )
    assert state["no_growth_streak"] == 0
    assert state["path_hint"] == "us"


def test_should_attempt_qwen_write_path_only_retry(monkeypatch):
    monkeypatch.setattr(
        run_agent_background_module,
        "QWEN_WRITE_PATH_ONLY_STREAK_THRESHOLD",
        2,
    )
    monkeypatch.setattr(
        run_agent_background_module,
        "QWEN_WRITE_PATH_ONLY_MIN_SECONDS",
        1.0,
    )
    monkeypatch.setattr(
        run_agent_background_module,
        "QWEN_WRITE_AUTO_REPAIR_MAX_RETRIES",
        1,
    )

    state = {
        "path": "constitutional-convention/index.html",
        "content_length_bytes": 0,
        "path_only_streak": 2,
        "phase_started_at": 10.0,
        "tool_call_id": "call-1",
    }
    assert run_agent_background_module._should_attempt_qwen_write_path_only_retry(
        state,
        now_monotonic=11.1,
        attempts_made=0,
    )
    assert not run_agent_background_module._should_attempt_qwen_write_path_only_retry(
        state,
        now_monotonic=11.1,
        attempts_made=1,
    )


def test_should_attempt_qwen_write_path_only_retry_for_contentless_path_hint(
    monkeypatch,
):
    monkeypatch.setattr(
        run_agent_background_module,
        "QWEN_WRITE_PATH_ONLY_STREAK_THRESHOLD",
        2,
    )
    monkeypatch.setattr(
        run_agent_background_module,
        "QWEN_WRITE_PATH_ONLY_MIN_SECONDS",
        1.0,
    )
    monkeypatch.setattr(
        run_agent_background_module,
        "QWEN_WRITE_AUTO_REPAIR_MAX_RETRIES",
        1,
    )

    state = {
        "path": None,
        "path_hint": "us",
        "content_length_bytes": 0,
        "path_only_streak": 2,
        "phase_started_at": 10.0,
        "tool_call_id": "call-2",
    }
    assert not run_agent_background_module._should_attempt_qwen_write_path_only_retry(
        state,
        now_monotonic=11.1,
        attempts_made=0,
    )
    assert (
        run_agent_background_module._resolve_qwen_write_retry_path_hint(state) is None
    )


def test_is_qwen_write_short_args_loop() -> None:
    state = {
        "tool_call_id": "call-1",
        "phase_started_at": 10.0,
        "path": None,
        "path_hint": None,
        "content_length_bytes": 0,
        "last_arguments_length": 15,
        "short_args_streak": 45,
        "same_arguments_hash_streak": 30,
    }
    assert run_agent_background_module._is_qwen_write_short_args_loop(
        state,
        now_monotonic=13.0,
    )


def test_build_qwen_write_path_only_retry_instruction_contains_constraints():
    prompt = run_agent_background_module._build_qwen_write_path_only_retry_instruction(
        path="constitutional-convention/index.html",
        attempt=1,
    )
    assert "constitutional-convention/index.html" in prompt
    assert "write_file exactly once" in prompt
    assert '"content"' in prompt


def test_build_qwen_write_path_only_retry_instruction_with_path_hint():
    prompt = run_agent_background_module._build_qwen_write_path_only_retry_instruction(
        path=None,
        path_hint="us",
        attempt=1,
    )
    assert "Path hint from previous failed call" in prompt
    assert "do not send partial fragments" in prompt
    assert "valid JSON object" in prompt


def test_check_qwen_write_idle_guard_triggers_after_timeout(monkeypatch):
    monkeypatch.setattr(
        run_agent_background_module, "QWEN_WRITE_IDLE_TIMEOUT_SECONDS", 2.0
    )
    monkeypatch.setattr(
        run_agent_background_module,
        "QWEN_WRITE_IDLE_TIMEOUT_WITH_CONTENT_SECONDS",
        2.0,
    )

    state = {}
    chunk = _build_write_chunk_response('{"content":"hello"}')
    assert (
        run_agent_background_module._check_qwen_write_arg_guard(
            chunk,
            state,
            now_monotonic=10.0,
        )
        is None
    )

    idle_reason = run_agent_background_module._check_qwen_write_idle_guard(
        state,
        now_monotonic=12.2,
        last_response_monotonic=10.0,
    )
    assert idle_reason is not None
    assert "no new chunks" in idle_reason


def test_check_qwen_write_idle_guard_uses_content_timeout(monkeypatch):
    monkeypatch.setattr(
        run_agent_background_module, "QWEN_WRITE_IDLE_TIMEOUT_SECONDS", 1.0
    )
    monkeypatch.setattr(
        run_agent_background_module,
        "QWEN_WRITE_IDLE_TIMEOUT_WITH_CONTENT_SECONDS",
        4.0,
    )

    state = {}
    chunk = _build_write_chunk_response('{"content":"hello"}')
    assert (
        run_agent_background_module._check_qwen_write_arg_guard(
            chunk,
            state,
            now_monotonic=10.0,
        )
        is None
    )
    assert (
        run_agent_background_module._check_qwen_write_idle_guard(
            state,
            now_monotonic=12.0,
            last_response_monotonic=10.0,
        )
        is None
    )

    idle_reason = run_agent_background_module._check_qwen_write_idle_guard(
        state,
        now_monotonic=14.2,
        last_response_monotonic=10.0,
    )
    assert idle_reason is not None
    assert "effective_timeout=4.0s" in idle_reason
    assert "content_bytes=" in idle_reason


def test_check_qwen_write_arg_guard_uses_content_stall_timeout(monkeypatch):
    monkeypatch.setattr(
        run_agent_background_module, "QWEN_WRITE_ARG_MAX_SECONDS", 999.0
    )
    monkeypatch.setattr(
        run_agent_background_module, "QWEN_WRITE_ARG_STALL_SECONDS", 0.5
    )
    monkeypatch.setattr(
        run_agent_background_module,
        "QWEN_WRITE_ARG_STALL_WITH_CONTENT_SECONDS",
        2.0,
    )
    monkeypatch.setattr(
        run_agent_background_module, "QWEN_WRITE_NO_GROWTH_MAX_CHUNKS", 2
    )

    state = {}
    first = _build_write_chunk_response('{"content":"hello"}')
    second = _build_write_chunk_response('{"content":"hello"}')
    third = _build_write_chunk_response('{"content":"hello"}')

    assert (
        run_agent_background_module._check_qwen_write_arg_guard(
            first,
            state,
            now_monotonic=10.0,
        )
        is None
    )
    assert (
        run_agent_background_module._check_qwen_write_arg_guard(
            second,
            state,
            now_monotonic=10.8,
        )
        is None
    )

    guard_error = run_agent_background_module._check_qwen_write_arg_guard(
        third,
        state,
        now_monotonic=12.2,
    )
    assert guard_error is not None
    assert "effective_stall_timeout=2.0s" in guard_error


@pytest.mark.asyncio
async def test_force_qwen_write_completion_uses_sandbox_code_tool(monkeypatch):
    monkeypatch.setattr(
        run_agent_background_module, "QWEN_WRITE_FORCE_COMPLETION_ENABLED", True
    )
    monkeypatch.setattr(
        run_agent_background_module, "QWEN_WRITE_FORCE_MIN_CONTENT_BYTES", 4
    )

    captured = {}

    class _FakeDBConnection:
        @property
        async def client(self):
            return "db-client"

    class _FakeThreadManagerAdapter:
        def __init__(self, *, db_client=None):
            captured["db_client"] = db_client
            self._db_client = db_client

    class _FakeTool:
        def __init__(
            self,
            project_id: str,
            thread_manager=None,
            *,
            shadow_clone_run_id=None,
            strict_sandbox=False,
        ):
            captured["project_id"] = project_id
            captured["thread_manager"] = thread_manager
            captured["shadow_clone_run_id"] = shadow_clone_run_id
            captured["strict_sandbox"] = strict_sandbox

        async def write_file(self, path: str, content: str):
            captured["path"] = path
            captured["content"] = content
            return types.SimpleNamespace(success=True, output={"path": path})

    import agent.tools.sandbox_code_tool as sandbox_code_tool_module
    import agentscope_integration.adapters.thread_manager_adapter as thread_manager_module

    monkeypatch.setattr(run_agent_background_module, "db", _FakeDBConnection())
    monkeypatch.setattr(
        thread_manager_module, "ThreadManagerAdapter", _FakeThreadManagerAdapter
    )
    monkeypatch.setattr(sandbox_code_tool_module, "SandboxCodeTool", _FakeTool)

    state = {
        "path": "/workspace/demo.py",
        "content": "print('ok')",
        "content_length_bytes": len("print('ok')".encode("utf-8")),
        "completion_forced": False,
    }
    ok, error = await run_agent_background_module._force_qwen_write_completion(
        project_id="project-123",
        state=state,
        agent_run_id="run-1",
        trigger_reason="test",
        shadow_clone_run_id="run-1",
        strict_sandbox=True,
    )

    assert ok
    assert error is None
    assert state["completion_forced"] is True
    assert captured["project_id"] == "project-123"
    assert isinstance(captured["thread_manager"], _FakeThreadManagerAdapter)
    assert captured["db_client"] == "db-client"
    assert captured["shadow_clone_run_id"] == "run-1"
    assert captured["strict_sandbox"] is True
    assert captured["path"] == "/workspace/demo.py"


@pytest.mark.asyncio
async def test_force_qwen_write_completion_reports_tool_initialization_failure(
    monkeypatch,
):
    monkeypatch.setattr(
        run_agent_background_module, "QWEN_WRITE_FORCE_COMPLETION_ENABLED", True
    )
    monkeypatch.setattr(
        run_agent_background_module, "QWEN_WRITE_FORCE_MIN_CONTENT_BYTES", 4
    )

    class _FakeDBConnection:
        @property
        async def client(self):
            return "db-client"

    class _FakeThreadManagerAdapter:
        def __init__(self, *, db_client=None):
            self._db_client = db_client

    class _FakeTool:
        def __init__(
            self,
            project_id: str,
            thread_manager=None,
            *,
            shadow_clone_run_id=None,
            strict_sandbox=False,
        ):
            raise RuntimeError("Sandbox tools require a thread manager")

    import agent.tools.sandbox_code_tool as sandbox_code_tool_module
    import agentscope_integration.adapters.thread_manager_adapter as thread_manager_module

    monkeypatch.setattr(run_agent_background_module, "db", _FakeDBConnection())
    monkeypatch.setattr(
        thread_manager_module, "ThreadManagerAdapter", _FakeThreadManagerAdapter
    )
    monkeypatch.setattr(sandbox_code_tool_module, "SandboxCodeTool", _FakeTool)

    state = {
        "path": "/workspace/demo.py",
        "content": "print('ok')",
        "content_length_bytes": len("print('ok')".encode("utf-8")),
        "completion_forced": False,
    }
    ok, error = await run_agent_background_module._force_qwen_write_completion(
        project_id="project-123",
        state=state,
        agent_run_id="run-1",
        trigger_reason="test",
    )

    assert not ok
    assert (
        error == "forced completion call failed: Sandbox tools require a thread manager"
    )


@pytest.mark.asyncio
async def test_force_qwen_write_completion_rejects_implausible_path(monkeypatch):
    monkeypatch.setattr(
        run_agent_background_module, "QWEN_WRITE_FORCE_COMPLETION_ENABLED", True
    )
    monkeypatch.setattr(
        run_agent_background_module, "QWEN_WRITE_FORCE_MIN_CONTENT_BYTES", 4
    )

    state = {
        "path": "constitutional",
        "content": "print('ok')",
        "content_length_bytes": len("print('ok')".encode("utf-8")),
        "completion_forced": False,
    }
    ok, error = await run_agent_background_module._force_qwen_write_completion(
        project_id="project-123",
        state=state,
        agent_run_id="run-1",
        trigger_reason="test",
    )

    assert not ok
    assert error == "write payload not ready for forced completion"


@pytest.mark.asyncio
async def test_force_qwen_write_completion_defaults_to_non_strict_context(monkeypatch):
    monkeypatch.setattr(
        run_agent_background_module, "QWEN_WRITE_FORCE_COMPLETION_ENABLED", True
    )
    monkeypatch.setattr(
        run_agent_background_module, "QWEN_WRITE_FORCE_MIN_CONTENT_BYTES", 4
    )

    captured = {}

    class _FakeDBConnection:
        @property
        async def client(self):
            return "db-client"

    class _FakeThreadManagerAdapter:
        def __init__(self, *, db_client=None):
            self._db_client = db_client

    class _FakeTool:
        def __init__(
            self,
            project_id: str,
            thread_manager=None,
            *,
            shadow_clone_run_id=None,
            strict_sandbox=False,
        ):
            captured["shadow_clone_run_id"] = shadow_clone_run_id
            captured["strict_sandbox"] = strict_sandbox

        async def write_file(self, path: str, content: str):
            return types.SimpleNamespace(success=True, output={"path": path})

    import agent.tools.sandbox_code_tool as sandbox_code_tool_module
    import agentscope_integration.adapters.thread_manager_adapter as thread_manager_module

    monkeypatch.setattr(run_agent_background_module, "db", _FakeDBConnection())
    monkeypatch.setattr(
        thread_manager_module, "ThreadManagerAdapter", _FakeThreadManagerAdapter
    )
    monkeypatch.setattr(sandbox_code_tool_module, "SandboxCodeTool", _FakeTool)

    state = {
        "path": "/workspace/demo.py",
        "content": "print('ok')",
        "content_length_bytes": len("print('ok')".encode("utf-8")),
        "completion_forced": False,
    }
    ok, error = await run_agent_background_module._force_qwen_write_completion(
        project_id="project-123",
        state=state,
        agent_run_id="run-1",
        trigger_reason="test",
    )

    assert ok
    assert error is None
    assert captured["shadow_clone_run_id"] is None
    assert captured["strict_sandbox"] is False


def test_check_qwen_write_idle_guard_clears_after_write_terminal_status(monkeypatch):
    monkeypatch.setattr(
        run_agent_background_module, "QWEN_WRITE_IDLE_TIMEOUT_SECONDS", 1.0
    )
    monkeypatch.setattr(
        run_agent_background_module,
        "QWEN_WRITE_IDLE_TIMEOUT_WITH_CONTENT_SECONDS",
        1.0,
    )

    state = {}
    chunk = _build_write_chunk_response('{"content":"hello"}')
    assert (
        run_agent_background_module._check_qwen_write_arg_guard(
            chunk,
            state,
            now_monotonic=5.0,
        )
        is None
    )
    assert (
        run_agent_background_module._check_qwen_write_idle_guard(
            state,
            now_monotonic=6.2,
            last_response_monotonic=5.0,
        )
        is not None
    )

    completed = _build_write_status_response("tool_completed")
    assert (
        run_agent_background_module._check_qwen_write_arg_guard(
            completed,
            state,
            now_monotonic=6.3,
        )
        is None
    )
    assert (
        run_agent_background_module._check_qwen_write_idle_guard(
            state,
            now_monotonic=8.5,
            last_response_monotonic=5.0,
        )
        is None
    )


@pytest.mark.asyncio
async def test_close_agent_generator_respects_timeout(monkeypatch):
    monkeypatch.setattr(
        run_agent_background_module,
        "QWEN_GENERATOR_CLOSE_TIMEOUT_SECONDS",
        0.01,
    )

    class _SlowGenerator:
        async def aclose(self):
            await asyncio.sleep(0.2)

    await run_agent_background_module._close_agent_generator(
        _SlowGenerator(),
        agent_run_id="run-timeout",
    )


def test_should_emit_qwen_write_chunk_uses_growth_and_interval_thresholds(monkeypatch):
    monkeypatch.setattr(
        run_agent_background_module, "QWEN_WRITE_CHUNK_MIN_GROWTH_BYTES", 10
    )
    monkeypatch.setattr(
        run_agent_background_module, "QWEN_WRITE_CHUNK_MIN_EMIT_INTERVAL_MS", 200
    )

    emit_state = {}
    first = _build_write_chunk_response('{"content":"abcdef"}')
    second = _build_write_chunk_response('{"content":"abcdefg"}')
    third = _build_write_chunk_response('{"content":"abcdefghijklmnop"}')

    assert run_agent_background_module._should_emit_qwen_write_chunk(
        first,
        emit_state,
        now_monotonic=20.0,
    )
    assert not run_agent_background_module._should_emit_qwen_write_chunk(
        second,
        emit_state,
        now_monotonic=20.05,
    )
    assert run_agent_background_module._should_emit_qwen_write_chunk(
        third,
        emit_state,
        now_monotonic=20.1,
    )


@pytest.mark.asyncio
async def test_append_missing_write_file_terminal_status_adds_compensation(monkeypatch):
    started_status = {
        "type": "status",
        "content": json.dumps(
            {
                "function_name": "write_file",
                "status_type": "tool_started",
                "arguments": json.dumps({"path": "/workspace/demo.py"}),
            },
            ensure_ascii=False,
        ),
    }

    async def _fake_lrange(_key: str, _start: int, _end: int):
        return [json.dumps(started_status, ensure_ascii=False)]

    captured_payloads = []

    async def _capture_push(
        _list_key: str, _channel: str, payload_json: str, *, agent_run_id: str
    ):
        captured_payloads.append((agent_run_id, json.loads(payload_json)))

    monkeypatch.setattr(run_agent_background_module.redis, "lrange", _fake_lrange)
    monkeypatch.setattr(
        run_agent_background_module,
        "_push_response_with_retry",
        _capture_push,
    )

    payload_json = (
        await run_agent_background_module._append_missing_write_file_terminal_status(
            response_list_key="agent_run:run-42:responses",
            response_channel="agent_run:run-42:new_response",
            agent_run_id="run-42",
            thread_id="thread-9",
            final_status="failed",
            enabled=True,
        )
    )

    assert len(captured_payloads) == 1
    _, payload = captured_payloads[0]
    assert payload_json is not None
    assert json.loads(payload_json) == payload
    content_payload = json.loads(payload["content"])
    assert content_payload["status_type"] == "tool_failed"
    assert json.loads(content_payload["arguments"])["path"] == "/workspace/demo.py"


@pytest.mark.asyncio
async def test_append_missing_write_file_terminal_status_noop_when_disabled(
    monkeypatch,
):
    async def _fake_lrange(_key: str, _start: int, _end: int):
        raise AssertionError(
            "lrange should not be called when compensation is disabled"
        )

    monkeypatch.setattr(run_agent_background_module.redis, "lrange", _fake_lrange)

    await run_agent_background_module._append_missing_write_file_terminal_status(
        response_list_key="agent_run:run-42:responses",
        response_channel="agent_run:run-42:new_response",
        agent_run_id="run-42",
        thread_id="thread-9",
        final_status="failed",
        enabled=False,
    )


@pytest.mark.asyncio
async def test_ensure_terminal_status_message_returns_payload_for_fallback(monkeypatch):
    async def _fake_lrange(_key: str, _start: int, _end: int):
        return []

    captured_payloads = []

    async def _capture_push(
        _list_key: str, _channel: str, payload_json: str, *, agent_run_id: str
    ):
        captured_payloads.append((agent_run_id, json.loads(payload_json)))
        return True

    monkeypatch.setattr(run_agent_background_module.redis, "lrange", _fake_lrange)
    monkeypatch.setattr(
        run_agent_background_module,
        "_push_response_with_retry",
        _capture_push,
    )

    payload_json = await run_agent_background_module._ensure_terminal_status_message(
        "agent_run:run-42:responses",
        "agent_run:run-42:new_response",
        "stopped",
        "Stopped by user",
    )

    assert payload_json is not None
    assert len(captured_payloads) == 1
    agent_run_id, payload = captured_payloads[0]
    assert agent_run_id == "run-42"
    assert json.loads(payload_json) == payload
    assert payload["type"] == "status"
    assert payload["status"] == "stopped"
    assert payload["message"] == "Stopped by user"


@pytest.mark.asyncio
async def test_ensure_terminal_status_message_noop_when_terminal_status_already_present(
    monkeypatch,
):
    existing_terminal = json.dumps(
        {"type": "status", "status": "completed", "message": "done"},
        ensure_ascii=False,
    )

    async def _fake_lrange(_key: str, _start: int, _end: int):
        return [existing_terminal]

    async def _unexpected_push(*_args, **_kwargs):
        raise AssertionError(
            "terminal status should not be pushed when one already exists"
        )

    monkeypatch.setattr(run_agent_background_module.redis, "lrange", _fake_lrange)
    monkeypatch.setattr(
        run_agent_background_module,
        "_push_response_with_retry",
        _unexpected_push,
    )

    payload_json = await run_agent_background_module._ensure_terminal_status_message(
        "agent_run:run-42:responses",
        "agent_run:run-42:new_response",
        "completed",
    )

    assert payload_json is None


@pytest.mark.asyncio
async def test_ensure_terminal_status_message_appends_corrective_terminal_when_existing_status_differs(
    monkeypatch,
):
    existing_terminal = json.dumps(
        {"type": "status", "status": "completed", "message": "done"},
        ensure_ascii=False,
    )

    async def _fake_lrange(_key: str, _start: int, _end: int):
        return [existing_terminal]

    captured_payloads = []

    async def _capture_push(
        _list_key: str, _channel: str, payload_json: str, *, agent_run_id: str
    ):
        captured_payloads.append((agent_run_id, json.loads(payload_json)))
        return True

    monkeypatch.setattr(run_agent_background_module.redis, "lrange", _fake_lrange)
    monkeypatch.setattr(
        run_agent_background_module,
        "_push_response_with_retry",
        _capture_push,
    )

    payload_json = await run_agent_background_module._ensure_terminal_status_message(
        "agent_run:run-42:responses",
        "agent_run:run-42:new_response",
        "stopped",
        "Stopped by user",
    )

    assert payload_json is not None
    assert len(captured_payloads) == 1
    agent_run_id, payload = captured_payloads[0]
    assert agent_run_id == "run-42"
    assert json.loads(payload_json) == payload
    assert payload["type"] == "status"
    assert payload["status"] == "stopped"
    assert payload["message"] == "Stopped by user"
