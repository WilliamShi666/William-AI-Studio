import importlib.util
import sys
import types
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

# Lightweight structlog stub so utils.logger can import in test environments.
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

MODULE_PATH = BACKEND_ROOT / "agentscope_integration" / "state" / "resume_state.py"

spec = importlib.util.spec_from_file_location("resume_state_module", MODULE_PATH)
resume_state_module = importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules["resume_state_module"] = resume_state_module
spec.loader.exec_module(resume_state_module)

ResumeCoordinator = resume_state_module.ResumeCoordinator


def test_objective_fingerprint_is_stable():
    a = ResumeCoordinator.build_objective_fingerprint("  Analyze Tesla  Q4   report ")
    b = ResumeCoordinator.build_objective_fingerprint("analyze tesla q4 report")
    assert a == b
    assert len(a) == 24


def test_resume_intent_detection():
    assert ResumeCoordinator.is_resume_intent("请继续刚才的任务")
    assert ResumeCoordinator.is_resume_intent("continue from previous checkpoint")
    assert not ResumeCoordinator.is_resume_intent("请帮我新建一个任务")


def test_resume_hint_contains_key_sections():
    state = types.SimpleNamespace(
        phase="writing",
        next_step="Finalize evidence table",
        completed_steps=["Collected sources", "Drafted report"],
        artifacts={
            "report_latest": "research/report.latest.md",
            "sources_latest": "research/sources.latest.json",
        },
        summary="Draft has most findings; evidence table pending.",
        updated_at="2026-02-06T00:00:00+00:00",
    )

    hint = ResumeCoordinator.build_resume_hint(state)
    assert "Resume Context" in hint
    assert "Finalize evidence table" in hint
    assert "/workspace/research/report.latest.md" in hint
