import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from agent.tools.task_list_tool import TaskListTool


def test_document_report_plan_redirects_exhaustive_extraction_tasks() -> None:
    tool = TaskListTool(
        project_id="project-1", thread_manager=None, thread_id="thread-1"
    )

    redirect = tool._document_report_plan_redirect(
        [
            "Extract all question content from every uploaded PDF",
            "Write Markdown report",
            "Create HTML report",
        ]
    )

    assert redirect is not None
    assert "DOCUMENT_REPORT_TASK_PLAN_REDIRECT" in redirect


def test_document_report_plan_allows_coarse_evidence_task() -> None:
    tool = TaskListTool(
        project_id="project-1", thread_manager=None, thread_id="thread-1"
    )

    redirect = tool._document_report_plan_redirect(
        [
            "Collect sufficient evidence from the provided documents",
            "Write Markdown report",
            "Create HTML report",
        ]
    )

    assert redirect is None
