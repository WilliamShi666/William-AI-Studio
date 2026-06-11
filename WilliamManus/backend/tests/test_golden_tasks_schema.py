from __future__ import annotations

import json
from pathlib import Path


CATALOG_PATH = Path(__file__).resolve().parents[1] / "eval" / "golden_tasks.json"

REQUIRED_FIELDS = {
    "task_id",
    "title",
    "task_family",
    "task_type",
    "complexity",
    "prompt",
    "required_sandbox_type",
    "resume_strategy",
    "runtime_mode_flags",
    "seed_inputs",
    "expected_outputs",
    "success_criteria",
    "timeout_seconds",
    "tags",
}

MANDATORY_TASK_FAMILIES = {
    "word_docx_grading",
    "html_courseware_generation",
    "html_to_ppt_conversion",
    "manim_animation_render",
}

ALLOWED_TASK_TYPES = {
    "animation_generation",
    "browser_automation",
    "bugfix",
    "code_generation",
    "data_reporting",
    "data_visualization",
    "document_generation",
    "document_processing",
    "frontend_generation",
    "multi_step_workflow",
    "packaging",
    "research_synthesis",
}

ALLOWED_COMPLEXITIES = {"simple", "medium", "complex"}
ALLOWED_SANDBOX_TYPES = {"browser", "code", "desktop"}
ALLOWED_RESUME_STRATEGIES = {"fresh"}
ALLOWED_CRITERION_TYPES = {
    "command_exit_zero",
    "csv_header_contains",
    "csv_row_count_at_least",
    "directory_exists",
    "file_exists",
    "file_size_at_least",
    "html_contains_links",
    "html_contains_selectors",
    "html_contains_text",
    "json_has_keys",
    "text_contains",
    "text_matches_regex",
    "zip_entry_contains",
    "zip_entry_count_at_least",
    "zip_entry_exists",
}


def _assert_named_path_items(items: list[dict], *, task_id: str, field_name: str) -> None:
    for index, item in enumerate(items):
        assert isinstance(item, dict), f"task {task_id} {field_name}[{index}] must be an object"
        assert isinstance(item.get("path"), str) and item["path"].strip(), (
            f"task {task_id} {field_name}[{index}] missing non-empty path"
        )
        assert isinstance(item.get("kind"), str) and item["kind"].strip(), (
            f"task {task_id} {field_name}[{index}] missing non-empty kind"
        )


def _assert_success_criteria(criteria: list[dict], *, task_id: str) -> None:
    for index, criterion in enumerate(criteria):
        assert isinstance(criterion, dict), (
            f"task {task_id} success_criteria[{index}] must be an object"
        )
        criterion_type = criterion.get("type")
        assert criterion_type in ALLOWED_CRITERION_TYPES, (
            f"task {task_id} success_criteria[{index}] has unsupported type: {criterion_type!r}"
        )
        assert isinstance(criterion.get("deterministic"), bool), (
            f"task {task_id} success_criteria[{index}] must declare deterministic as bool"
        )

        if criterion_type == "command_exit_zero":
            argv = criterion.get("argv")
            assert isinstance(argv, list) and argv, (
                f"task {task_id} success_criteria[{index}] command_exit_zero must declare non-empty argv"
            )
            assert all(isinstance(arg, str) and arg.strip() for arg in argv), (
                f"task {task_id} success_criteria[{index}] command_exit_zero argv items must be non-empty strings"
            )
            if "cwd" in criterion:
                assert isinstance(criterion["cwd"], str) and criterion["cwd"].strip(), (
                    f"task {task_id} success_criteria[{index}] cwd must be a non-empty string"
                )
            assert "command" not in criterion, (
                f"task {task_id} success_criteria[{index}] must use argv/cwd instead of ambiguous command"
            )

        elif criterion_type in {
            "file_exists",
            "directory_exists",
            "file_size_at_least",
            "html_contains_links",
            "html_contains_selectors",
            "html_contains_text",
            "json_has_keys",
            "text_contains",
            "text_matches_regex",
            "csv_header_contains",
            "csv_row_count_at_least",
            "zip_entry_exists",
            "zip_entry_contains",
            "zip_entry_count_at_least",
        }:
            assert isinstance(criterion.get("path"), str) and criterion["path"].strip(), (
                f"task {task_id} success_criteria[{index}] missing non-empty path"
            )

        if criterion_type in {"zip_entry_exists", "zip_entry_contains"}:
            assert isinstance(criterion.get("entry"), str) and criterion["entry"].strip(), (
                f"task {task_id} success_criteria[{index}] missing non-empty entry"
            )

        if criterion_type in {"zip_entry_contains", "text_contains", "html_contains_text", "csv_header_contains"}:
            contains = criterion.get("contains")
            assert isinstance(contains, list) and contains, (
                f"task {task_id} success_criteria[{index}] must declare non-empty contains"
            )
            assert all(isinstance(item, str) and item.strip() for item in contains), (
                f"task {task_id} success_criteria[{index}] contains items must be non-empty strings"
            )

        if criterion_type == "html_contains_selectors":
            selectors = criterion.get("selectors")
            assert isinstance(selectors, list) and selectors, (
                f"task {task_id} success_criteria[{index}] must declare non-empty selectors"
            )
            assert all(isinstance(item, str) and item.strip() for item in selectors), (
                f"task {task_id} success_criteria[{index}] selectors items must be non-empty strings"
            )

        if criterion_type == "json_has_keys":
            keys = criterion.get("keys")
            assert isinstance(keys, list) and keys, (
                f"task {task_id} success_criteria[{index}] must declare non-empty keys"
            )
            assert all(isinstance(item, str) and item.strip() for item in keys), (
                f"task {task_id} success_criteria[{index}] keys items must be non-empty strings"
            )

        if criterion_type == "text_matches_regex":
            assert isinstance(criterion.get("pattern"), str) and criterion["pattern"].strip(), (
                f"task {task_id} success_criteria[{index}] missing non-empty pattern"
            )

        if criterion_type == "html_contains_links":
            assert isinstance(criterion.get("minimum_links"), int) and criterion["minimum_links"] > 0, (
                f"task {task_id} success_criteria[{index}] minimum_links must be a positive int"
            )

        if criterion_type == "csv_row_count_at_least":
            assert isinstance(criterion.get("minimum_rows"), int) and criterion["minimum_rows"] > 0, (
                f"task {task_id} success_criteria[{index}] minimum_rows must be a positive int"
            )

        if criterion_type == "file_size_at_least":
            assert isinstance(criterion.get("minimum_bytes"), int) and criterion["minimum_bytes"] > 0, (
                f"task {task_id} success_criteria[{index}] minimum_bytes must be a positive int"
            )

        if criterion_type == "zip_entry_count_at_least":
            assert isinstance(criterion.get("prefix"), str) and criterion["prefix"].strip(), (
                f"task {task_id} success_criteria[{index}] missing non-empty prefix"
            )
            assert isinstance(criterion.get("minimum"), int) and criterion["minimum"] > 0, (
                f"task {task_id} success_criteria[{index}] minimum must be a positive int"
            )


def _load_catalog() -> list[dict]:
    assert CATALOG_PATH.exists(), f"missing catalog file: {CATALOG_PATH}"
    data = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    assert isinstance(data, list), "golden task catalog must be a list"
    return data


def test_golden_catalog_has_exactly_20_tasks() -> None:
    tasks = _load_catalog()
    assert len(tasks) == 20


def test_golden_catalog_task_ids_are_unique() -> None:
    tasks = _load_catalog()
    task_ids = [task.get("task_id") for task in tasks]
    assert len(task_ids) == len(set(task_ids))


def test_golden_catalog_required_fields_are_present() -> None:
    tasks = _load_catalog()
    for task in tasks:
        missing = REQUIRED_FIELDS - task.keys()
        assert not missing, f"task {task.get('task_id')} missing fields: {sorted(missing)}"
        assert isinstance(task["task_id"], str) and task["task_id"].strip()
        assert isinstance(task["title"], str) and task["title"].strip()
        assert isinstance(task["task_family"], str) and task["task_family"].strip()
        assert task["task_type"] in ALLOWED_TASK_TYPES
        assert task["complexity"] in ALLOWED_COMPLEXITIES
        assert isinstance(task["prompt"], str) and task["prompt"].strip()
        assert isinstance(task["seed_inputs"], list) and task["seed_inputs"]
        assert isinstance(task["expected_outputs"], list) and task["expected_outputs"]
        assert isinstance(task["success_criteria"], list) and task["success_criteria"]
        assert isinstance(task["runtime_mode_flags"], dict)
        assert isinstance(task["timeout_seconds"], int) and task["timeout_seconds"] > 0
        assert task["required_sandbox_type"] in ALLOWED_SANDBOX_TYPES
        assert task["resume_strategy"] in ALLOWED_RESUME_STRATEGIES
        assert isinstance(task["tags"], list) and task["tags"]
        assert all(isinstance(tag, str) and tag.strip() for tag in task["tags"])

        _assert_named_path_items(task["seed_inputs"], task_id=task["task_id"], field_name="seed_inputs")
        _assert_named_path_items(
            task["expected_outputs"],
            task_id=task["task_id"],
            field_name="expected_outputs",
        )
        _assert_success_criteria(task["success_criteria"], task_id=task["task_id"])


def test_golden_catalog_contains_four_mandatory_task_families() -> None:
    tasks = _load_catalog()
    task_families = {task["task_family"] for task in tasks}
    assert MANDATORY_TASK_FAMILIES <= task_families


def test_golden_catalog_declares_required_sandbox_type_for_all_tasks() -> None:
    tasks = _load_catalog()
    for task in tasks:
        required_sandbox_type = task.get("required_sandbox_type")
        assert isinstance(required_sandbox_type, str) and required_sandbox_type.strip()


def test_golden_catalog_has_deterministic_success_criteria_for_all_tasks() -> None:
    tasks = _load_catalog()
    for task in tasks:
        deterministic_criteria = [
            criterion
            for criterion in task["success_criteria"]
            if isinstance(criterion, dict) and criterion.get("deterministic") is True
        ]
        assert (
            len(deterministic_criteria) >= 1
        ), f"task {task['task_id']} must include at least one deterministic criterion"


def test_golden_catalog_is_first_release_regular_mode_only() -> None:
    tasks = _load_catalog()
    for task in tasks:
        runtime_mode_flags = task["runtime_mode_flags"]
        assert runtime_mode_flags.get("execution_mode") == "regular"
        assert runtime_mode_flags.get("shadow_clone_mode") == "off"
        assert runtime_mode_flags.get("sandbox_bootstrap") == "cold_start"
