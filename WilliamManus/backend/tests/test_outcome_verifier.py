from __future__ import annotations

import csv
import io
import json
import zipfile
from dataclasses import dataclass

import pytest

from eval.outcome_verifier import (
    CommandExecutionResult,
    OutcomeVerificationRequest,
    verify_outcome,
)


def _build_zip(entries: dict[str, str | bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, payload in entries.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


def _build_csv(rows: list[list[str]]) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


@dataclass(frozen=True)
class _CommandKey:
    argv: tuple[str, ...]
    cwd: str | None


class _FakeVerifierIO:
    def __init__(
        self,
        *,
        files: dict[str, bytes] | None = None,
        directories: set[str] | None = None,
        commands: dict[_CommandKey, CommandExecutionResult] | None = None,
    ) -> None:
        self._files = dict(files or {})
        self._directories = set(directories or set())
        self._commands = dict(commands or {})

    async def read_bytes(self, path: str) -> bytes | None:
        return self._files.get(path)

    async def list_dir(self, path: str) -> list[dict]:
        normalized = path.rstrip("/") or "/"
        if normalized not in self._directories:
            raise FileNotFoundError(path)

        prefix = normalized.rstrip("/") + "/"
        entries = []
        seen_names: set[str] = set()
        for file_path in self._files:
            if not file_path.startswith(prefix):
                continue
            remainder = file_path[len(prefix) :]
            if not remainder:
                continue
            name = remainder.split("/", 1)[0]
            if name in seen_names:
                continue
            seen_names.add(name)
            entries.append(
                {"name": name, "path": prefix + name, "is_dir": "/" in remainder}
            )
        return entries

    async def run_command(
        self,
        argv: list[str],
        *,
        cwd: str | None = None,
        timeout_seconds: int | None = None,
    ) -> CommandExecutionResult:
        del timeout_seconds
        key = _CommandKey(tuple(argv), cwd)
        if key not in self._commands:
            raise AssertionError(f"Unexpected command: argv={argv!r}, cwd={cwd!r}")
        return self._commands[key]


@pytest.mark.asyncio
async def test_verify_docx_and_pptx_zip_backed_outputs_pass() -> None:
    docx_path = "/workspace/eval_outputs/wm_word_grading_docx/grading-report.docx"
    pptx_path = "/workspace/eval_outputs/wm_html_to_ppt_conversion/lesson-deck.pptx"
    io = _FakeVerifierIO(
        files={
            docx_path: _build_zip(
                {
                    "word/document.xml": "<document>Overall Score Rubric Feedback</document>",
                }
            ),
            pptx_path: _build_zip(
                {
                    "[Content_Types].xml": "<Types/>",
                    "ppt/slides/slide1.xml": "<slide/>",
                    "ppt/slides/slide2.xml": "<slide/>",
                    "ppt/slides/slide3.xml": "<slide/>",
                }
            ),
        }
    )
    request = OutcomeVerificationRequest(
        task_id="wm_office_checks",
        project_id="proj-1",
        success_criteria=[
            {"type": "file_exists", "path": docx_path, "deterministic": True},
            {
                "type": "zip_entry_exists",
                "path": docx_path,
                "entry": "word/document.xml",
                "deterministic": True,
            },
            {
                "type": "zip_entry_contains",
                "path": docx_path,
                "entry": "word/document.xml",
                "contains": ["Overall Score", "Rubric", "Feedback"],
                "deterministic": True,
            },
            {"type": "file_exists", "path": pptx_path, "deterministic": True},
            {
                "type": "zip_entry_count_at_least",
                "path": pptx_path,
                "prefix": "ppt/slides/slide",
                "minimum": 3,
                "deterministic": True,
            },
        ],
    )

    result = await verify_outcome(request, io)

    assert result.passed is True
    assert len(result.checks) == 5
    assert docx_path in result.artifacts_found
    assert pptx_path in result.artifacts_found
    assert not result.artifacts_missing
    assert not result.errors


@pytest.mark.asyncio
async def test_verify_html_structure_text_and_links_pass() -> None:
    html_path = "/workspace/eval_outputs/wm_html_courseware_generation/index.html"
    io = _FakeVerifierIO(
        files={
            html_path: b"""
            <html>
              <body>
                <main>
                  <section>Learning Objectives</section>
                  <section data-quiz='true'>Quiz</section>
                  <button>Start</button>
                  <a href='lesson-1.html'>Lesson 1</a>
                  <a href='lesson-2.html'>Lesson 2</a>
                </main>
              </body>
            </html>
            """,
        }
    )
    request = OutcomeVerificationRequest(
        task_id="wm_html_checks",
        project_id="proj-1",
        success_criteria=[
            {"type": "file_exists", "path": html_path, "deterministic": True},
            {
                "type": "html_contains_selectors",
                "path": html_path,
                "selectors": ["main", "section", "button", "[data-quiz]"],
                "deterministic": True,
            },
            {
                "type": "html_contains_text",
                "path": html_path,
                "contains": ["Learning Objectives", "Quiz"],
                "deterministic": True,
            },
            {
                "type": "html_contains_links",
                "path": html_path,
                "minimum_links": 2,
                "deterministic": True,
            },
        ],
    )

    result = await verify_outcome(request, io)

    assert result.passed is True
    assert all(check.passed for check in result.checks)


@pytest.mark.asyncio
async def test_verify_json_csv_text_and_command_criteria_pass() -> None:
    json_path = (
        "/workspace/eval_outputs/wm_browser_form_workflow_export/submission.json"
    )
    csv_path = "/workspace/eval_outputs/wm_pdf_table_extraction_csv/output.csv"
    brief_path = "/workspace/eval_outputs/wm_local_docs_synthesis_brief/brief.md"
    cli_path = "/workspace/eval_outputs/wm_python_cli_json_to_csv/transform.py"
    io = _FakeVerifierIO(
        files={
            json_path: json.dumps(
                {"student_name": "Ada", "course": "Physics", "status": "submitted"}
            ).encode("utf-8"),
            csv_path: _build_csv(
                [
                    ["item", "value"],
                    ["alpha", "1"],
                    ["beta", "2"],
                    ["gamma", "3"],
                ]
            ),
            brief_path: b"## Summary\nSources\n[source-1]\n",
            "/workspace/exact.txt": b"DONE\n",
            cli_path: b"print('ok')\n",
        },
        commands={
            _CommandKey(
                (
                    "python",
                    cli_path,
                    "/workspace/eval_seed/wm_python_cli_json_to_csv/input.json",
                    "/workspace/eval_outputs/wm_python_cli_json_to_csv/recheck.csv",
                ),
                None,
            ): CommandExecutionResult(exit_code=0, stdout="ok", stderr=""),
        },
    )
    request = OutcomeVerificationRequest(
        task_id="wm_mixed_checks",
        project_id="proj-1",
        success_criteria=[
            {
                "type": "json_has_keys",
                "path": json_path,
                "keys": ["student_name", "course", "status"],
                "deterministic": True,
            },
            {
                "type": "csv_row_count_at_least",
                "path": csv_path,
                "minimum_rows": 3,
                "deterministic": True,
            },
            {
                "type": "csv_header_contains",
                "path": csv_path,
                "contains": ["item", "value"],
                "deterministic": True,
            },
            {
                "type": "text_contains",
                "path": brief_path,
                "contains": ["## Summary", "Sources"],
                "deterministic": True,
            },
            {
                "type": "text_equals",
                "path": "/workspace/exact.txt",
                "value": "DONE\n",
                "deterministic": True,
            },
            {
                "type": "text_matches_regex",
                "path": brief_path,
                "pattern": r"\[(source|doc)-[0-9]+\]",
                "deterministic": True,
            },
            {
                "type": "command_exit_zero",
                "argv": [
                    "python",
                    cli_path,
                    "/workspace/eval_seed/wm_python_cli_json_to_csv/input.json",
                    "/workspace/eval_outputs/wm_python_cli_json_to_csv/recheck.csv",
                ],
                "deterministic": True,
            },
        ],
    )

    result = await verify_outcome(request, io)

    assert result.passed is True
    assert not result.errors
    assert len(result.artifacts_found) >= 3


@pytest.mark.asyncio
async def test_verify_text_equals_reports_exact_mismatch() -> None:
    path = "/workspace/exact.txt"
    io = _FakeVerifierIO(files={path: b"DONE with extra text\n"})
    request = OutcomeVerificationRequest(
        task_id="exact_text",
        project_id="proj-1",
        success_criteria=[
            {
                "type": "text_equals",
                "path": path,
                "value": "DONE\n",
                "deterministic": True,
            }
        ],
    )

    result = await verify_outcome(request, io)

    assert result.passed is False
    assert result.artifacts_missing == [path]
    assert "text did not exactly match expected value" in result.errors


@pytest.mark.asyncio
async def test_verify_text_equals_allows_empty_expected_value() -> None:
    path = "/workspace/empty.txt"
    io = _FakeVerifierIO(files={path: b""})
    request = OutcomeVerificationRequest(
        task_id="empty_text",
        project_id="proj-1",
        success_criteria=[
            {
                "type": "text_equals",
                "path": path,
                "value": "",
                "deterministic": True,
            }
        ],
    )

    result = await verify_outcome(request, io)

    assert result.passed is True
    assert path in result.artifacts_found


@pytest.mark.asyncio
async def test_verify_directory_and_media_size_pass() -> None:
    directory_path = "/workspace/eval_outputs/wm_seeded_bugfix_with_tests/project"
    media_path = "/workspace/eval_outputs/wm_manim_animation_render/explainer.mp4"
    io = _FakeVerifierIO(
        files={
            f"{directory_path}/pytest.ini": b"[pytest]\n",
            media_path: b"x" * 12000,
        },
        directories={directory_path},
    )
    request = OutcomeVerificationRequest(
        task_id="wm_directory_media_checks",
        project_id="proj-1",
        success_criteria=[
            {"type": "directory_exists", "path": directory_path, "deterministic": True},
            {"type": "file_exists", "path": media_path, "deterministic": True},
            {
                "type": "file_size_at_least",
                "path": media_path,
                "minimum_bytes": 10240,
                "deterministic": True,
            },
        ],
    )

    result = await verify_outcome(request, io)

    assert result.passed is True
    assert not result.errors


@pytest.mark.asyncio
async def test_verify_reports_missing_artifact_and_failed_command() -> None:
    missing_path = "/workspace/eval_outputs/wm_missing/report.md"
    project_path = "/workspace/eval_outputs/wm_seeded_bugfix_with_tests/project"
    io = _FakeVerifierIO(
        files={},
        directories={project_path},
        commands={
            _CommandKey(("pytest", "-q"), project_path): CommandExecutionResult(
                exit_code=1,
                stdout="",
                stderr="boom",
            ),
        },
    )
    request = OutcomeVerificationRequest(
        task_id="wm_failure_checks",
        project_id="proj-1",
        success_criteria=[
            {"type": "file_exists", "path": missing_path, "deterministic": True},
            {
                "type": "command_exit_zero",
                "argv": ["pytest", "-q"],
                "cwd": project_path,
                "deterministic": True,
            },
        ],
    )

    result = await verify_outcome(request, io)

    assert result.passed is False
    assert missing_path in result.artifacts_missing
    assert any(check.passed is False for check in result.checks)
    assert result.errors
