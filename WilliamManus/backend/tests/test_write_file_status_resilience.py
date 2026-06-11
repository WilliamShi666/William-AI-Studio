import json
import importlib.util
import sys
import types
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))
MODULE_PATH = BACKEND_ROOT / "agent" / "tools" / "sandbox_code_tool.py"


def _build_structlog_stub():
    class _DummyStructlogLogger:
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
        def wrap_for_formatter(*_args, **_kwargs):
            return None

    class _DummyLoggerFactory:
        def __call__(self, *_args, **_kwargs):
            return _DummyStructlogLogger()

    return types.SimpleNamespace(
        configure=lambda **_kwargs: None,
        get_logger=lambda *_args, **_kwargs: _DummyStructlogLogger(),
        stdlib=types.SimpleNamespace(
            add_log_level=lambda *_args, **_kwargs: None,
            PositionalArgumentsFormatter=lambda *_args, **_kwargs: None,
            ProcessorFormatter=_DummyProcessorFormatter,
            LoggerFactory=lambda *_args, **_kwargs: _DummyLoggerFactory(),
            BoundLogger=_DummyStructlogLogger,
        ),
        processors=types.SimpleNamespace(
            TimeStamper=lambda **_kwargs: None,
        ),
    )


def _build_agentpress_tool_stub():
    class _ToolResult:
        def __init__(self, success: bool, output: dict):
            self.success = success
            self.output = output

    tool_mod = types.ModuleType("agentpress.tool")
    tool_mod.ToolResult = _ToolResult
    return tool_mod


def _build_adk_thread_manager_stub():
    manager_mod = types.ModuleType("agentpress.adk_thread_manager")
    manager_mod.ADKThreadManager = type("ADKThreadManager", (), {})
    return manager_mod


def _build_tool_base_stub():
    tool_base_mod = types.ModuleType("sandbox.tool_base")

    class _SandboxToolsBase:
        def __init__(
            self,
            project_id: str,
            thread_manager=None,
            sandbox_type: str = "desktop",
            **_kwargs,
        ):
            self.project_id = project_id
            self.thread_manager = thread_manager
            self.sandbox_type = sandbox_type
            self.workspace_path = "/workspace"
            self._sandbox = None
            self._sandbox_id = None
            self._sandbox_pass = None

        async def _sync_workspace_artifacts_from_sandbox(self, *, source: str):
            return None

    tool_base_mod.SandboxToolsBase = _SandboxToolsBase
    return tool_base_mod


def _build_redis_stub():
    async def _noop_async(*_args, **_kwargs):
        return 1

    return types.SimpleNamespace(
        rpush=_noop_async,
        publish=_noop_async,
        expire=_noop_async,
        keys=_noop_async,
        llen=_noop_async,
        REDIS_KEY_TTL=3600,
    )


def _install_sandbox_code_tool_stubs(monkeypatch):
    monkeypatch.setitem(sys.modules, "structlog", _build_structlog_stub())
    monkeypatch.setitem(sys.modules, "agentpress", types.ModuleType("agentpress"))
    monkeypatch.setitem(sys.modules, "agentpress.tool", _build_agentpress_tool_stub())
    monkeypatch.setitem(
        sys.modules,
        "agentpress.adk_thread_manager",
        _build_adk_thread_manager_stub(),
    )

    sandbox_package = sys.modules.get("sandbox")
    if sandbox_package is None:
        sandbox_package = types.ModuleType("sandbox")
        sandbox_package.__path__ = []
        monkeypatch.setitem(sys.modules, "sandbox", sandbox_package)
    tool_base_mod = _build_tool_base_stub()
    monkeypatch.setitem(sys.modules, "sandbox.tool_base", tool_base_mod)
    monkeypatch.setattr(sandbox_package, "tool_base", tool_base_mod, raising=False)

    services_package = sys.modules.get("services")
    if services_package is None:
        services_package = types.ModuleType("services")
        monkeypatch.setitem(sys.modules, "services", services_package)
    redis_stub = _build_redis_stub()
    monkeypatch.setitem(sys.modules, "services.redis", redis_stub)
    monkeypatch.setattr(services_package, "redis", redis_stub, raising=False)


@pytest.fixture
def sandbox_code_tool_module(monkeypatch):
    _install_sandbox_code_tool_stubs(monkeypatch)

    spec = importlib.util.spec_from_file_location("sandbox_code_tool_under_test", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


@pytest.mark.asyncio
async def test_emit_write_file_status_pushes_to_redis(monkeypatch, sandbox_code_tool_module):
    tool = sandbox_code_tool_module.SandboxCodeTool(project_id="project-1", thread_manager=None)

    monkeypatch.setattr(
        tool,
        "_resolve_agent_run_context",
        lambda: ("run-1", "thread-1"),
    )
    monkeypatch.setattr(
        tool,
        "_is_run_write_allowed",
        lambda *_args, **_kwargs: _async_true(),
    )

    pushed_payloads = []
    published_events = []

    async def _fake_rpush(key: str, payload: str):
        pushed_payloads.append((key, json.loads(payload)))
        return 1

    async def _fake_publish(channel: str, message: str):
        published_events.append((channel, message))
        return 1

    monkeypatch.setattr(sandbox_code_tool_module.redis, "rpush", _fake_rpush)
    monkeypatch.setattr(sandbox_code_tool_module.redis, "publish", _fake_publish)

    await tool._emit_write_file_status("tool_started", "/workspace/demo.md")

    assert len(pushed_payloads) == 1
    key, payload = pushed_payloads[0]
    assert key == "agent_run:run-1:responses"
    assert payload["thread_id"] == "thread-1"
    assert payload["type"] == "status"

    content_payload = json.loads(payload["content"])
    assert content_payload["status_type"] == "tool_started"
    assert content_payload["function_name"] == "write_file"

    assert published_events == [("agent_run:run-1:new_response", "new")]


@pytest.mark.asyncio
async def test_emit_write_file_status_uses_attempt_scoped_response_list_when_execution_epoch_present(
    monkeypatch,
    sandbox_code_tool_module,
):
    tool = sandbox_code_tool_module.SandboxCodeTool(project_id="project-phase2", thread_manager=None)

    monkeypatch.setattr(
        tool,
        "_resolve_agent_run_context",
        lambda: ("run-phase2", "thread-phase2", 7),
    )
    monkeypatch.setattr(
        tool,
        "_is_run_write_allowed",
        lambda *_args, **_kwargs: _async_true(),
    )

    pushed_payloads = []

    async def _fake_rpush(key: str, payload: str):
        pushed_payloads.append((key, json.loads(payload)))
        return 1

    monkeypatch.setattr(sandbox_code_tool_module.redis, "rpush", _fake_rpush)
    monkeypatch.setattr(sandbox_code_tool_module.redis, "publish", _async_noop)

    await tool._emit_write_file_status("tool_started", "/workspace/phase2.md")

    assert [key for key, _payload in pushed_payloads] == [
        "agent_run:run-phase2:epoch:7:responses",
        "agent_run:run-phase2:responses",
    ]


@pytest.mark.asyncio
async def test_stream_write_progress_mirrors_run_wide_response_list_when_execution_epoch_present(
    monkeypatch,
    sandbox_code_tool_module,
):
    tool = sandbox_code_tool_module.SandboxCodeTool(
        project_id="project-phase2-progress",
        thread_manager=None,
    )

    monkeypatch.setattr(
        tool,
        "_resolve_agent_run_context",
        lambda: ("run-phase2-progress", "thread-phase2", 7),
    )
    monkeypatch.setattr(
        tool,
        "_is_run_write_allowed",
        lambda *_args, **_kwargs: _async_true(),
    )
    monkeypatch.setattr(sandbox_code_tool_module, "WRITE_STREAM_MAX_UPDATES", 2)
    monkeypatch.setattr(sandbox_code_tool_module, "WRITE_STREAM_MIN_CHUNK_CHARS", 2)
    monkeypatch.setattr(sandbox_code_tool_module, "WRITE_STREAM_CHUNK_DELAY_SEC", 0)
    monkeypatch.setattr(sandbox_code_tool_module, "WRITE_STREAM_MAX_CHARS", 100)
    monkeypatch.setattr(
        sandbox_code_tool_module,
        "WRITE_STREAM_TOKEN_MODE_MAX_CHARS",
        0,
    )

    pushed_keys = []

    async def _fake_rpush(key: str, _payload: str):
        pushed_keys.append(key)
        return len(pushed_keys)

    monkeypatch.setattr(sandbox_code_tool_module.redis, "rpush", _fake_rpush)
    monkeypatch.setattr(sandbox_code_tool_module.redis, "publish", _async_noop)

    await tool._stream_write_progress("/workspace/phase2-progress.md", "abcd")

    assert pushed_keys == [
        "agent_run:run-phase2-progress:epoch:7:responses",
        "agent_run:run-phase2-progress:responses",
        "agent_run:run-phase2-progress:epoch:7:responses",
        "agent_run:run-phase2-progress:responses",
    ]


@pytest.mark.asyncio
async def test_stream_write_progress_halts_on_redis_error_without_raising(
    monkeypatch,
    sandbox_code_tool_module,
):
    tool = sandbox_code_tool_module.SandboxCodeTool(project_id="project-2", thread_manager=None)

    monkeypatch.setattr(
        tool,
        "_resolve_agent_run_context",
        lambda: ("run-2", "thread-2"),
    )
    monkeypatch.setattr(
        tool,
        "_is_run_write_allowed",
        lambda *_args, **_kwargs: _async_true(),
    )

    async def _failing_rpush(*args, **kwargs):
        raise TimeoutError("redis timeout")

    async def _failing_publish(*args, **kwargs):
        raise TimeoutError("redis timeout")

    monkeypatch.setattr(sandbox_code_tool_module.redis, "rpush", _failing_rpush)
    monkeypatch.setattr(sandbox_code_tool_module.redis, "publish", _failing_publish)

    # Should return early instead of propagating the Redis timeout.
    await tool._stream_write_progress("/workspace/demo.md", "abcdef")


async def _async_true():
    return True


async def _async_false():
    return False


async def _async_noop(*_args, **_kwargs):
    return None


@pytest.mark.asyncio
async def test_emit_write_file_status_respects_terminal_fence(monkeypatch, sandbox_code_tool_module):
    tool = sandbox_code_tool_module.SandboxCodeTool(project_id="project-3", thread_manager=None)

    monkeypatch.setattr(
        tool,
        "_resolve_agent_run_context",
        lambda: ("run-3", "thread-3"),
    )
    monkeypatch.setattr(
        tool,
        "_is_run_write_allowed",
        lambda *_args, **_kwargs: _async_false(),
    )

    pushed_payloads = []

    async def _fake_rpush(key: str, payload: str):
        pushed_payloads.append((key, payload))
        return 1

    monkeypatch.setattr(sandbox_code_tool_module.redis, "rpush", _fake_rpush)

    await tool._emit_write_file_status("tool_started", "/workspace/demo.md")
    assert pushed_payloads == []


@pytest.mark.asyncio
async def test_stream_write_progress_stops_when_terminal_fence_flips(
    monkeypatch,
    sandbox_code_tool_module,
):
    tool = sandbox_code_tool_module.SandboxCodeTool(project_id="project-4", thread_manager=None)

    monkeypatch.setattr(
        tool,
        "_resolve_agent_run_context",
        lambda: ("run-4", "thread-4"),
    )

    allowed_calls = {"count": 0}

    async def _toggle_allowed(*_args, **_kwargs):
        allowed_calls["count"] += 1
        return allowed_calls["count"] < 3

    monkeypatch.setattr(tool, "_is_run_write_allowed", _toggle_allowed)

    pushed_payloads = []

    async def _fake_rpush(key: str, payload: str):
        pushed_payloads.append((key, payload))
        return len(pushed_payloads)

    async def _fake_publish(_channel: str, _message: str):
        return 1

    monkeypatch.setattr(sandbox_code_tool_module.redis, "rpush", _fake_rpush)
    monkeypatch.setattr(sandbox_code_tool_module.redis, "publish", _fake_publish)
    monkeypatch.setattr(sandbox_code_tool_module.redis, "expire", _fake_publish)
    monkeypatch.setattr(sandbox_code_tool_module, "WRITE_STREAM_CHUNK_DELAY_SEC", 0.0)

    await tool._stream_write_progress("/workspace/demo.md", "abcdef")

    # Initial gate check + first chunk check pass; second chunk check should stop.
    assert len(pushed_payloads) == 1


@pytest.mark.asyncio
async def test_stream_write_progress_reduces_updates_when_backlog_is_high(
    monkeypatch,
    sandbox_code_tool_module,
):
    tool = sandbox_code_tool_module.SandboxCodeTool(project_id="project-4b", thread_manager=None)

    monkeypatch.setattr(
        tool,
        "_resolve_agent_run_context",
        lambda: ("run-4b", "thread-4b"),
    )
    monkeypatch.setattr(
        tool,
        "_is_run_write_allowed",
        lambda *_args, **_kwargs: _async_true(),
    )

    pushed_payloads = []

    async def _fake_rpush(_key: str, payload: str):
        pushed_payloads.append(json.loads(payload))
        return len(pushed_payloads)

    async def _fake_publish(_channel: str, _message: str):
        return 1

    async def _fake_llen(_key: str):
        return 99

    monkeypatch.setattr(sandbox_code_tool_module.redis, "rpush", _fake_rpush)
    monkeypatch.setattr(sandbox_code_tool_module.redis, "publish", _fake_publish)
    monkeypatch.setattr(sandbox_code_tool_module.redis, "expire", _fake_publish)
    monkeypatch.setattr(sandbox_code_tool_module.redis, "llen", _fake_llen)
    monkeypatch.setattr(sandbox_code_tool_module, "WRITE_STREAM_CHUNK_DELAY_SEC", 0.0)
    monkeypatch.setattr(sandbox_code_tool_module, "WRITE_STREAM_HIGH_BACKLOG_THRESHOLD", 5)
    monkeypatch.setattr(sandbox_code_tool_module, "WRITE_STREAM_HIGH_BACKLOG_MAX_UPDATES", 2)

    await tool._stream_write_progress("/workspace/demo.md", "abcdef")

    assert len(pushed_payloads) == 2
    first_tool_call = json.loads(
        pushed_payloads[0]["metadata"],
    )["tool_calls"][0]["function"]["arguments"]
    second_tool_call = json.loads(
        pushed_payloads[1]["metadata"],
    )["tool_calls"][0]["function"]["arguments"]
    assert json.loads(first_tool_call)["delta_index"] == 0
    assert json.loads(second_tool_call)["delta_index"] == 1


@pytest.mark.asyncio
async def test_execute_command_emits_started_and_completed_status(
    monkeypatch,
    sandbox_code_tool_module,
):
    tool = sandbox_code_tool_module.SandboxCodeTool(project_id="project-5", thread_manager=None)

    monkeypatch.setattr(tool, "_ensure_sandbox", _async_noop, raising=False)
    monkeypatch.setattr(
        tool,
        "_normalize_path",
        lambda path: f"/workspace/{path.strip('/')}" if path else "/workspace",
        raising=False,
    )

    run_calls = []

    class _FakeRunResult:
        exit_code = 0
        stdout = "ok\n"
        stderr = ""

    tool.sandbox = types.SimpleNamespace(
        commands=types.SimpleNamespace(
            run=lambda command, **kwargs: run_calls.append((command, kwargs)) or _FakeRunResult(),
        ),
    )

    async def _fake_run_blocking_sandbox_call(_name, callback, **_kwargs):
        return callback()

    lifecycle_events = []

    async def _fake_emit_tool_status(
        status_type,
        function_name,
        *,
        arguments=None,
        message=None,
        extra=None,
        xml_tag_name=None,
        tool_index=0,
    ):
        lifecycle_events.append(
            {
                "status_type": status_type,
                "function_name": function_name,
                "arguments": arguments,
                "message": message,
                "extra": extra,
                "xml_tag_name": xml_tag_name,
                "tool_index": tool_index,
            },
        )

    monkeypatch.setattr(
        tool,
        "_run_blocking_sandbox_call",
        _fake_run_blocking_sandbox_call,
        raising=False,
    )
    monkeypatch.setattr(tool, "_emit_tool_status", _fake_emit_tool_status)

    result = await tool.execute_command("echo hi", workdir="demo", timeout=42)

    assert result.success is True
    assert len(run_calls) == 1
    command, run_kwargs = run_calls[0]
    assert command == "cd /workspace/demo && echo hi"
    assert run_kwargs["timeout"] == 42
    assert [event["status_type"] for event in lifecycle_events] == [
        "tool_started",
        "tool_completed",
    ]
    assert all(event["function_name"] == "execute_command" for event in lifecycle_events)
    assert lifecycle_events[0]["arguments"] == {
        "command": "echo hi",
        "workdir": "/workspace/demo",
        "timeout": 42,
        "session_name": None,
        "blocking": True,
    }
    assert lifecycle_events[1]["extra"] == {"exit_code": 0}


@pytest.mark.asyncio
async def test_execute_command_emits_started_and_failed_status_for_nonzero_exit(
    monkeypatch,
    sandbox_code_tool_module,
):
    tool = sandbox_code_tool_module.SandboxCodeTool(project_id="project-6", thread_manager=None)

    monkeypatch.setattr(tool, "_ensure_sandbox", _async_noop, raising=False)
    monkeypatch.setattr(
        tool,
        "_normalize_path",
        lambda path: f"/workspace/{path.strip('/')}" if path else "/workspace",
        raising=False,
    )

    class _FakeRunResult:
        exit_code = 17
        stdout = ""
        stderr = "boom"

    tool.sandbox = types.SimpleNamespace(
        commands=types.SimpleNamespace(
            run=lambda *_args, **_kwargs: _FakeRunResult(),
        ),
    )

    async def _fake_run_blocking_sandbox_call(_name, callback, **_kwargs):
        return callback()

    lifecycle_events = []

    async def _fake_emit_tool_status(
        status_type,
        function_name,
        *,
        arguments=None,
        message=None,
        extra=None,
        xml_tag_name=None,
        tool_index=0,
    ):
        lifecycle_events.append(
            {
                "status_type": status_type,
                "function_name": function_name,
                "arguments": arguments,
                "message": message,
                "extra": extra,
                "xml_tag_name": xml_tag_name,
                "tool_index": tool_index,
            },
        )

    monkeypatch.setattr(
        tool,
        "_run_blocking_sandbox_call",
        _fake_run_blocking_sandbox_call,
        raising=False,
    )
    monkeypatch.setattr(tool, "_emit_tool_status", _fake_emit_tool_status)

    result = await tool.execute_command("exit 17")

    assert result.success is False
    assert [event["status_type"] for event in lifecycle_events] == [
        "tool_started",
        "tool_failed",
    ]
    assert lifecycle_events[1]["extra"] == {"exit_code": 17}


@pytest.mark.asyncio
async def test_execute_command_syncs_workspace_artifacts_after_command(
    monkeypatch,
    sandbox_code_tool_module,
):
    tool = sandbox_code_tool_module.SandboxCodeTool(project_id="project-7", thread_manager=None)

    monkeypatch.setattr(tool, "_ensure_sandbox", _async_noop, raising=False)

    class _FakeRunResult:
        exit_code = 0
        stdout = "ok"
        stderr = ""

    tool.sandbox = types.SimpleNamespace(
        commands=types.SimpleNamespace(
            run=lambda *_args, **_kwargs: _FakeRunResult(),
        ),
    )

    async def _fake_run_blocking_sandbox_call(_name, callback, **_kwargs):
        return callback()

    sync_calls = []

    async def _fake_sync_workspace_artifacts_from_sandbox(*, source):
        sync_calls.append(source)

    monkeypatch.setattr(
        tool,
        "_run_blocking_sandbox_call",
        _fake_run_blocking_sandbox_call,
        raising=False,
    )
    monkeypatch.setattr(
        tool,
        "_sync_workspace_artifacts_from_sandbox",
        _fake_sync_workspace_artifacts_from_sandbox,
        raising=False,
    )

    result = await tool.execute_command("echo build")

    assert result.success is True
    assert sync_calls == ["sandbox_code_tool.execute_command"]
