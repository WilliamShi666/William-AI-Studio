from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parents[1].parent
    / "agent_eval"
    / "wrong_mcq_report_e2e_eval.py"
)
spec = importlib.util.spec_from_file_location("wrong_mcq_report_e2e_eval", SCRIPT)
module = importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules["wrong_mcq_report_e2e_eval"] = module
spec.loader.exec_module(module)


def _call(name: str, args: dict, *, success: bool = True) -> dict:
    return {"name": name, "arguments": args, "success": success}


def test_validate_reports_require_student_task_papers_and_html_shell() -> None:
    body = (
        "# 胡宜含 A2 Economics 错题分析报告\n\n"
        "本报告围绕错题、知识点和复习建议展开。\n"
        + "\n".join(module.EXPECTED_PAPERS)
        + "\n"
        + "错题分析 " * 400
    )

    assert module.validate_markdown_report(body)
    assert module.validate_html_report(f"<html><body>{body}</body></html>")
    assert not module.validate_html_report(body)
    assert not module.validate_markdown_report("sandbox was not found timeout summary")


def test_count_modifications_excludes_direct_writes_but_counts_edits_and_shell_mutations() -> (
    None
):
    calls = [
        _call("execute_command", {"command": "pdftotext /workspace/a.pdf -"}),
        _call(
            "write_file", {"path": "/workspace/wrong_mcq_analysis.md", "content": "x"}
        ),
        _call(
            "write_file", {"path": "/workspace/wrong_mcq_analysis.html", "content": "x"}
        ),
        _call(
            "execute_command",
            {"command": "sed -n '1,10p' /workspace/wrong_mcq_analysis.md"},
        ),
        _call("edit_file", {"path": "/workspace/wrong_mcq_analysis.md"}),
        _call(
            "execute_command",
            {
                "command": "python3 << 'PY'\nfrom pathlib import Path\nPath('/workspace/a.html').write_text('x')\nPY"
            },
        ),
        _call("write_file", {"path": "/workspace/failed.md"}, success=False),
    ]

    assert module.count_modifications(calls) == 2


def test_e2e_defaults_to_openrouter_deepseek_with_relaxed_edit_budget() -> None:
    assert module.DEFAULT_MAX_MODIFICATIONS == 10
    assert module.DEFAULT_MAX_MODIFICATIONS > 5


def test_find_pdf_dir_accepts_trailing_space_directory() -> None:
    path = module.find_pdf_dir("Experimental runs_CIE A2 Wrong MCQs report ")
    assert path.exists()
    assert list(path.glob("9708_*_qp_*.pdf"))


@pytest.mark.asyncio
async def test_find_reports_accepts_valid_artifacts_with_unexpected_names() -> None:
    body = (
        "# 胡宜含 A2 Economics 错题分析报告\n\n"
        "本报告围绕错题、知识点和复习建议展开。\n"
        + "\n".join(module.EXPECTED_PAPERS)
        + "\n"
        + "错题分析 " * 400
    )
    artifacts = {
        "/workspace/student_a2_review.md": body,
        "/workspace/student_a2_review.html": f"<html><body>{body}</body></html>",
    }
    runner = object.__new__(module.Runner)

    async def artifact_text(*, project_id: str, path: str) -> str | None:
        return artifacts.get(path)

    async def artifact_paths(*, project_id: str, suffix: str) -> list[str]:
        return [path for path in artifacts if path.endswith(suffix)]

    runner._artifact_text = artifact_text
    runner._artifact_paths = artifact_paths

    md_path, html_path, md_ok, html_ok = await runner._find_reports(
        client=None,
        sandbox_id="sandbox-id",
        project_id="project-id",
    )

    assert (md_path, html_path, md_ok, html_ok) == (
        "/workspace/student_a2_review.md",
        "/workspace/student_a2_review.html",
        True,
        True,
    )


def test_wrong_mcq_prompt_requests_static_report_and_dynamic_enhancement_split() -> (
    None
):
    prompt = module.WRONG_MCQ_PROMPT

    assert "Markdown" in prompt or "markdown" in prompt
    assert "HTML" in prompt or "html" in prompt
    assert "静态" in prompt
    assert "动态" in prompt
    assert "正文" in prompt
    assert "增强" in prompt
    assert "足够证据" in prompt
    assert "不要逐题" in prompt
    assert "最多运行4条文档抽取/检索命令" in prompt
    assert "不要先 create_tasks 或 update_tasks" in prompt
    assert "同一条 assistant 回复中连续调用两个 write_file" in prompt
    assert "/workspace/wrong_mcq_report.md" in prompt
    assert "/workspace/wrong_mcq_report.html" in prompt


def test_retryable_attempt_failure_is_limited_to_transient_infra_errors() -> None:
    assert module.is_retryable_attempt_failure(
        module.AttemptResult(
            attempt=1,
            passed=False,
            status="failed",
            error="Instance single shutting down",
        )
    )
    assert module.is_retryable_attempt_failure(
        module.AttemptResult(
            attempt=1,
            passed=False,
            status="error",
            error="All connection attempts failed",
        )
    )
    assert module.is_retryable_attempt_failure(
        module.AttemptResult(
            attempt=1,
            passed=False,
            status="stopped",
            markdown_valid=False,
            html_valid=False,
            tool_call_count=1,
        )
    )
    assert not module.is_retryable_attempt_failure(
        module.AttemptResult(
            attempt=1,
            passed=False,
            status="completed",
            markdown_valid=False,
            html_valid=False,
        )
    )
