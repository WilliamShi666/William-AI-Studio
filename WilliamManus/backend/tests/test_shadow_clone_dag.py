from agentscope_integration.shadow_clone.dag_executor import (
    topological_sort_layers,
    validate_dependencies,
    validate_layer_widths,
    validate_subtasks,
)


def test_topological_sort_all_parallel():
    subtasks = [
        {"id": "a", "role": "r1", "task_description": "A"},
        {"id": "b", "role": "r2", "task_description": "B"},
    ]
    layers = topological_sort_layers(subtasks, [])
    assert len(layers) == 1
    assert [item["id"] for item in layers[0]] == ["a", "b"]


def test_topological_sort_chunks_parallel_layer_when_requested():
    subtasks = [
        {"id": "a", "role": "r1", "task_description": "A"},
        {"id": "b", "role": "r2", "task_description": "B"},
        {"id": "c", "role": "r3", "task_description": "C"},
        {"id": "d", "role": "r4", "task_description": "D"},
        {"id": "e", "role": "r5", "task_description": "E"},
    ]

    layers = topological_sort_layers(subtasks, [], max_parallelism=2)

    assert [[item["id"] for item in layer] for layer in layers] == [
        ["a", "b"],
        ["c", "d"],
        ["e"],
    ]


def test_topological_sort_chain():
    subtasks = [
        {"id": "a", "role": "r1", "task_description": "A"},
        {"id": "b", "role": "r2", "task_description": "B"},
        {"id": "c", "role": "r3", "task_description": "C"},
    ]
    dependencies = [
        {"from_id": "a", "to_id": "b"},
        {"from_id": "b", "to_id": "c"},
    ]
    layers = topological_sort_layers(subtasks, dependencies)
    assert [[item["id"] for item in layer] for layer in layers] == [["a"], ["b"], ["c"]]


def test_cycle_detection_raises():
    subtasks = [
        {"id": "a", "role": "r1", "task_description": "A"},
        {"id": "b", "role": "r2", "task_description": "B"},
    ]
    dependencies = [
        {"from_id": "a", "to_id": "b"},
        {"from_id": "b", "to_id": "a"},
    ]
    try:
        topological_sort_layers(subtasks, dependencies)
        assert False, "expected ValueError for cycle"
    except ValueError as exc:
        assert "Cycle detected" in str(exc)


def test_invalid_dependency_reference_raises():
    subtasks = [{"id": "a", "role": "r1", "task_description": "A"}]
    dependencies = [{"from_id": "a", "to_id": "missing"}]
    try:
        validate_dependencies(subtasks, dependencies)
        assert False, "expected ValueError for unknown dependency node"
    except ValueError as exc:
        assert "unknown to_id" in str(exc)


def test_validate_subtasks_requires_fields():
    subtasks = [{"id": "a", "role": "r1"}]
    try:
        validate_subtasks(subtasks)
        assert False, "expected ValueError for missing task_description"
    except ValueError as exc:
        assert "task_description" in str(exc)


def test_validate_layer_widths_rejects_wide_natural_parallel_layer():
    subtasks = [
        {"id": "a", "role": "r1", "task_description": "A"},
        {"id": "b", "role": "r2", "task_description": "B"},
        {"id": "c", "role": "r3", "task_description": "C"},
    ]

    try:
        validate_layer_widths(subtasks, [], max_layer_width=2)
        assert False, "expected ValueError for a naturally wide layer"
    except ValueError as exc:
        assert "same-layer fanout" in str(exc)
