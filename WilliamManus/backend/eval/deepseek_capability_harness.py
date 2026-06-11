from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import PurePosixPath
import re
from typing import Any

_HELPER_SCRIPT_NAME_RE = re.compile(
    r"(^|/)(?:fix|repair|helper|debug|rewrite|patch|generate|generator|update|rename|modify|check)[^/]*\.(?:py|sed|sh|bash|txt)$",
    re.IGNORECASE,
)
_HELPER_SCRIPT_COMMAND_RE = re.compile(
    r"\b(?:python|python3|uv\s+run\s+python|bash|sh|sed\s+-f)\s+[^\n;&|]*"
    r"(?:fix|repair|helper|debug|rewrite|patch|generate|generator|update|rename|modify|check)[^/\s;&|]*\.(?:py|sed|sh|bash|txt)\b",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class CapabilityTask:
    task_id: str
    category: str
    prompt: str
    expected_tools: tuple[str, ...]
    success_criteria: tuple[dict[str, Any], ...] = ()
    model_family: str = "deepseek-v4-pro"
    requires_artifact_verification: bool = False
    max_duration_seconds: float | None = None


@dataclass(frozen=True)
class ToolCallRecord:
    name: str
    arguments: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class RunObservation:
    task_id: str
    attempt: int
    status: str
    tool_calls: tuple[ToolCallRecord, ...]
    duration_seconds: float | None = None
    artifact_verified: bool | None = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CapabilityGrade:
    task_id: str
    attempt: int
    passed: bool
    failed_criteria: tuple[str, ...]
    helper_script_violations: tuple[str, ...]
    metrics: dict[str, float | int | str | None]


def default_capability_tasks() -> tuple[CapabilityTask, ...]:
    return (
        CapabilityTask(
            task_id="direct_file_write_smoke",
            category="file_editing",
            prompt=(
                "Create /workspace/ds_smoke.txt containing exactly "
                "DEEPSEEK_ROYS_ALPHA_SMOKE_OK. Use file tools directly; no helper scripts."
            ),
            expected_tools=("write_file",),
            success_criteria=(
                {
                    "type": "text_equals",
                    "path": "/workspace/ds_smoke.txt",
                    "value": "DEEPSEEK_ROYS_ALPHA_SMOKE_OK",
                    "deterministic": True,
                },
            ),
            requires_artifact_verification=True,
        ),
        CapabilityTask(
            task_id="existing_file_edit_surgical",
            category="file_editing",
            prompt=(
                "Use the exact path /workspace/ds_edit_target.txt. "
                "Copy the path exactly; never write /workspace/deepseek_edit_target.txt, "
                "/workspace/depseek_edit_target.txt, or any other spelling. "
                "Create /workspace/ds_edit_target.txt with a small baseline, then "
                "use edit_file to surgically replace only the marker line with "
                "DEEPSEEK_SURGICAL_EDIT_OK. Verify the change; no helper scripts, "
                "no Python repair/update/rename scripts, and no execute_command for editing."
            ),
            expected_tools=("write_file", "edit_file"),
            success_criteria=(
                {
                    "type": "text_contains",
                    "path": "/workspace/ds_edit_target.txt",
                    "contains": ["DEEPSEEK_SURGICAL_EDIT_OK"],
                    "deterministic": True,
                },
            ),
            requires_artifact_verification=True,
        ),
        CapabilityTask(
            task_id="static_html_artifact",
            category="web_artifact",
            prompt=(
                "Create a minimal valid static HTML file at /workspace/ds_static.html. "
                "The write_file path must be exactly /workspace/ds_static.html. "
                "Use exactly one write_file call containing complete html, body, main, "
                "and section elements. Keep it under 1200 characters. Do not read the "
                "file, do not edit the file after write_file succeeds, do not execute "
                "commands, and after writing reply DONE. No helper scripts."
            ),
            expected_tools=("write_file",),
            success_criteria=(
                {
                    "type": "html_contains_selectors",
                    "path": "/workspace/ds_static.html",
                    "selectors": ["html", "body", "main", "section"],
                    "deterministic": True,
                },
            ),
            requires_artifact_verification=True,
        ),
        CapabilityTask(
            task_id="manim_skill_router",
            category="skill_routing",
            prompt=(
                "Load the Manim skill, then create a minimal Manim source file at "
                "/workspace/ds_manim_scene.py. The file only needs imports and one "
                "tiny Scene class; do not render anything, do not execute any "
                "commands, do not edit the file after write_file succeeds, and "
                "after writing reply DONE. No helper scripts."
            ),
            expected_tools=("load_skill", "write_file"),
            success_criteria=(
                {
                    "type": "text_contains",
                    "path": "/workspace/ds_manim_scene.py",
                    "contains": ["manim", "Scene"],
                    "deterministic": True,
                },
            ),
            requires_artifact_verification=True,
            max_duration_seconds=180.0,
        ),
        CapabilityTask(
            task_id="ppt_skill_router",
            category="skill_routing",
            prompt=(
                "Load the PPT skill, then create a minimal presentation outline at "
                "/workspace/ds_ppt_outline.md. The file only needs a title and "
                "three short bullet points; do not run any export or render "
                "commands, do not edit the file after write_file succeeds, and "
                "after writing reply DONE. No helper scripts."
            ),
            expected_tools=("load_skill", "write_file"),
            success_criteria=(
                {
                    "type": "text_contains",
                    "path": "/workspace/ds_ppt_outline.md",
                    "contains": ["#"],
                    "deterministic": True,
                },
            ),
            requires_artifact_verification=True,
            max_duration_seconds=120.0,
        ),
    )


def real_artifact_capability_tasks() -> tuple[CapabilityTask, ...]:
    return (
        CapabilityTask(
            task_id="real_manim_mp4_render",
            category="real_artifact",
            prompt=(
                "Load the Manim skill. Create a short, real Manim scene source file at "
                "/workspace/ds_real_manim_scene.py, render it at low quality, and copy "
                "the final MP4 artifact to exactly /workspace/ds_real_manim.mp4. Keep the "
                "animation simple, deterministic, and under ten seconds: use Text(), "
                "Circle/Square/Arrow, Create/FadeIn/Transform/FadeOut, and avoid MathTex "
                "or Tex so LaTeX cannot block rendering. Use write_file only to create "
                "the scene file, execute_command for rendering/copying, and edit_file "
                "only for surgical fixes if rendering fails. If the scene fails, first "
                "use read_file on the exact scene path, then edit_file with exact "
                "old_text/new_text. Use sed -i only if edit_file fails, and only as a "
                "one-shot non-no-op fallback that immediately reruns the Manim command "
                "or checks the replacement result. Never delete or rewrite the scene "
                "after it exists. Do not create fix/repair/helper/debug/rewrite scripts, "
                "including fix_text.py, fix_*.py, check_*.py, fix*.sed, helper scripts, "
                "sed patch files, or temporary repair/diagnostic scripts. Stop after "
                "/workspace/ds_real_manim.mp4 exists and has been verified."
            ),
            expected_tools=("load_skill", "write_file", "execute_command"),
            success_criteria=(
                {
                    "type": "file_exists",
                    "path": "/workspace/ds_real_manim_scene.py",
                    "deterministic": True,
                },
                {
                    "type": "file_size_at_least",
                    "path": "/workspace/ds_real_manim.mp4",
                    "minimum_bytes": 10240,
                    "deterministic": True,
                },
                {
                    "type": "text_contains",
                    "path": "/workspace/ds_real_manim_scene.py",
                    "contains": ["from manim import", "Scene"],
                    "deterministic": True,
                },
            ),
            requires_artifact_verification=True,
            max_duration_seconds=1200.0,
        ),
        CapabilityTask(
            task_id="real_pptx_deck_generation",
            category="real_artifact",
            prompt=(
                "Load the PPT skill. Create a real PowerPoint deck at exactly "
                "/workspace/ds_real_deck.pptx with at least three slides about "
                "DeepSeek v4 Pro improvements for Roys Alpha. First write a concise "
                "markdown outline at /workspace/ds_real_deck_outline.md. Do not call "
                "load_reference and do not read references; use this prompt as the "
                "complete task spec. Then write /workspace/ds_real_deck_builder.py as "
                "a self-contained python-pptx builder script and execute it with "
                "python3 to create the PPTX. You must call execute_command with "
                "python3 /workspace/ds_real_deck_builder.py after the builder file is "
                "written; merely writing the builder is not completion. If python-pptx "
                "is missing, install or use the available PPTX library directly in the "
                "command step. Do not create fix/repair/helper/debug/rewrite scripts. "
                "Keep the builder intentionally simple and under 180 lines: use only "
                "Presentation(), blank slides, background rectangles, and simple title "
                "and bullet text boxes. No charts, no custom XML, no custom bullet XML, "
                "no complex table/chart APIs, and no long helper framework inside the "
                "builder; three to five straightforward slides is enough. "
                "If the builder fails, first use read_file on the exact builder path, "
                "then use edit_file with exact old_text/new_text for a surgical fix. "
                "Use sed -i only if edit_file fails, and only as a one-shot fallback "
                "that immediately reruns python3 /workspace/ds_real_deck_builder.py "
                "or checks the replacement result. Never delete or rewrite the builder "
                "after it exists. If shell quoting is hard, do not write fix_quotes.py, "
                "fix_*.py, check_*.py, fix*.sed, helper scripts, sed patch files, "
                "or temporary repair/diagnostic scripts; return to read_file plus "
                "edit_file, or use one correct direct non-no-op sed -i command. "
                "Do not stop until /workspace/ds_real_deck.pptx exists and has been "
                "verified with an execute_command check."
            ),
            expected_tools=("load_skill", "write_file", "execute_command"),
            success_criteria=(
                {
                    "type": "file_exists",
                    "path": "/workspace/ds_real_deck.pptx",
                    "deterministic": True,
                },
                {
                    "type": "zip_entry_exists",
                    "path": "/workspace/ds_real_deck.pptx",
                    "entry": "[Content_Types].xml",
                    "deterministic": True,
                },
                {
                    "type": "zip_entry_count_at_least",
                    "path": "/workspace/ds_real_deck.pptx",
                    "prefix": "ppt/slides/slide",
                    "minimum": 3,
                    "deterministic": True,
                },
            ),
            requires_artifact_verification=True,
            max_duration_seconds=1200.0,
        ),
    )


def _tool_argument_text(record: ToolCallRecord, key: str) -> str:
    value = record.arguments.get(key)
    return str(value or "").strip()


def _normalize_path(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    return str(PurePosixPath(raw))


def detect_helper_script_violations(
    tool_calls: tuple[ToolCallRecord, ...],
) -> tuple[str, ...]:
    violations: list[str] = []
    for record in tool_calls:
        tool_name = str(record.name or "").strip()
        if tool_name in {"write_file", "edit_file"}:
            path = _normalize_path(_tool_argument_text(record, "path"))
            if path and _HELPER_SCRIPT_NAME_RE.search(path):
                violations.append(f"{tool_name} created helper script: {path}")
        if tool_name == "execute_command":
            command = _tool_argument_text(record, "command")
            if command and _HELPER_SCRIPT_COMMAND_RE.search(command):
                violations.append(f"execute_command ran helper script: {command}")
    return tuple(violations)


def _expected_tool_hit_rate(
    *,
    expected_tools: tuple[str, ...],
    tool_calls: tuple[ToolCallRecord, ...],
) -> float:
    if not expected_tools:
        return 1.0
    called_tools = {str(record.name or "").strip() for record in tool_calls}
    hits = sum(1 for tool_name in expected_tools if tool_name in called_tools)
    return round(hits / len(expected_tools), 6)


def grade_observation(
    task: CapabilityTask,
    observation: RunObservation,
) -> CapabilityGrade:
    helper_script_violations = detect_helper_script_violations(observation.tool_calls)
    expected_tool_hit_rate = _expected_tool_hit_rate(
        expected_tools=task.expected_tools,
        tool_calls=observation.tool_calls,
    )
    normalized_status = str(observation.status or "").strip().lower()
    failed: list[str] = []

    artifact_success_terminal_status = (
        task.requires_artifact_verification
        and observation.artifact_verified is True
        and normalized_status == "stopped"
    )

    if normalized_status != "completed" and not artifact_success_terminal_status:
        failed.append("status_completed")
    if expected_tool_hit_rate < 1.0:
        failed.append("expected_tools")
    if helper_script_violations:
        failed.append("no_helper_scripts")
    if (
        task.requires_artifact_verification
        and observation.artifact_verified is not True
    ):
        failed.append("artifact_verified")
    if (
        task.max_duration_seconds is not None
        and observation.duration_seconds is not None
        and observation.duration_seconds > task.max_duration_seconds
    ):
        failed.append("max_duration_seconds")

    return CapabilityGrade(
        task_id=task.task_id,
        attempt=observation.attempt,
        passed=not failed,
        failed_criteria=tuple(failed),
        helper_script_violations=helper_script_violations,
        metrics={
            "status": normalized_status,
            "expected_tool_hit_rate": expected_tool_hit_rate,
            "tool_call_count": len(observation.tool_calls),
            "duration_seconds": observation.duration_seconds,
            "helper_script_violation_count": len(helper_script_violations),
        },
    )


def summarize_grades(
    tasks: tuple[CapabilityTask, ...],
    grades: tuple[CapabilityGrade, ...],
    *,
    max_k: int = 3,
) -> dict[str, float | int]:
    task_ids = tuple(task.task_id for task in tasks)
    grades_by_task: dict[str, list[CapabilityGrade]] = {
        task_id: [] for task_id in task_ids
    }
    for grade in grades:
        if grade.task_id in grades_by_task:
            grades_by_task[grade.task_id].append(grade)

    summary: dict[str, float | int] = {
        "task_count": len(tasks),
        "attempt_count": len(grades),
        "passed_attempt_count": sum(1 for grade in grades if grade.passed),
        "helper_script_violation_count": sum(
            int(grade.metrics.get("helper_script_violation_count") or 0)
            for grade in grades
        ),
    }

    denominator = max(len(tasks), 1)
    for k in range(1, max_k + 1):
        task_success_count = 0
        for task_id in task_ids:
            first_k = sorted(grades_by_task[task_id], key=lambda item: item.attempt)[:k]
            if any(grade.passed for grade in first_k):
                task_success_count += 1
        summary[f"pass@{k}"] = round(task_success_count / denominator, 6)

    return summary


def _task_to_dict(task: CapabilityTask) -> dict[str, Any]:
    payload = asdict(task)
    payload["expected_tools"] = list(task.expected_tools)
    payload["success_criteria"] = [dict(item) for item in task.success_criteria]
    return payload


def _grade_to_dict(grade: CapabilityGrade) -> dict[str, Any]:
    payload = asdict(grade)
    payload["failed_criteria"] = list(grade.failed_criteria)
    payload["helper_script_violations"] = list(grade.helper_script_violations)
    return payload


def _observation_to_dict(observation: RunObservation) -> dict[str, Any]:
    payload = asdict(observation)
    payload["tool_calls"] = [
        {
            "name": tool_call.name,
            "arguments": dict(tool_call.arguments),
        }
        for tool_call in observation.tool_calls
    ]
    return payload


def build_capability_report(
    *,
    tasks: tuple[CapabilityTask, ...],
    observations: tuple[RunObservation, ...],
    model_name: str,
    generated_at: str,
    max_k: int = 3,
) -> dict[str, Any]:
    tasks_by_id = {task.task_id: task for task in tasks}
    grades = tuple(
        grade_observation(tasks_by_id[observation.task_id], observation)
        for observation in observations
        if observation.task_id in tasks_by_id
    )
    return {
        "schema_version": 1,
        "generated_at": str(generated_at),
        "model_name": str(model_name),
        "summary": summarize_grades(tasks, grades, max_k=max_k),
        "tasks": [_task_to_dict(task) for task in tasks],
        "observations": [
            _observation_to_dict(observation) for observation in observations
        ],
        "grades": [_grade_to_dict(grade) for grade in grades],
    }
