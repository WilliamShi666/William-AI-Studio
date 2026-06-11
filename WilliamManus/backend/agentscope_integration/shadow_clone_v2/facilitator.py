"""Facilitator helpers for Shadow Clone V2."""

from __future__ import annotations

from typing import Any, Sequence

from .constants import SHADOW_CLONE_V2_MAX_SUBAGENTS_PER_RUN
from .models import AgentIdentity, Task, TeamConfig
from .policy import validate_run_subagent_capacity


def build_dynamic_team_hint(
    *,
    max_subagents: int = SHADOW_CLONE_V2_MAX_SUBAGENTS_PER_RUN,
) -> str:
    """Build the main-agent instruction for dynamic subagent creation."""
    effective_cap = min(max(0, int(max_subagents)), SHADOW_CLONE_V2_MAX_SUBAGENTS_PER_RUN)
    return (
        "Shadow Clone V2 team planning: dynamically decide how many subagents "
        "are needed for this user task and which roles they should have. "
        f"Do not create more than {effective_cap} subagents in one run. "
        "Reused idle subagents count toward the same cap as newly created subagents. "
        "Prefer the smallest team that can complete the task well."
    )


def _safe_text(value: Any, fallback: str) -> str:
    text = str(value or "").strip()
    return text if text else fallback


def _agent_name_for_role(role: str, used_names: set[str]) -> str:
    base = role.lower().replace(" ", "-") or "specialist"
    candidate = base
    suffix = 2
    while candidate in used_names:
        candidate = f"{base}-{suffix}"
        suffix += 1
    used_names.add(candidate)
    return candidate


def plan_dynamic_subtasks(
    *,
    user_message: str,
    user_media_refs: Sequence[dict[str, Any]] | None = None,
    max_subagents: int = SHADOW_CLONE_V2_MAX_SUBAGENTS_PER_RUN,
) -> list[dict[str, str]]:
    """Create a minimal dynamic V2 task plan when no explicit plan is injected.

    This is the production-safe facilitator fallback: it avoids the previous
    empty-run behavior while keeping the runner independent from a specific LLM
    planning implementation. The plan remains dynamic because roles are selected
    from the request shape and capped by policy.
    """
    effective_cap = min(
        max(1, int(max_subagents or SHADOW_CLONE_V2_MAX_SUBAGENTS_PER_RUN)),
        SHADOW_CLONE_V2_MAX_SUBAGENTS_PER_RUN,
    )
    text = str(user_message or "").strip()
    lowered = text.lower()
    has_media = bool(user_media_refs)
    planned: list[tuple[str, str, str]] = []

    def add(task_id: str, role: str, focus: str) -> None:
        if len(planned) >= effective_cap:
            return
        if any(existing_id == task_id for existing_id, _role, _focus in planned):
            return
        planned.append((task_id, role, focus))

    if has_media or any(
        keyword in lowered
        for keyword in ("document", "docx", "pdf", "memo", "spreadsheet", "extract", "summarize")
    ):
        add(
            "document_analysis",
            "document analyst",
            "Analyze the provided/requested document content and extract the important facts, structure, and action items.",
        )

    asks_for_distinct_variants = (
        (
            ("版本 a" in lowered or "version a" in lowered)
            and ("版本 b" in lowered or "version b" in lowered)
        )
        or "不同版本" in lowered
        or "different versions" in lowered
        or "distinct variants" in lowered
        or "multiple variants" in lowered
    )
    if asks_for_distinct_variants:
        add(
            "deliverable_variant_a",
            "variant A specialist",
            "Produce one distinct deliverable variant requested by the user. Keep it independent from other variants, make the angle/style/format clearly different, and reference any /workspace artifacts when applicable.",
        )
        add(
            "deliverable_variant_b",
            "variant B specialist",
            "Produce one distinct deliverable variant requested by the user. Keep it independent from other variants, make the angle/style/format clearly different, and reference any /workspace artifacts when applicable.",
        )
        add(
            "deliverable_variant_c",
            "variant C specialist",
            "Produce one distinct deliverable variant requested by the user. Keep it independent from other variants, make the angle/style/format clearly different, and reference any /workspace artifacts when applicable.",
        )
    elif any(keyword in lowered for keyword in ("manim", "animation", "video", "visual", "diagram")):
        add(
            "visual_production",
            "visual production specialist",
            "Design or produce the requested visual/animation deliverable and keep all artifacts under /workspace.",
        )

    if any(keyword in lowered for keyword in ("research", "market", "compare", "competitor", "policy", "strategy")):
        add(
            "research",
            "research analyst",
            "Research and compare the key options, evidence, risks, and trade-offs needed for the final answer.",
        )

    if any(keyword in lowered for keyword in ("qa", "review", "check", "verify", "validate", "quality")):
        add(
            "quality_review",
            "quality reviewer",
            "Review the work for completeness, factual consistency, missing requirements, and user-facing clarity.",
        )

    if not planned:
        add(
            "task_execution",
            "task specialist",
            "Complete the user's request directly and produce a concise, useful deliverable.",
        )

    return [
        {
            "id": task_id,
            "role": role,
            "task_description": (
                f"User request:\n{text or 'Complete the user request.'}\n\n"
                f"Your focus:\n{focus}\n\n"
                "Coordinate through Shadow Clone V2 mailbox/events when useful. "
                "Return a concise completion summary and reference any /workspace artifacts."
            ),
        }
        for task_id, role, focus in planned
    ]


def normalize_dynamic_team_plan(
    *,
    run_id: str,
    thread_id: str,
    project_id: str,
    subtasks: Sequence[dict[str, Any]],
    created_at: str,
) -> tuple[TeamConfig, list[Task]]:
    """Convert a main-agent subtask proposal into V2 team/task records."""
    members: list[AgentIdentity] = []
    tasks: list[Task] = []
    agent_names_by_index: list[str] = []
    agent_names_by_role: dict[str, str] = {}
    effective_subagent_cap = SHADOW_CLONE_V2_MAX_SUBAGENTS_PER_RUN

    for index, raw_subtask in enumerate(subtasks, start=1):
        subtask = dict(raw_subtask or {})
        role = _safe_text(subtask.get("role"), f"specialist-{index}")
        task_id = _safe_text(subtask.get("id"), f"task-{index}")
        description = _safe_text(
            subtask.get("task_description") or subtask.get("description"),
            "Complete the assigned Shadow Clone V2 task.",
        )
        blocked_by = [
            str(task_id).strip()
            for task_id in (subtask.get("blocked_by") or [])
            if str(task_id).strip()
        ]
        blocks = [
            str(task_id).strip()
            for task_id in (subtask.get("blocks") or [])
            if str(task_id).strip()
        ]
        if len(members) < effective_subagent_cap:
            used_names = set(agent_names_by_index)
            agent_name = agent_names_by_role.get(role)
            if agent_name is None:
                agent_name = _agent_name_for_role(role, used_names)
                agent_names_by_role[role] = agent_name
                agent_names_by_index.append(agent_name)
                members.append(
                    AgentIdentity(
                        agent_id=f"{agent_name}@{thread_id}",
                        agent_name=agent_name,
                        thread_id=thread_id,
                        project_id=project_id,
                        role=role,
                    )
                )
        else:
            agent_name = agent_names_by_index[(index - 1) % effective_subagent_cap]
            role = next(
                (
                    member.role
                    for member in members
                    if member.agent_name == agent_name
                ),
                role,
            )
        tasks.append(
            Task(
                id=task_id,
                run_id=run_id,
                thread_id=thread_id,
                subject=role,
                description=description,
                blocked_by=blocked_by,
                blocks=blocks,
                created_at=created_at,
                updated_at=created_at,
                metadata={"agent_name": agent_name},
            )
        )

    validate_run_subagent_capacity(
        reused_agent_names=[],
        new_agent_names=[member.agent_name for member in members],
    )
    team = TeamConfig(
        team_id=f"shadow-clone-v2:{project_id}:{thread_id}",
        thread_id=thread_id,
        project_id=project_id,
        facilitator_id=f"facilitator@{thread_id}",
        members=members,
    )
    return team, tasks
