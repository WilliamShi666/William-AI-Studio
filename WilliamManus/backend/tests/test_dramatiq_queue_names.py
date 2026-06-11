import importlib.util
from pathlib import Path


QUEUE_NAMES_PATH = Path(__file__).resolve().parents[1] / "utils" / "dramatiq_queue_names.py"


def _load_queue_names_module():
    module_spec = importlib.util.spec_from_file_location(
        "dramatiq_queue_names_for_test",
        QUEUE_NAMES_PATH,
    )
    assert module_spec is not None and module_spec.loader is not None
    module = importlib.util.module_from_spec(module_spec)
    module_spec.loader.exec_module(module)
    return module


def test_dramatiq_queue_names_default_to_shared_queue_names(monkeypatch):
    monkeypatch.delenv("DRAMATIQ_RUN_AGENT_QUEUE", raising=False)
    monkeypatch.delenv("DRAMATIQ_SANDBOX_CLEANUP_QUEUE", raising=False)
    monkeypatch.delenv("SHADOW_CLONE_SUBAGENT_QUEUE", raising=False)

    queue_names = _load_queue_names_module()

    assert queue_names.RUN_AGENT_BACKGROUND_QUEUE == "default"
    assert queue_names.SANDBOX_CLEANUP_QUEUE == "sandbox_cleanup"
    assert queue_names.SHADOW_CLONE_SUBAGENT_QUEUE == "shadow_clone_subagents"


def test_dramatiq_queue_names_honor_explicit_overrides(monkeypatch):
    monkeypatch.setenv("DRAMATIQ_RUN_AGENT_QUEUE", "run_agent_background_worktree_8003")
    monkeypatch.setenv("DRAMATIQ_SANDBOX_CLEANUP_QUEUE", "sandbox_cleanup_worktree_8003")
    monkeypatch.setenv("SHADOW_CLONE_SUBAGENT_QUEUE", "shadow_clone_subagents_worktree_8003")

    queue_names = _load_queue_names_module()

    assert queue_names.RUN_AGENT_BACKGROUND_QUEUE == "run_agent_background_worktree_8003"
    assert queue_names.SANDBOX_CLEANUP_QUEUE == "sandbox_cleanup_worktree_8003"
    assert queue_names.SHADOW_CLONE_SUBAGENT_QUEUE == "shadow_clone_subagents_worktree_8003"
