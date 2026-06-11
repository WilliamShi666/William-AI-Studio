from __future__ import annotations

from agentscope_integration.shadow_clone_v2.facilitator import (
    build_dynamic_team_hint,
    plan_dynamic_subtasks,
)


def test_dynamic_team_hint_tells_main_agent_to_choose_roles_but_cap_at_ten() -> None:
    hint = build_dynamic_team_hint(max_subagents=10)

    assert "dynamically decide" in hint
    assert "roles" in hint
    assert "10" in hint
    assert "Do not create more than 10 subagents" in hint


def test_plan_dynamic_subtasks_splits_explicit_variant_requests_generically() -> None:
    tasks = plan_dynamic_subtasks(
        user_message=(
            "请为客户沟通材料做 3 个不同版本："
            "版本 A：高管摘要；版本 B：销售邮件；版本 C：风险说明。"
        )
    )

    assert [task["id"] for task in tasks] == [
        "deliverable_variant_a",
        "deliverable_variant_b",
        "deliverable_variant_c",
    ]
    assert all("distinct deliverable variant" in task["task_description"] for task in tasks)


import pytest

from agentscope_integration.shadow_clone_v2.facilitator import normalize_dynamic_team_plan
from agentscope_integration.shadow_clone_v2.models import AgentLifecycleStatus, TaskStatus


def test_normalize_dynamic_team_plan_creates_members_and_tasks_from_subtasks() -> None:
    team, tasks = normalize_dynamic_team_plan(
        run_id="run-1",
        thread_id="thread-1",
        project_id="project-1",
        subtasks=[
            {"id": "research", "role": "researcher", "task_description": "Find evidence."},
            {"id": "review", "role": "reviewer", "task_description": "Review the result."},
        ],
        created_at="2026-05-31T12:00:00Z",
    )

    assert team.team_id == "shadow-clone-v2:project-1:thread-1"
    assert [member.agent_name for member in team.members] == ["researcher", "reviewer"]
    assert all(member.status == AgentLifecycleStatus.STARTING for member in team.members)
    assert [task.id for task in tasks] == ["research", "review"]
    assert [task.status for task in tasks] == [TaskStatus.PENDING, TaskStatus.PENDING]
    assert tasks[0].thread_id == "thread-1"


def test_normalize_dynamic_team_plan_batches_more_than_ten_tasks_onto_ten_subagents() -> None:
    team, tasks = normalize_dynamic_team_plan(
        run_id="run-1",
        thread_id="thread-1",
        project_id="project-1",
        subtasks=[
            {"id": f"task-{index}", "role": f"role-{index}", "task_description": "Work."}
            for index in range(12)
        ],
        created_at="2026-05-31T12:00:00Z",
    )

    assert len(team.members) == 10
    assert len(tasks) == 12
    assert tasks[10].metadata["agent_name"] == team.members[0].agent_name
    assert tasks[11].metadata["agent_name"] == team.members[1].agent_name
