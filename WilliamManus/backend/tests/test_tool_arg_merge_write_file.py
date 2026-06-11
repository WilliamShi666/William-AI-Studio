import json
import importlib.util
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

MODULE_PATH = BACKEND_ROOT / "agentscope_integration" / "utils" / "tool_arg_merge.py"
spec = importlib.util.spec_from_file_location("tool_arg_merge_module", MODULE_PATH)
module = importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules["tool_arg_merge_module"] = module
spec.loader.exec_module(module)

merge_write_file_arguments = module.merge_write_file_arguments
merge_tool_arguments = module.merge_tool_arguments
sanitize_write_file_input = module.sanitize_write_file_input
is_plausible_write_file_path = module.is_plausible_write_file_path


def test_merge_tool_arguments_preserves_true_delta_repeated_boundary_chars() -> None:
    """True delta streams must not treat repeated boundary chars as overlap."""
    cases = [
        ("045", "5", "0455"),
        ("occur", "rence", "occurrence"),
        ("occur", "rences", "occurrences"),
        (
            '{"path":"/workspace/045',
            '5.md","content":"ok"}',
            '{"path":"/workspace/0455.md","content":"ok"}',
        ),
    ]

    for previous, incoming, expected in cases:
        assert merge_tool_arguments(previous, incoming) == expected


def test_merge_write_file_arguments_removes_pseudo_json_key_fragment() -> None:
    previous = (
        '{"path":"us-constitutional-convention/index.html","content":"<!DOCTYPE html>\\n'
        '<link href=\\"https://fonts.googleapis.com\\" rel=\\"stylesheet\\">"}'
    )
    incoming = (
        '{"path":"us-constitutional-convention/index.html","content":"<!DOCTYPE html>\\n'
        '<link href=\\"https://fonts.googleapis.com\\" rel=\\"stylesheet\\">",'
        '"href=\\"https":""}'
    )

    merged = merge_write_file_arguments(previous, incoming)
    payload = json.loads(merged)

    assert payload["path"] == "us-constitutional-convention/index.html"
    assert 'href="https' not in payload
    assert payload["content"].startswith("<!DOCTYPE html>")
    assert "fonts.googleapis.com" in payload["content"]


def test_merge_write_file_arguments_keeps_longer_clean_content_snapshot() -> None:
    previous = '{"path":"/workspace/demo.py","content":"print(1)"}'
    incoming = '{"path":"/workspace/demo.py","content":"print(1)\\nprint(2)"}'

    merged = merge_write_file_arguments(previous, incoming)
    payload = json.loads(merged)

    assert payload["path"] == "/workspace/demo.py"
    assert payload["content"] == "print(1)\nprint(2)"


def test_merge_write_file_arguments_avoids_shorter_parseable_regression() -> None:
    previous = '{"path":"/workspace/demo.py","content":"print(1)'
    incoming = '{"path":"/workspace/demo.py"}'

    merged = merge_write_file_arguments(previous, incoming)

    # Keep richer raw accumulation so later chunks can still complete content reconstruction.
    assert "print(1)" in merged or "print(1" in merged
    assert len(merged) >= len(incoming)


def test_merge_write_file_arguments_preserves_previous_clean_parseable_snapshot() -> None:
    previous = '{"path":"/workspace/a.html","content":"new Chart(occurrences, 0455)"}'
    incoming = '{"path":"/workspace/a.html","content":"ew Chart(occurences, 045)"}'

    merged = merge_write_file_arguments(previous, incoming)
    payload = json.loads(merged)

    assert payload["path"] == "/workspace/a.html"
    assert payload["content"] == "new Chart(occurrences, 0455)"


def test_merge_write_file_arguments_preserves_left_brace_delta_inside_content() -> None:
    expected = json.dumps(
        {
            "path": "/workspace/brace.md",
            "content": 'regex = r"^a{2,4}\\d+$"\n',
        },
        ensure_ascii=False,
    )
    split_at = expected.index("{2,4}")
    chunks = [expected[:split_at], expected[split_at], expected[split_at + 1 :]]

    merged = ""
    for chunk in chunks:
        merged = merge_write_file_arguments(merged, chunk)

    payload = json.loads(merged)
    assert payload["path"] == "/workspace/brace.md"
    assert payload["content"] == 'regex = r"^a{2,4}\\d+$"\n'


def test_sanitize_write_file_input_drops_implausible_path_fragment() -> None:
    sanitized = sanitize_write_file_input(
        {"path": "constitutional", "content": "hello"},
        raw_arguments='{"path":"constitutional","content":"hello"}',
    )
    assert "path" not in sanitized
    assert sanitized["content"] == "hello"


def test_sanitize_write_file_input_accepts_html_filename_without_directory() -> None:
    sanitized = sanitize_write_file_input(
        {
            "path": "deepseek_static_artifact.html",
            "content": "<html><body></body></html>",
        }
    )

    assert sanitized["path"] == "deepseek_static_artifact.html"
    assert sanitized["content"].startswith("<html>")


def test_is_plausible_write_file_path_accepts_workspace_relative_and_dot_file() -> None:
    assert is_plausible_write_file_path("/workspace/demo.py")
    assert is_plausible_write_file_path("src/index.ts")
    assert is_plausible_write_file_path(".env")
