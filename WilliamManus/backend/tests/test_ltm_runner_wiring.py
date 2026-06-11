import sys
import types
from pathlib import Path

import pytest

# Minimal stubs for optional infra deps required during module import.
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

if "services.postgresql" not in sys.modules:
    services_pkg = types.ModuleType("services")
    postgresql_mod = types.ModuleType("services.postgresql")

    class _DummyDBConnection:
        @property
        async def client(self):
            raise RuntimeError("DB client is not available in this unit test")

    postgresql_mod.DBConnection = _DummyDBConnection
    services_pkg.postgresql = postgresql_mod
    sys.modules["services"] = services_pkg
    sys.modules["services.postgresql"] = postgresql_mod

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

try:
    from agentscope_integration.runner import AgentScopeRunner
except Exception as exc:  # pragma: no cover - env-specific optional deps
    pytest.skip(f"Skipping LTM runner wiring test due to import error: {exc}", allow_module_level=True)

from agentscope.model import ChatModelBase
from agentscope.formatter import OpenAIChatFormatter
from agentscope.tool import Toolkit


class _DummyModel(ChatModelBase):
    async def __call__(self, *_args, **_kwargs):
        return {"ok": True}


class _DummyToolkitAdapter:
    def __init__(self, *args, **kwargs):
        self._toolkit = Toolkit()

    def get_toolkit(self):
        return self._toolkit


class _DummyWorkerAgent:
    last_init_kwargs = None

    def __init__(self, **kwargs):
        self.__class__.last_init_kwargs = kwargs
        self.agent = object()

    def get_agent(self):
        return self.agent

    def set_console_output_enabled(self, _enabled: bool):
        return None


class _DummyOrchestratorAgent:
    last_init_kwargs = None

    def __init__(self, **kwargs):
        self.__class__.last_init_kwargs = kwargs
        self.agent = object()

    def get_agent(self):
        return self.agent

    def set_console_output_enabled(self, _enabled: bool):
        return None

    async def __call__(self, _msg):
        return {"ok": True}


class _FakeLTM:
    def __init__(self):
        self.entered = False
        self.exited = False

    async def __aenter__(self):
        self.entered = True
        return self

    async def __aexit__(self, *_args):
        self.exited = True

    async def retrieve(self, *_args, **_kwargs):
        return "ltm"

    async def retrieve_from_memory(self, *_args, **_kwargs):
        return {"ok": True}


def _fake_model_create(_model_key, stream=True, **_kwargs):
    return _DummyModel("dummy-model", stream=stream), OpenAIChatFormatter()


@pytest.mark.asyncio
async def test_runner_injects_ltm_into_orchestrator_when_enabled(monkeypatch: pytest.MonkeyPatch):
    fake_ltm = _FakeLTM()

    monkeypatch.setenv("AGENTSCOPE_LTM_ENABLED", "true")
    monkeypatch.setenv("AGENTSCOPE_LTM_CONTROL", "both")
    monkeypatch.setenv("AGENTSCOPE_LTM_ATTACH_SCOPE", "orchestrator")

    monkeypatch.setattr("agentscope_integration.runner.ModelFactory.create", _fake_model_create)
    monkeypatch.setattr("agentscope_integration.runner.ToolkitAdapter", _DummyToolkitAdapter)
    monkeypatch.setattr("agentscope_integration.runner.WorkerAgent", _DummyWorkerAgent)
    monkeypatch.setattr("agentscope_integration.runner.OrchestratorAgent", _DummyOrchestratorAgent)
    monkeypatch.setattr(
        "agentscope_integration.runner.create_long_term_memory",
        lambda **_kwargs: fake_ltm,
    )

    runner = AgentScopeRunner(thread_id="t1", project_id="p1", model_key="glm-4.7")
    await runner.setup()

    assert fake_ltm.entered is True
    assert _DummyOrchestratorAgent.last_init_kwargs["long_term_memory"] is fake_ltm
    assert _DummyOrchestratorAgent.last_init_kwargs["long_term_memory_mode"] == "both"
    assert _DummyWorkerAgent.last_init_kwargs is not None
    assert _DummyWorkerAgent.last_init_kwargs["long_term_memory"] is not None
    assert _DummyWorkerAgent.last_init_kwargs["long_term_memory_mode"] == "static_control"
    assert _DummyWorkerAgent.last_init_kwargs["compression_config"] is not None

    await runner.close()
    assert fake_ltm.exited is True


@pytest.mark.asyncio
async def test_runner_skips_ltm_when_disabled(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AGENTSCOPE_LTM_ENABLED", "false")

    monkeypatch.setattr("agentscope_integration.runner.ModelFactory.create", _fake_model_create)
    monkeypatch.setattr("agentscope_integration.runner.ToolkitAdapter", _DummyToolkitAdapter)
    monkeypatch.setattr("agentscope_integration.runner.WorkerAgent", _DummyWorkerAgent)
    monkeypatch.setattr("agentscope_integration.runner.OrchestratorAgent", _DummyOrchestratorAgent)
    monkeypatch.setattr(
        "agentscope_integration.runner.create_long_term_memory",
        lambda **_kwargs: pytest.fail("LTM factory should not be called when disabled"),
    )

    runner = AgentScopeRunner(thread_id="t1", project_id="p1", model_key="glm-4.7")
    await runner.setup()

    assert _DummyOrchestratorAgent.last_init_kwargs["long_term_memory"] is None
    assert _DummyWorkerAgent.last_init_kwargs["long_term_memory"] is None
    assert _DummyWorkerAgent.last_init_kwargs["compression_config"] is not None
