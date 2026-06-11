from __future__ import annotations

import json

from eval.deepseek_capability_harness import (
    CapabilityTask,
    RunObservation,
    ToolCallRecord,
    build_capability_report,
    default_capability_tasks,
    detect_helper_script_violations,
    grade_observation,
    real_artifact_capability_tasks,
    summarize_grades,
)


def test_default_capability_tasks_cover_core_deepseek_regressions() -> None:
    tasks = default_capability_tasks()
    task_ids = {task.task_id for task in tasks}
    tasks_by_id = {task.task_id: task for task in tasks}

    assert "direct_file_write_smoke" in task_ids
    assert "existing_file_edit_surgical" in task_ids
    assert "static_html_artifact" in task_ids
    assert "manim_skill_router" in task_ids
    assert "ppt_skill_router" in task_ids
    assert all(task.model_family == "deepseek-v4-pro" for task in tasks)
    assert all("helper script" in task.prompt.lower() for task in tasks)
    assert tasks_by_id["direct_file_write_smoke"].success_criteria == (
        {
            "type": "text_equals",
            "path": "/workspace/ds_smoke.txt",
            "value": "DEEPSEEK_ROYS_ALPHA_SMOKE_OK",
            "deterministic": True,
        },
    )
    assert any(
        criterion["type"] == "html_contains_selectors"
        for criterion in tasks_by_id["static_html_artifact"].success_criteria
    )
    surgical_prompt = tasks_by_id["existing_file_edit_surgical"].prompt
    assert "/workspace/ds_edit_target.txt" in surgical_prompt
    assert "copy the path exactly" in surgical_prompt.lower()
    assert "deepseek_edit_target" in surgical_prompt.lower()
    html_prompt = tasks_by_id["static_html_artifact"].prompt
    assert "write_file path" in html_prompt
    assert "/workspace/ds_static.html" in html_prompt
    assert "minimal" in html_prompt.lower()
    assert "exactly one write_file" in html_prompt.lower()
    assert "do not read" in html_prompt.lower()
    assert "do not edit" in html_prompt.lower()
    assert "do not execute" in html_prompt.lower()
    assert "done" in html_prompt.lower()

    manim_task = tasks_by_id["manim_skill_router"]
    manim_prompt = manim_task.prompt.lower()
    assert "/workspace/ds_manim_scene.py" in manim_task.prompt
    assert "/workspace/deepseek_manim_scene.py" not in manim_task.prompt
    assert manim_task.expected_tools == ("load_skill", "write_file")
    assert "minimal" in manim_prompt
    assert "do not render" in manim_prompt
    assert "do not execute" in manim_prompt
    assert "do not edit" in manim_prompt
    assert "after writing" in manim_prompt
    assert "done" in manim_prompt
    assert any(
        criterion.get("path") == "/workspace/ds_manim_scene.py"
        for criterion in manim_task.success_criteria
    )
    assert manim_task.max_duration_seconds is not None

    ppt_task = tasks_by_id["ppt_skill_router"]
    ppt_prompt = ppt_task.prompt.lower()
    assert "/workspace/ds_ppt_outline.md" in ppt_task.prompt
    assert "/workspace/deepseek_presentation_outline.md" not in ppt_task.prompt
    assert ppt_task.expected_tools == ("load_skill", "write_file")
    assert "minimal" in ppt_prompt
    assert "do not run" in ppt_prompt
    assert "do not edit" in ppt_prompt
    assert "after writing" in ppt_prompt
    assert "done" in ppt_prompt
    assert any(
        criterion.get("path") == "/workspace/ds_ppt_outline.md"
        for criterion in ppt_task.success_criteria
    )
    assert ppt_task.max_duration_seconds is not None


def test_real_artifact_capability_tasks_cover_manim_mp4_and_pptx_deck() -> None:
    tasks = real_artifact_capability_tasks()
    tasks_by_id = {task.task_id: task for task in tasks}

    assert set(tasks_by_id) == {
        "real_manim_mp4_render",
        "real_pptx_deck_generation",
    }

    manim_task = tasks_by_id["real_manim_mp4_render"]
    assert manim_task.category == "real_artifact"
    assert manim_task.expected_tools == ("load_skill", "write_file", "execute_command")
    assert manim_task.requires_artifact_verification is True
    assert manim_task.max_duration_seconds is not None
    assert manim_task.max_duration_seconds >= 900
    assert "/workspace/ds_real_manim_scene.py" in manim_task.prompt
    assert "/workspace/ds_real_manim.mp4" in manim_task.prompt
    assert "render" in manim_task.prompt.lower()
    assert "copy" in manim_task.prompt.lower()
    assert "text(" in manim_task.prompt.lower()
    assert "avoid mathtex" in manim_task.prompt.lower()
    assert "read_file" in manim_task.prompt
    assert "edit_file" in manim_task.prompt
    assert "sed -i" in manim_task.prompt
    assert "never delete or rewrite" in manim_task.prompt.lower()
    assert "fix_text.py" in manim_task.prompt
    assert "stable scene class named" not in manim_task.prompt.lower()
    assert any(
        criterion.get("type") == "file_size_at_least"
        and criterion.get("path") == "/workspace/ds_real_manim.mp4"
        and int(criterion.get("minimum_bytes") or 0) >= 10240
        for criterion in manim_task.success_criteria
    )
    assert any(
        criterion.get("type") == "text_contains"
        and criterion.get("path") == "/workspace/ds_real_manim_scene.py"
        and criterion.get("contains") == ["from manim import", "Scene"]
        for criterion in manim_task.success_criteria
    )

    ppt_task = tasks_by_id["real_pptx_deck_generation"]
    assert ppt_task.category == "real_artifact"
    assert ppt_task.expected_tools == ("load_skill", "write_file", "execute_command")
    assert ppt_task.requires_artifact_verification is True
    assert ppt_task.max_duration_seconds is not None
    assert ppt_task.max_duration_seconds >= 900
    assert "/workspace/ds_real_deck.pptx" in ppt_task.prompt
    assert "markdown" in ppt_task.prompt.lower()
    assert "do not call load_reference" in ppt_task.prompt.lower()
    assert "do not read references" in ppt_task.prompt.lower()
    assert "/workspace/ds_real_deck_builder.py" in ppt_task.prompt
    assert "python-pptx" in ppt_task.prompt.lower()
    assert "must call execute_command" in ppt_task.prompt.lower()
    assert "python3 /workspace/ds_real_deck_builder.py" in ppt_task.prompt
    assert "do not stop" in ppt_task.prompt.lower()
    assert "read_file" in ppt_task.prompt
    assert "edit_file" in ppt_task.prompt
    assert "sed -i" in ppt_task.prompt
    assert "only if edit_file fails" in ppt_task.prompt.lower()
    assert "fix_quotes.py" in ppt_task.prompt
    assert "fix*.sed" in ppt_task.prompt
    assert "check_*.py" in ppt_task.prompt
    assert "do not write" in ppt_task.prompt.lower()
    assert "no-op sed" in ppt_task.prompt.lower()
    assert "under 180 lines" in ppt_task.prompt.lower()
    assert "no charts" in ppt_task.prompt.lower()
    assert "no custom xml" in ppt_task.prompt.lower()
    assert "simple title and bullet text boxes" in ppt_task.prompt.lower()
    assert any(
        criterion.get("type") == "zip_entry_count_at_least"
        and criterion.get("path") == "/workspace/ds_real_deck.pptx"
        and criterion.get("prefix") == "ppt/slides/slide"
        and int(criterion.get("minimum") or 0) >= 3
        for criterion in ppt_task.success_criteria
    )


def test_detect_helper_script_violations_finds_helper_file_creation_and_execution() -> (
    None
):
    tool_calls = (
        ToolCallRecord(
            name="write_file",
            arguments={"path": "fix_line110.py", "content": "print('patch')"},
        ),
        ToolCallRecord(
            name="execute_command",
            arguments={"command": "python3 /workspace/helper_rewrite.py"},
        ),
        ToolCallRecord(
            name="write_file",
            arguments={"path": "final.txt", "content": "ok"},
        ),
    )

    violations = detect_helper_script_violations(tool_calls)

    assert violations == (
        "write_file created helper script: fix_line110.py",
        "execute_command ran helper script: python3 /workspace/helper_rewrite.py",
    )


def test_detect_helper_script_violations_blocks_python_helper_stems_without_keyword() -> (
    None
):
    tool_calls = (
        ToolCallRecord(
            name="write_file",
            arguments={"path": "/workspace/rename.py", "content": "..."},
        ),
        ToolCallRecord(
            name="execute_command",
            arguments={"command": "python /workspace/update_file.py"},
        ),
    )

    violations = detect_helper_script_violations(tool_calls)

    assert violations == (
        "write_file created helper script: /workspace/rename.py",
        "execute_command ran helper script: python /workspace/update_file.py",
    )




def test_detect_helper_script_violations_blocks_sed_and_check_files() -> None:
    tool_calls = (
        ToolCallRecord(
            name="write_file",
            arguments={"path": "/workspace/fix198.sed", "content": "s/a/b/"},
        ),
        ToolCallRecord(
            name="write_file",
            arguments={"path": "/workspace/check_lines.py", "content": "print('x')"},
        ),
        ToolCallRecord(
            name="execute_command",
            arguments={"command": "sed -f /workspace/fix198.sed target.py"},
        ),
    )

    violations = detect_helper_script_violations(tool_calls)

    assert violations == (
        "write_file created helper script: /workspace/fix198.sed",
        "write_file created helper script: /workspace/check_lines.py",
        "execute_command ran helper script: sed -f /workspace/fix198.sed target.py",
    )


def test_grade_observation_rewards_direct_file_tool_and_blocks_helper_scripts() -> None:
    task = CapabilityTask(
        task_id="direct-file",
        category="file_editing",
        prompt="Create a file directly. No helper scripts.",
        expected_tools=("write_file",),
        requires_artifact_verification=True,
    )
    passing_observation = RunObservation(
        task_id="direct-file",
        attempt=1,
        status="completed",
        duration_seconds=12.5,
        tool_calls=(
            ToolCallRecord(
                name="write_file",
                arguments={"path": "answer.txt", "content": "OK"},
            ),
        ),
        artifact_verified=True,
    )
    failing_observation = RunObservation(
        task_id="direct-file",
        attempt=1,
        status="completed",
        duration_seconds=12.5,
        tool_calls=(
            ToolCallRecord(
                name="write_file",
                arguments={"path": "fix_answer.py", "content": "print('OK')"},
            ),
        ),
        artifact_verified=True,
    )

    passing_grade = grade_observation(task, passing_observation)
    failing_grade = grade_observation(task, failing_observation)

    assert passing_grade.passed is True
    assert passing_grade.metrics["expected_tool_hit_rate"] == 1.0
    assert failing_grade.passed is False
    assert "no_helper_scripts" in failing_grade.failed_criteria


def test_grade_observation_accepts_verified_artifact_from_stopped_run() -> None:
    """If the real artifact is verified, a stopped terminal status should not fail it."""
    task = CapabilityTask(
        task_id="real-artifact",
        category="real_artifact",
        prompt="Create a real artifact.",
        expected_tools=("write_file", "execute_command"),
        requires_artifact_verification=True,
    )
    observation = RunObservation(
        task_id="real-artifact",
        attempt=1,
        status="stopped",
        duration_seconds=120.0,
        tool_calls=(
            ToolCallRecord(name="write_file", arguments={"path": "/workspace/a.py"}),
            ToolCallRecord(
                name="execute_command",
                arguments={"command": "python3 /workspace/a.py"},
            ),
        ),
        artifact_verified=True,
    )

    grade = grade_observation(task, observation)

    assert grade.passed is True
    assert "status_completed" not in grade.failed_criteria


def test_summarize_grades_computes_pass_at_k_and_helper_script_totals() -> None:
    tasks = (
        CapabilityTask(
            task_id="task-a",
            category="file_editing",
            prompt="Task A. No helper scripts.",
            expected_tools=("write_file",),
        ),
        CapabilityTask(
            task_id="task-b",
            category="file_editing",
            prompt="Task B. No helper scripts.",
            expected_tools=("edit_file",),
        ),
    )
    grades = (
        grade_observation(
            tasks[0],
            RunObservation(
                task_id="task-a",
                attempt=1,
                status="completed",
                tool_calls=(ToolCallRecord(name="write_file", arguments={}),),
            ),
        ),
        grade_observation(
            tasks[1],
            RunObservation(
                task_id="task-b",
                attempt=1,
                status="failed",
                tool_calls=(
                    ToolCallRecord(
                        name="write_file",
                        arguments={"path": "repair_task.py", "content": "..."},
                    ),
                ),
            ),
        ),
        grade_observation(
            tasks[1],
            RunObservation(
                task_id="task-b",
                attempt=2,
                status="completed",
                tool_calls=(ToolCallRecord(name="edit_file", arguments={}),),
            ),
        ),
    )

    summary = summarize_grades(tasks, grades, max_k=3)

    assert summary["task_count"] == 2
    assert summary["attempt_count"] == 3
    assert summary["pass@1"] == 0.5
    assert summary["pass@2"] == 1.0
    assert summary["pass@3"] == 1.0
    assert summary["helper_script_violation_count"] == 1


def test_build_capability_report_is_json_serializable_and_schema_stable() -> None:
    tasks = default_capability_tasks()[:1]
    observations = (
        RunObservation(
            task_id=tasks[0].task_id,
            attempt=1,
            status="completed",
            duration_seconds=21.5,
            tool_calls=(ToolCallRecord(name="write_file", arguments={}),),
            artifact_verified=True,
        ),
    )

    report = build_capability_report(
        tasks=tasks,
        observations=observations,
        model_name="deepseek-v4-pro-high",
        generated_at="2026-05-17T00:00:00Z",
    )

    assert report["schema_version"] == 1
    assert report["model_name"] == "deepseek-v4-pro-high"
    assert report["summary"]["pass@1"] == 1.0
    assert report["tasks"][0]["task_id"] == tasks[0].task_id
    assert "success_criteria" in report["tasks"][0]
    assert report["observations"][0]["task_id"] == tasks[0].task_id
    assert report["observations"][0]["artifact_verified"] is True
    assert report["observations"][0]["tool_calls"][0]["name"] == "write_file"
    assert report["grades"][0]["passed"] is True
    json.dumps(report)
