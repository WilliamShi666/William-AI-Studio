"""DAG helpers for Shadow Clone subtask execution."""

from __future__ import annotations

from collections import defaultdict, deque
from typing import Dict, List


REQUIRED_SUBTASK_FIELDS = ("id", "role", "task_description")


def validate_subtasks(subtasks: List[dict]) -> None:
    """Validate subtask schema and uniqueness."""
    if not isinstance(subtasks, list):
        raise ValueError("subtasks must be a list")

    seen_ids: set[str] = set()
    for index, subtask in enumerate(subtasks):
        if not isinstance(subtask, dict):
            raise ValueError(f"subtask at index {index} must be a dict")

        for field in REQUIRED_SUBTASK_FIELDS:
            value = subtask.get(field)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(
                    f"subtask[{index}] missing required non-empty field: {field}",
                )

        subtask_id = subtask["id"].strip()
        if subtask_id in seen_ids:
            raise ValueError(f"duplicate subtask id: {subtask_id}")
        seen_ids.add(subtask_id)


def validate_dependencies(subtasks: List[dict], dependencies: List[dict]) -> None:
    """Validate dependency references and self-loop constraints."""
    if dependencies is None:
        return
    if not isinstance(dependencies, list):
        raise ValueError("dependencies must be a list")

    subtask_ids = {str(item["id"]).strip() for item in subtasks}
    for index, dep in enumerate(dependencies):
        if not isinstance(dep, dict):
            raise ValueError(f"dependency at index {index} must be a dict")
        from_id = str(dep.get("from_id", "")).strip()
        to_id = str(dep.get("to_id", "")).strip()
        if not from_id or not to_id:
            raise ValueError(
                f"dependency[{index}] must include non-empty from_id/to_id",
            )
        if from_id not in subtask_ids:
            raise ValueError(f"dependency[{index}] unknown from_id: {from_id}")
        if to_id not in subtask_ids:
            raise ValueError(f"dependency[{index}] unknown to_id: {to_id}")
        if from_id == to_id:
            raise ValueError(f"dependency[{index}] self-cycle detected: {from_id}")


def validate_layer_widths(
    subtasks: List[dict],
    dependencies: List[dict] | None,
    *,
    max_layer_width: int,
) -> None:
    """Reject naturally wide parallel layers before runtime reshaping."""
    if max_layer_width < 1:
        raise ValueError("max_layer_width must be at least 1")

    layers = topological_sort_layers(subtasks, dependencies, max_parallelism=None)
    widest_layer = max((len(layer) for layer in layers), default=0)
    if widest_layer > max_layer_width:
        raise ValueError(
            f"same-layer fanout {widest_layer} exceeds max_layer_width={max_layer_width}",
        )


def topological_sort_layers(
    subtasks: List[dict],
    dependencies: List[dict] | None,
    *,
    max_parallelism: int | None = None,
) -> List[List[dict]]:
    """
    Group subtasks into topological layers with stable ordering.

    Returns:
        A list of layers, each layer containing executable subtasks in parallel.
    """
    if not subtasks:
        return []

    validate_subtasks(subtasks)
    dep_list = dependencies or []
    validate_dependencies(subtasks, dep_list)

    id_to_subtask: Dict[str, dict] = {str(item["id"]).strip(): item for item in subtasks}
    original_index = {str(item["id"]).strip(): idx for idx, item in enumerate(subtasks)}

    graph: Dict[str, set[str]] = defaultdict(set)
    indegree: Dict[str, int] = {subtask_id: 0 for subtask_id in id_to_subtask}

    for dep in dep_list:
        from_id = str(dep["from_id"]).strip()
        to_id = str(dep["to_id"]).strip()
        if to_id in graph[from_id]:
            continue
        graph[from_id].add(to_id)
        indegree[to_id] += 1

    ready = deque(
        sorted(
            [subtask_id for subtask_id, degree in indegree.items() if degree == 0],
            key=lambda sid: original_index[sid],
        ),
    )
    visited = 0
    layers: List[List[dict]] = []

    while ready:
        current_layer_ids = list(ready)
        ready.clear()
        current_layer = [id_to_subtask[subtask_id] for subtask_id in current_layer_ids]
        if max_parallelism is not None and max_parallelism > 0:
            for start in range(0, len(current_layer), max_parallelism):
                layers.append(current_layer[start : start + max_parallelism])
        else:
            layers.append(current_layer)
        visited += len(current_layer_ids)

        next_ready: List[str] = []
        for subtask_id in current_layer_ids:
            for neighbor in graph.get(subtask_id, set()):
                indegree[neighbor] -= 1
                if indegree[neighbor] == 0:
                    next_ready.append(neighbor)

        for subtask_id in sorted(next_ready, key=lambda sid: original_index[sid]):
            ready.append(subtask_id)

    if visited != len(subtasks):
        cycle_nodes = sorted(
            [subtask_id for subtask_id, degree in indegree.items() if degree > 0],
            key=lambda sid: original_index[sid],
        )
        raise ValueError(f"Cycle detected involving: {cycle_nodes}")

    return layers
