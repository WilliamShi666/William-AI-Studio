import importlib
import sys
import types
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

if "tavily" not in sys.modules:
    sys.modules["tavily"] = types.SimpleNamespace(
        AsyncTavilyClient=lambda api_key=None: types.SimpleNamespace(search=None),
    )
if "dotenv" not in sys.modules:
    sys.modules["dotenv"] = types.SimpleNamespace(load_dotenv=lambda *args, **kwargs: None)
if "agentpress.tool" not in sys.modules:
    class _ToolResult:
        def __init__(self, success=False, output=None):
            self.success = success
            self.output = output
    sys.modules.setdefault("agentpress", types.SimpleNamespace())
    sys.modules["agentpress.tool"] = types.SimpleNamespace(ToolResult=_ToolResult)
if "utils.config" not in sys.modules:
    sys.modules.setdefault("utils", types.SimpleNamespace())
    sys.modules["utils.config"] = types.SimpleNamespace(
        config=types.SimpleNamespace(
            TAVILY_API_KEY="test-tavily",
            FIRECRAWL_API_KEY="test-firecrawl",
            FIRECRAWL_URL="https://firecrawl.test",
        ),
    )
if "utils.agent_run_context" not in sys.modules:
    sys.modules["utils.agent_run_context"] = types.SimpleNamespace(
        get_agent_run_context=lambda: (None, None),
    )
if "sandbox.tool_base" not in sys.modules:
    class _StubSandboxToolsBase:
        def __init__(self, *args, **kwargs):
            pass
        def fail_response(self, output):
            from agentpress.tool import ToolResult
            return ToolResult(success=False, output=output)
    sys.modules.setdefault("sandbox", types.SimpleNamespace())
    sys.modules["sandbox.tool_base"] = types.SimpleNamespace(SandboxToolsBase=_StubSandboxToolsBase)

module = importlib.import_module("agent.tools.sandbox_web_search_tool")


@pytest.mark.parametrize(
    "query",
    [
        "9708_s24_qp_33 mark scheme answers",
        "answer key for uploaded exam pdf",
        "CIE economics question paper solutions 9708_m24_qp_32",
    ],
)
def test_companion_solution_lookup_detection_blocks_document_answer_searches(query):
    assert module._looks_like_companion_solution_lookup(query) is True


@pytest.mark.parametrize(
    "query",
    [
        "latest UK inflation data May 2026",
        "what is a mark scheme in assessment design",
        "climate policy solutions for market failure",
    ],
)
def test_companion_solution_lookup_detection_allows_general_web_research(query):
    assert module._looks_like_companion_solution_lookup(query) is False


@pytest.mark.asyncio
async def test_web_search_refuses_companion_solution_lookup_without_network(monkeypatch):
    tool = module.SandboxWebSearchTool(project_id="project", thread_manager=None)
    called = {"value": False}

    async def _search(**kwargs):
        called["value"] = True
        return {"results": [{"title": "should not be called"}]}

    tool.tavily_client = types.SimpleNamespace(search=_search)

    result = await tool.web_search("9708_s24_qp_33 mark scheme answers", num_results=5)

    assert result.success is False
    assert result.output["error_code"] == "DOCUMENT_COMPANION_SEARCH_REDIRECT"
    assert "provided files" in result.output["error"]
    assert called["value"] is False
