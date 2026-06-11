import importlib.util
import sys
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
ORCHESTRATOR_AGENT_PATH = BACKEND_ROOT / "agentscope_integration" / "agents" / "orchestrator.py"
WORKER_AGENT_PATH = BACKEND_ROOT / "agentscope_integration" / "agents" / "worker.py"
ORCHESTRATOR_PROMPT_PATH = BACKEND_ROOT / "agentscope_integration" / "prompts" / "orchestrator_prompt.py"
WORKER_PROMPT_PATH = BACKEND_ROOT / "agentscope_integration" / "prompts" / "worker_prompt.py"


def _load_module(module_name: str, path: Path):
    spec = importlib.util.spec_from_file_location(module_name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_orchestrator_agent_source_uses_parallel_tool_call_flag():
    source = ORCHESTRATOR_AGENT_PATH.read_text(encoding="utf-8")
    assert "AGENTSCOPE_PARALLEL_TOOL_CALLS" in source
    assert "parallel_tool_calls=self._parallel_tool_calls_enabled" in source


def test_worker_agent_source_uses_parallel_tool_call_flag():
    source = WORKER_AGENT_PATH.read_text(encoding="utf-8")
    assert "AGENTSCOPE_PARALLEL_TOOL_CALLS" in source
    assert "parallel_tool_calls=self._parallel_tool_calls_enabled" in source


def test_orchestrator_prompt_allows_parallel_tool_calls():
    module = _load_module("orchestrator_prompt_parallel_test", ORCHESTRATOR_PROMPT_PATH)
    prompt = module.get_orchestrator_prompt()
    assert "No parallel operations" not in prompt
    assert "Parallel execution is enabled" in prompt
    assert "edit_file" in prompt


def test_worker_prompt_allows_parallel_tool_calls():
    module = _load_module("worker_prompt_parallel_test", WORKER_PROMPT_PATH)
    prompt = module.get_worker_prompt()
    assert "No parallel operations" not in prompt
    assert "Parallel execution is enabled" in prompt
    assert "edit_file" in prompt
