import sys
import types
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
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

if "agentpress.adk_thread_manager" not in sys.modules:
    sys.modules["agentpress.adk_thread_manager"] = types.SimpleNamespace(
        ADKThreadManager=object,
    )

if "sandbox.tool_base" not in sys.modules:
    class _StubSandboxToolsBase:
        def __init__(self, project_id: str, thread_manager=None, sandbox_type: str = "code"):
            self.project_id = project_id
            self.thread_manager = thread_manager
            self.sandbox_type = sandbox_type
            self.workspace_path = "/workspace"
            self.sandbox = None

        @staticmethod
        def clean_path(path: str) -> str:
            return str(path).lstrip("/")

        async def _ensure_sandbox(self):
            return None

        async def _run_blocking_sandbox_call(self, _operation, call, timeout_seconds=None):
            del timeout_seconds
            return call()

    sys.modules["sandbox.tool_base"] = types.SimpleNamespace(
        SandboxToolsBase=_StubSandboxToolsBase,
    )

if "services.redis" not in sys.modules:
    async def _noop(*args, **kwargs):
        del args, kwargs
        return 1

    sys.modules["services.redis"] = types.SimpleNamespace(
        rpush=_noop,
        expire=_noop,
        publish=_noop,
    )

from agent.tools import sandbox_code_tool as sandbox_code_tool_module
from agent.tools.sandbox_code_tool import _truncate, _truncate_head_tail


class TestTruncateHeadTail:
    def test_short_text_unchanged(self):
        """Text under limit should be returned as-is."""
        text = "short output"
        result = _truncate_head_tail(text, limit=100)
        assert result == "short output"

    def test_empty_text(self):
        assert _truncate_head_tail("") == ""
        assert _truncate_head_tail(None) == ""

    def test_head_only_truncation_default(self):
        """Default _truncate does head-only truncation (unchanged)."""
        text = "A" * 200
        result = _truncate(text, limit=100)
        assert len(result) < len(text)
        assert result.startswith("A" * 100)
        assert "<truncated" in result

    def test_execute_command_uses_head_tail_for_long_output(self):
        """Head+tail truncation preserves both start and end of long output.
        Errors typically appear at the end, so tail preservation is critical."""
        long_output = "line start\n" + ("mid " * 5000) + "\nerror: something failed at line 5000"
        result = _truncate_head_tail(long_output, limit=2000)
        assert result.startswith("line start\n")
        assert "error: something failed" in result
        assert "<truncated" in result
        assert len(result) < len(long_output)


class _FakeCommands:
    def __init__(self):
        self.calls = []

    def run(self, command: str, **kwargs):
        self.calls.append({"command": command, "kwargs": kwargs})
        return types.SimpleNamespace(exit_code=0, stdout="ok", stderr="")


class _FakeSandbox:
    def __init__(self):
        self.commands = _FakeCommands()
        self.run_code_calls = []

    def run_code(self, code: str, **kwargs):
        self.run_code_calls.append({"code": code, "kwargs": kwargs})
        return types.SimpleNamespace(logs="", result="ok", error=None)


def _clear_dashscope_env_defaults(monkeypatch):
    monkeypatch.setattr(sandbox_code_tool_module.config, "DASHSCOPE_API_KEY", None, raising=False)
    monkeypatch.setattr(sandbox_code_tool_module.config, "DASHSCOPE_BASE_URL", None, raising=False)
    monkeypatch.setattr(sandbox_code_tool_module.config, "DASHSCOPE_VISION_MODEL", None, raising=False)


@pytest.mark.asyncio
async def test_execute_command_omits_envs_when_no_overrides(monkeypatch):
    _clear_dashscope_env_defaults(monkeypatch)
    tool = sandbox_code_tool_module.SandboxCodeTool(project_id="project-env-1", thread_manager=None)
    fake_sandbox = _FakeSandbox()
    tool.sandbox = fake_sandbox

    async def _ensure_sandbox():
        return None

    async def _run_blocking_sandbox_call(_operation, call, timeout_seconds=None):
        del timeout_seconds
        return call()

    monkeypatch.setattr(tool, "_ensure_sandbox", _ensure_sandbox)
    monkeypatch.setattr(tool, "_run_blocking_sandbox_call", _run_blocking_sandbox_call)

    result = await tool.execute_command("echo hello")

    assert result.success is True
    assert fake_sandbox.commands.calls
    first_call = fake_sandbox.commands.calls[0]
    assert "envs" not in first_call["kwargs"]


@pytest.mark.asyncio
async def test_execute_command_merges_caller_env_with_dashscope_fallback(monkeypatch):
    monkeypatch.setattr(
        sandbox_code_tool_module.config,
        "DASHSCOPE_API_KEY",
        "sk-test-key",
        raising=False,
    )
    monkeypatch.setattr(
        sandbox_code_tool_module.config,
        "DASHSCOPE_BASE_URL",
        "https://dashscope.test/v1",
        raising=False,
    )
    monkeypatch.setattr(
        sandbox_code_tool_module.config,
        "DASHSCOPE_VISION_MODEL",
        "qwen3.5-vision-test",
        raising=False,
    )

    tool = sandbox_code_tool_module.SandboxCodeTool(project_id="project-env-2", thread_manager=None)
    fake_sandbox = _FakeSandbox()
    tool.sandbox = fake_sandbox

    async def _ensure_sandbox():
        return None

    async def _run_blocking_sandbox_call(_operation, call, timeout_seconds=None):
        del timeout_seconds
        return call()

    monkeypatch.setattr(tool, "_ensure_sandbox", _ensure_sandbox)
    monkeypatch.setattr(tool, "_run_blocking_sandbox_call", _run_blocking_sandbox_call)

    caller_envs = {"FOO": "bar", "DASHSCOPE_BASE_URL": "https://caller.override/v1"}
    caller_envs_snapshot = dict(caller_envs)
    result = await tool.execute_command("echo hello", envs=caller_envs)

    assert result.success is True
    passed_envs = fake_sandbox.commands.calls[0]["kwargs"]["envs"]
    assert passed_envs["FOO"] == "bar"
    assert passed_envs["DASHSCOPE_API_KEY"] == "sk-test-key"
    assert passed_envs["DASHSCOPE_BASE_URL"] == "https://caller.override/v1"
    assert passed_envs["DASHSCOPE_VISION_MODEL"] == "qwen3.5-vision-test"
    assert caller_envs == caller_envs_snapshot


@pytest.mark.asyncio
async def test_run_code_omits_envs_when_no_overrides(monkeypatch):
    _clear_dashscope_env_defaults(monkeypatch)
    tool = sandbox_code_tool_module.SandboxCodeTool(project_id="project-env-3", thread_manager=None)
    fake_sandbox = _FakeSandbox()
    tool.sandbox = fake_sandbox

    async def _ensure_sandbox():
        return None

    async def _run_blocking_sandbox_call(_operation, call, timeout_seconds=None):
        del timeout_seconds
        return call()

    monkeypatch.setattr(tool, "_ensure_sandbox", _ensure_sandbox)
    monkeypatch.setattr(tool, "_run_blocking_sandbox_call", _run_blocking_sandbox_call)

    result = await tool.run_code("print('ok')")

    assert result.success is True
    assert fake_sandbox.run_code_calls
    first_call = fake_sandbox.run_code_calls[0]
    assert first_call["kwargs"]["language"] == "python"
    assert "envs" not in first_call["kwargs"]


@pytest.mark.asyncio
async def test_run_code_merges_dashscope_fallback(monkeypatch):
    monkeypatch.setattr(
        sandbox_code_tool_module.config,
        "DASHSCOPE_API_KEY",
        "sk-test-key",
        raising=False,
    )
    monkeypatch.setattr(
        sandbox_code_tool_module.config,
        "DASHSCOPE_BASE_URL",
        "https://dashscope.test/v1",
        raising=False,
    )
    monkeypatch.setattr(
        sandbox_code_tool_module.config,
        "DASHSCOPE_VISION_MODEL",
        "qwen3.5-vision-test",
        raising=False,
    )

    tool = sandbox_code_tool_module.SandboxCodeTool(project_id="project-env-4", thread_manager=None)
    fake_sandbox = _FakeSandbox()
    tool.sandbox = fake_sandbox

    async def _ensure_sandbox():
        return None

    async def _run_blocking_sandbox_call(_operation, call, timeout_seconds=None):
        del timeout_seconds
        return call()

    monkeypatch.setattr(tool, "_ensure_sandbox", _ensure_sandbox)
    monkeypatch.setattr(tool, "_run_blocking_sandbox_call", _run_blocking_sandbox_call)

    result = await tool.run_code("print('ok')", envs={"FOO": "bar"})

    assert result.success is True
    passed_envs = fake_sandbox.run_code_calls[0]["kwargs"]["envs"]
    assert passed_envs["FOO"] == "bar"
    assert passed_envs["DASHSCOPE_API_KEY"] == "sk-test-key"
    assert passed_envs["DASHSCOPE_BASE_URL"] == "https://dashscope.test/v1"
    assert passed_envs["DASHSCOPE_VISION_MODEL"] == "qwen3.5-vision-test"
