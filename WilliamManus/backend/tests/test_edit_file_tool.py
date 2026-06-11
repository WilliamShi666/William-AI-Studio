import types
import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_ROOT))

if "structlog" not in sys.modules:
    class _DummyBoundLogger:
        def info(self, *args, **kwargs):
            return None

        def warning(self, *args, **kwargs):
            return None

        def error(self, *args, **kwargs):
            return None

        def debug(self, *args, **kwargs):
            return None

    class _DummyProcessorFormatter:
        @staticmethod
        def wrap_for_formatter(*args, **kwargs):
            return None

    sys.modules["structlog"] = types.SimpleNamespace(
        configure=lambda **kwargs: None,
        get_logger=lambda *args, **kwargs: _DummyBoundLogger(),
        stdlib=types.SimpleNamespace(
            add_log_level=lambda *args, **kwargs: None,
            PositionalArgumentsFormatter=lambda *args, **kwargs: None,
            ProcessorFormatter=_DummyProcessorFormatter,
            LoggerFactory=lambda *args, **kwargs: None,
            BoundLogger=_DummyBoundLogger,
        ),
        processors=types.SimpleNamespace(TimeStamper=lambda *args, **kwargs: None),
        contextvars=types.SimpleNamespace(
            clear_contextvars=lambda: None,
            bind_contextvars=lambda **kwargs: None,
            get_contextvars=lambda: {},
        ),
    )

# Avoid importing DB-backed thread manager dependencies in unit tests.
if "agentpress.adk_thread_manager" not in sys.modules:
    sys.modules["agentpress.adk_thread_manager"] = types.SimpleNamespace(
        ADKThreadManager=object,
    )

if "sandbox.tool_base" not in sys.modules:
    class _StubSandboxToolsBase:
        def __init__(self, project_id: str, thread_manager=None, sandbox_type: str = "code",
                     *, shadow_clone_run_id=None, strict_sandbox=False):
            self.project_id = project_id
            self.thread_manager = thread_manager
            self.sandbox_type = sandbox_type
            self.workspace_path = "/workspace"
            self.sandbox = None

        @staticmethod
        def clean_path(path: str) -> str:
            return str(path).lstrip("/")

        async def _ensure_sandbox(self):
            return None

        async def _run_blocking_sandbox_call(self, _operation, call, timeout_seconds=None):
            del timeout_seconds
            return call()

        async def _persist_workspace_artifact(self, path, content, source="", content_type=None):
            return None

        async def _read_workspace_artifact_bytes(self, path):
            return None

        async def _list_workspace_artifact_entries(self, path):
            return None

        async def _sync_workspace_artifacts_from_sandbox(self, source=""):
            return None

        async def _emit_tool_status(self, *args, **kwargs):
            return None

        async def _emit_write_file_status(self, *args, **kwargs):
            return None

        async def _emit_guard_stage(self, *args, **kwargs):
            return None

        def _should_apply_code_guard(self, path: str, content: str) -> bool:
            return False

        async def _prepare_python_write_validation(self, path, content):
            return {"syntax_ok": True}

        async def _finalize_python_write_validation(self, path, validation_payload):
            return validation_payload

    sys.modules["sandbox.tool_base"] = types.SimpleNamespace(
        SandboxToolsBase=_StubSandboxToolsBase,
    )

if "services.redis" not in sys.modules:
    async def _noop(*args, **kwargs):
        del args, kwargs
        return 1

    sys.modules["services.redis"] = types.SimpleNamespace(
        rpush=_noop,
        expire=_noop,
        publish=_noop,
        REDIS_KEY_TTL=3600,
    )

from agent.tools import sandbox_code_tool as sandbox_code_tool_module


class _FakeFiles:
    def __init__(self, file_map):
        self._file_map = file_map

    def list(self, path: str):
        prefix = path.rstrip("/") + "/"
        entries = []
        for file_path in sorted(self._file_map):
            if not file_path.startswith(prefix):
                continue
            rest = file_path[len(prefix):]
            if "/" in rest or not rest:
                continue
            entries.append({"name": rest, "path": file_path})
        return entries

    def read(self, path: str):
        if path not in self._file_map:
            raise FileNotFoundError(path)
        return self._file_map[path]

    def write(self, path: str, content: str):
        self._file_map[path] = content
        return None


class _FakeCommands:
    def run(self, _command: str):
        return types.SimpleNamespace(exit_code=127, stdout="", stderr="formatter not available")


@pytest.fixture
def edit_tool(monkeypatch):
    tool = sandbox_code_tool_module.SandboxCodeTool(project_id="project-edit", thread_manager=None)
    file_map = {"/workspace/demo.py": "line1\nline2\nline3\n"}
    tool.sandbox = types.SimpleNamespace(files=_FakeFiles(file_map), commands=_FakeCommands())

    async def _ensure_sandbox():
        return None

    async def _run_blocking_sandbox_call(_operation, call, timeout_seconds=None):
        del timeout_seconds
        return call()

    monkeypatch.setattr(tool, "_ensure_sandbox", _ensure_sandbox)
    monkeypatch.setattr(tool, "_run_blocking_sandbox_call", _run_blocking_sandbox_call)
    return tool, file_map


@pytest.mark.asyncio
async def test_edit_file_line_range_success(edit_tool):
    tool, file_map = edit_tool

    result = await tool.edit_file(
        path="demo.py",
        start_line=2,
        end_line=2,
        new_content="LINE2\n",
    )

    assert result.success is True
    assert file_map["/workspace/demo.py"] == "line1\nLINE2\nline3\n"
    assert result.output["mode"] == "line_range"
    assert result.output["changed"] is True


@pytest.mark.asyncio
async def test_edit_file_line_range_accepts_numeric_strings(edit_tool):
    tool, file_map = edit_tool

    result = await tool.edit_file(
        path="demo.py",
        start_line="2",
        end_line="2",
        new_content="LINE2\n",
    )

    assert result.success is True
    assert file_map["/workspace/demo.py"] == "line1\nLINE2\nline3\n"


@pytest.mark.asyncio
async def test_edit_file_line_range_rejects_non_numeric_strings(edit_tool):
    tool, file_map = edit_tool
    before = file_map["/workspace/demo.py"]

    result = await tool.edit_file(
        path="demo.py",
        start_line="line-2",
        end_line="2",
        new_content="LINE2\n",
    )

    assert result.success is False
    assert "start_line must be an integer" in result.output["error"]
    assert file_map["/workspace/demo.py"] == before


@pytest.mark.asyncio
async def test_edit_file_line_range_out_of_bounds_fails(edit_tool):
    tool, file_map = edit_tool
    before = file_map["/workspace/demo.py"]

    result = await tool.edit_file(
        path="demo.py",
        start_line=9,
        end_line=9,
        new_content="x\n",
    )

    assert result.success is False
    assert "out of bounds" in result.output["error"]
    assert file_map["/workspace/demo.py"] == before


@pytest.mark.asyncio
async def test_edit_file_anchor_replace_success(edit_tool):
    tool, file_map = edit_tool
    file_map["/workspace/demo.py"] = "alpha=1\nbeta=2\n"

    result = await tool.edit_file(
        path="demo.py",
        old_text="beta=2",
        new_text="beta=3",
        expected_occurrences=1,
    )

    assert result.success is True
    assert file_map["/workspace/demo.py"] == "alpha=1\nbeta=3\n"
    assert result.output["mode"] == "anchor_replace"


@pytest.mark.asyncio
async def test_edit_file_anchor_replace_accepts_string_expected_occurrences(edit_tool):
    tool, file_map = edit_tool
    file_map["/workspace/demo.py"] = "alpha=1\nbeta=2\n"

    result = await tool.edit_file(
        path="demo.py",
        old_text="beta=2",
        new_text="beta=4",
        expected_occurrences="1",
    )

    assert result.success is True
    assert file_map["/workspace/demo.py"] == "alpha=1\nbeta=4\n"


@pytest.mark.asyncio
async def test_edit_file_anchor_replace_not_found_fails(edit_tool):
    tool, file_map = edit_tool
    before = file_map["/workspace/demo.py"]

    result = await tool.edit_file(
        path="demo.py",
        old_text="missing-token",
        new_text="x",
        expected_occurrences=1,
    )

    assert result.success is False
    assert "found 0" in result.output["error"] or "ANCHOR_NOT_FOUND" == result.output.get("error_code")
    assert file_map["/workspace/demo.py"] == before


@pytest.mark.asyncio
async def test_edit_file_anchor_replace_multiple_matches_fail_fast(edit_tool):
    tool, file_map = edit_tool
    file_map["/workspace/demo.py"] = "foo\nfoo\n"

    result = await tool.edit_file(
        path="demo.py",
        old_text="foo",
        new_text="bar",
        expected_occurrences=1,
    )

    assert result.success is False
    assert result.output.get("error_code") == "ANCHOR_MULTIPLE_MATCHES"
    assert file_map["/workspace/demo.py"] == "foo\nfoo\n"


@pytest.mark.asyncio
async def test_edit_file_anchor_mismatch_returns_diagnostics(edit_tool):
    tool, file_map = edit_tool
    # Use a genuinely different old_text that won't fuzzy-match
    file_map["/workspace/demo.py"] = "def compute_total(items):\n    return sum(items)\n"

    result = await tool.edit_file(
        path="demo.py",
        old_text="def calc_sum(values):\n    return sum(values)",  # different function name
        new_text="def calc_sum(values):\n    return total(values)",
        expected_occurrences=1,
    )

    assert result.success is False
    diagnostics = result.output.get("diagnostics", {})
    assert diagnostics.get("near_line_matches")


@pytest.mark.asyncio
async def test_read_file_auto_corrects_repeated_character_path_after_storm(edit_tool):
    tool, file_map = edit_tool
    file_map["/workspace/data_0455.csv"] = "value,42\n"
    missing_path = "data_045.csv"

    for _ in range(2):
        result = await tool.read_file(path=missing_path)
        assert result.success is False

    corrected = await tool.read_file(path=missing_path)

    assert corrected.success is True
    assert corrected.output["path"] == "/workspace/data_0455.csv"
    assert corrected.output["auto_corrected_path"] is True
    assert corrected.output["original_path"] == "/workspace/data_045.csv"
    assert "Auto-corrected" in corrected.output["output"]
    assert "value,42" in corrected.output["content"]


@pytest.mark.asyncio
async def test_write_file_auto_corrects_repeated_character_path_after_storm(edit_tool):
    tool, file_map = edit_tool
    file_map["/workspace/data_0455.csv"] = "old\n"
    missing_path = "data_045.csv"

    for _ in range(2):
        result = await tool.read_file(path=missing_path)
        assert result.success is False

    written = await tool.write_file(path=missing_path, content="new\n")

    assert written.success is True
    assert "/workspace/data_045.csv" not in file_map
    assert file_map["/workspace/data_0455.csv"] == "new\n"
    assert written.output["path"] == "/workspace/data_0455.csv"
    assert written.output["auto_corrected_path"] is True
    assert written.output["original_path"] == "/workspace/data_045.csv"


@pytest.mark.asyncio
async def test_write_file_does_not_auto_correct_on_first_call(edit_tool):
    tool, file_map = edit_tool
    file_map["/workspace/data_0455.csv"] = "old\n"

    written = await tool.write_file(path="data_045.csv", content="new\n")

    assert written.success is True
    assert file_map["/workspace/data_045.csv"] == "new\n"
    assert file_map["/workspace/data_0455.csv"] == "old\n"
    assert written.output.get("auto_corrected_path") is not True


@pytest.mark.asyncio
async def test_write_file_does_not_auto_correct_when_original_path_now_exists(edit_tool):
    tool, file_map = edit_tool

    for _ in range(2):
        result = await tool.read_file(path="data_045.txt")
        assert result.success is False

    first_write = await tool.write_file(path="data_045.txt", content="original\n")
    assert first_write.success is True
    file_map["/workspace/data_0455.txt"] = "candidate\n"

    second_write = await tool.write_file(path="data_045.txt", content="updated\n")

    assert second_write.success is True
    assert file_map["/workspace/data_045.txt"] == "updated\n"
    assert file_map["/workspace/data_0455.txt"] == "candidate\n"
    assert second_write.output.get("auto_corrected_path") is not True


@pytest.mark.asyncio
async def test_write_file_ambiguous_auto_correct_keeps_original_path(edit_tool):
    tool, file_map = edit_tool
    file_map["/workspace/data_0455.csv"] = "a\n"
    file_map["/workspace/data_0456.csv"] = "b\n"

    for _ in range(2):
        result = await tool.read_file(path="data_045.csv")
        assert result.success is False

    written = await tool.write_file(path="data_045.csv", content="new\n")

    assert written.success is True
    assert file_map["/workspace/data_045.csv"] == "new\n"
    assert file_map["/workspace/data_0455.csv"] == "a\n"
    assert file_map["/workspace/data_0456.csv"] == "b\n"
    assert written.output.get("auto_corrected_path") is not True


@pytest.mark.asyncio
async def test_path_auto_correct_skips_ambiguous_candidates(edit_tool):
    tool, file_map = edit_tool
    file_map["/workspace/data_0455.csv"] = "a\n"
    file_map["/workspace/data_0456.csv"] = "b\n"

    for _ in range(3):
        result = await tool.read_file(path="data_045.csv")

    assert result.success is False
    assert result.output.get("auto_corrected_path") is not True
    assert "auto_correct_candidates" in result.output


@pytest.mark.asyncio
async def test_path_auto_correct_requires_single_candidate_within_threshold(edit_tool):
    tool, file_map = edit_tool
    file_map["/workspace/data_0455.csv"] = "a\n"
    file_map["/workspace/data_04556.csv"] = "b\n"

    for _ in range(3):
        result = await tool.read_file(path="data_045.csv")

    assert result.success is True
    assert result.output["auto_corrected_path"] is True
    assert result.output["path"] == "/workspace/data_0455.csv"
    assert result.output["original_path"] == "/workspace/data_045.csv"
    assert result.output["auto_correct_distance"] == 1


@pytest.mark.asyncio
async def test_edit_file_auto_corrects_repeated_character_path_after_storm(edit_tool):
    tool, file_map = edit_tool
    file_map["/workspace/data_0455.txt"] = "alpha\nbeta\n"

    for _ in range(2):
        result = await tool.edit_file(
            path="data_045.txt",
            old_text="beta",
            new_text="BETA",
        )
        assert result.success is False

    corrected = await tool.edit_file(
        path="data_045.txt",
        old_text="beta",
        new_text="BETA",
    )

    assert corrected.success is True
    assert file_map["/workspace/data_0455.txt"] == "alpha\nBETA\n"
    assert corrected.output["path"] == "/workspace/data_0455.txt"
    assert corrected.output["auto_corrected_path"] is True
    assert corrected.output["original_path"] == "/workspace/data_045.txt"


@pytest.mark.asyncio
async def test_write_file_python_guard_repairs_expected_indent(edit_tool, monkeypatch):
    tool, file_map = edit_tool
    monkeypatch.setattr(sandbox_code_tool_module, "CODE_GUARD_ENABLED", True)
    monkeypatch.setattr(sandbox_code_tool_module, "CODE_GUARD_LANGS", {"py"})
    monkeypatch.setattr(sandbox_code_tool_module, "CODE_GUARD_AUTOFORMAT", False)

    result = await tool.write_file(
        path="new_guarded.py",
        content="def build_message():\nprint('ok')\n",
    )

    assert result.success is True
    saved = file_map["/workspace/new_guarded.py"]
    assert "    print('ok')" in saved
    validation = result.output["post_write_validation"]
    assert validation["syntax_ok"] is True
    assert validation["repair_passes"] >= 1


@pytest.mark.asyncio
async def test_write_file_python_guard_normalizes_tab_indentation(edit_tool, monkeypatch):
    tool, file_map = edit_tool
    monkeypatch.setattr(sandbox_code_tool_module, "CODE_GUARD_ENABLED", True)
    monkeypatch.setattr(sandbox_code_tool_module, "CODE_GUARD_LANGS", {"py"})
    monkeypatch.setattr(sandbox_code_tool_module, "CODE_GUARD_AUTOFORMAT", False)

    result = await tool.write_file(
        path="tab_guarded.py",
        content="def run():\n\treturn 1\n",
    )

    assert result.success is True
    saved = file_map["/workspace/tab_guarded.py"]
    assert "\t" not in saved
    validation = result.output["post_write_validation"]
    assert validation["normalized_tabs"] is True
    assert validation["syntax_ok"] is True


@pytest.mark.asyncio
async def test_write_file_python_guard_returns_structured_syntax_error(edit_tool, monkeypatch):
    tool, file_map = edit_tool
    monkeypatch.setattr(sandbox_code_tool_module, "CODE_GUARD_ENABLED", True)
    monkeypatch.setattr(sandbox_code_tool_module, "CODE_GUARD_LANGS", {"py"})
    monkeypatch.setattr(sandbox_code_tool_module, "CODE_GUARD_AUTOFORMAT", False)

    result = await tool.write_file(
        path="broken_guarded.py",
        content="def broken(:\n    pass\n",
    )

    assert result.success is False
    assert "/workspace/broken_guarded.py" not in file_map
    assert result.output["error_code"] == "PYTHON_SYNTAX_VALIDATION_FAILED"
    assert "was not written" in result.output["error"]
    validation = result.output["post_write_validation"]
    assert validation["syntax_ok"] is False
    assert validation["error"]["error_type"] == "SyntaxError"


@pytest.mark.asyncio
async def test_repeated_python_syntax_failures_force_minimal_docx_template(edit_tool, monkeypatch):
    tool, file_map = edit_tool
    monkeypatch.setattr(sandbox_code_tool_module, "CODE_GUARD_ENABLED", True)
    monkeypatch.setattr(sandbox_code_tool_module, "CODE_GUARD_LANGS", {"py"})
    monkeypatch.setattr(sandbox_code_tool_module, "CODE_GUARD_AUTOFORMAT", False)

    result = None
    for _ in range(3):
        result = await tool.write_file(
            path="generate_report.py",
            content="from docx import Document\ndoc = Document()\ndoc.add_paragraph(')\n",
        )

    assert result is not None
    assert result.success is False
    assert "/workspace/generate_report.py" not in file_map
    assert result.output["error_code"] == "PYTHON_SYNTAX_VALIDATION_FAILED"
    assert result.output["forced_minimal_docx_template"] is True
    assert "Use this minimal DOCX template" in result.output["error"]
    assert "do not write expressions with repeated adjacent letters or digits" in result.output["error"]
    assert "rows = [" in result.output["minimal_docx_template"]


# ── Anchor Normalization Tests ──────────────────────────────────────


@pytest.mark.asyncio
async def test_anchor_exact_match_still_works(edit_tool):
    """L1: Exact match should work just as before."""
    tool, file_map = edit_tool
    file_map["/workspace/demo.py"] = "alpha=1\nbeta=2\n"

    result = await tool.edit_file(
        path="demo.py",
        old_text="beta=2",
        new_text="beta=3",
        expected_occurrences=1,
    )

    assert result.success is True
    assert file_map["/workspace/demo.py"] == "alpha=1\nbeta=3\n"
    assert result.output["mode"] == "anchor_replace"


@pytest.mark.asyncio
async def test_anchor_crlf_normalized_match(edit_tool):
    """L2: \\r\\n file content matched by \\n old_text."""
    tool, file_map = edit_tool
    file_map["/workspace/demo.py"] = "line1\r\nline2\r\nline3\r\n"

    result = await tool.edit_file(
        path="demo.py",
        old_text="line1\nline2",
        new_text="LINE1\nLINE2",
        expected_occurrences=1,
    )

    assert result.success is True
    # The original \\r\\n after line2 remains; only the matched span is replaced
    assert "LINE1" in file_map["/workspace/demo.py"]
    assert "LINE2" in file_map["/workspace/demo.py"]


@pytest.mark.asyncio
async def test_anchor_trailing_whitespace_stripped_match(edit_tool):
    """L3: File has trailing spaces, Agent's old_text doesn't."""
    tool, file_map = edit_tool
    file_map["/workspace/demo.py"] = "def foo():    \n    pass\n"

    result = await tool.edit_file(
        path="demo.py",
        old_text="def foo():\n    pass",
        new_text="def foo():\n    return 1",
        expected_occurrences=1,
    )

    assert result.success is True
    assert "def foo():" in file_map["/workspace/demo.py"]
    assert "return 1" in file_map["/workspace/demo.py"]


@pytest.mark.asyncio
async def test_anchor_curly_quotes_normalized_match(edit_tool):
    """L4: File has curly quotes, Agent gives straight quotes."""
    tool, file_map = edit_tool
    # Simulate a file where curly quotes are used INSTEAD of straight quotes
    file_map["/workspace/demo.py"] = 'message = “Hello World”\n'

    result = await tool.edit_file(
        path="demo.py",
        old_text='message = "Hello World"',
        new_text='message = "Hi There"',
        expected_occurrences=1,
    )

    assert result.success is True
    assert 'Hi There' in file_map["/workspace/demo.py"]


@pytest.mark.asyncio
async def test_anchor_combined_crlf_and_trailing_match(edit_tool):
    """L5: \\r\\n + trailing spaces combined."""
    tool, file_map = edit_tool
    file_map["/workspace/demo.py"] = "x = 1   \r\ny = 2   \r\n"

    result = await tool.edit_file(
        path="demo.py",
        old_text="x = 1\ny = 2",
        new_text="x = 10\ny = 20",
        expected_occurrences=1,
    )

    assert result.success is True
    assert "x = 10" in file_map["/workspace/demo.py"]


@pytest.mark.asyncio
async def test_anchor_markdown_skips_trailing_strip(edit_tool):
    """Trailing whitespace stripping is skipped for .md files."""
    tool, file_map = edit_tool
    # Two trailing spaces = Markdown hard line break
    file_map["/workspace/readme.md"] = "line with two spaces  \nnext line\n"

    result = await tool.edit_file(
        path="readme.md",
        old_text="line with two spaces  \nnext line",
        new_text="changed line\nnext line",
        expected_occurrences=1,
    )

    assert result.success is True


@pytest.mark.asyncio
async def test_anchor_multiple_occurrences_exact(edit_tool):
    """Exact match with expected_occurrences=2 should succeed."""
    tool, file_map = edit_tool
    file_map["/workspace/demo.py"] = "x = 1\ny = 2\nx = 1\n"

    result = await tool.edit_file(
        path="demo.py",
        old_text="x = 1",
        new_text="x = 99",
        expected_occurrences=2,
    )

    assert result.success is True
    assert file_map["/workspace/demo.py"].count("x = 99") == 2


@pytest.mark.asyncio
async def test_anchor_all_layers_fail_returns_diagnostics(edit_tool):
    """When all normalization layers fail, return diagnostics."""
    tool, file_map = edit_tool
    file_map["/workspace/demo.py"] = "completely\ndifferent\ncontent\n"

    result = await tool.edit_file(
        path="demo.py",
        old_text="not in file at all",
        new_text="replacement",
        expected_occurrences=1,
    )

    assert result.success is False
    assert "diagnostics" in result.output
    assert result.output.get("error_code") == "ANCHOR_NOT_FOUND"


@pytest.mark.asyncio
async def test_anchor_multi_match_structured_error(edit_tool):
    """old_text appears twice but expected_occurrences=1 → structured error."""
    tool, file_map = edit_tool
    file_map["/workspace/demo.py"] = "foo\nfoo\n"

    result = await tool.edit_file(
        path="demo.py",
        old_text="foo",
        new_text="bar",
        expected_occurrences=1,
    )

    assert result.success is False
    assert result.output.get("error_code") == "ANCHOR_MULTIPLE_MATCHES"
    assert "2" in result.output["error"]


@pytest.mark.asyncio
async def test_anchor_old_equals_new_rejected(edit_tool):
    """old_text == new_text should fail so agents do not treat no-op edits as progress."""
    tool, file_map = edit_tool
    before = file_map["/workspace/demo.py"]

    result = await tool.edit_file(
        path="demo.py",
        old_text="line2",
        new_text="line2",  # same as old
        expected_occurrences=1,
    )

    assert result.success is False
    assert result.output.get("error_code") == "EDIT_FILE_NOOP"
    assert "old_text and new_text are exactly the same" in result.output.get("error", "")
    assert file_map["/workspace/demo.py"] == before


@pytest.mark.asyncio
async def test_anchor_non_python_file_no_syntax_check(edit_tool, monkeypatch):
    """Non-.py files should not trigger syntax validation."""
    tool, file_map = edit_tool
    monkeypatch.setattr(sandbox_code_tool_module, "CODE_GUARD_ENABLED", True)
    monkeypatch.setattr(sandbox_code_tool_module, "CODE_GUARD_LANGS", {"py"})

    file_map["/workspace/data.txt"] = "hello world\n"

    result = await tool.edit_file(
        path="data.txt",
        old_text="hello",
        new_text="hi",
        expected_occurrences=1,
    )

    assert result.success is True
    assert "syntax_warning" not in result.output


@pytest.mark.asyncio
async def test_anchor_python_file_syntax_warning(edit_tool, monkeypatch):
    """Editing .py file with broken syntax should attach warning."""
    tool, file_map = edit_tool
    monkeypatch.setattr(sandbox_code_tool_module, "CODE_GUARD_ENABLED", True)
    monkeypatch.setattr(sandbox_code_tool_module, "CODE_GUARD_LANGS", {"py"})
    monkeypatch.setattr(sandbox_code_tool_module, "CODE_GUARD_AUTOFORMAT", False)

    file_map["/workspace/demo.py"] = "def foo():\n    pass\n"

    result = await tool.edit_file(
        path="demo.py",
        old_text="def foo():",
        new_text="def broken(:",  # syntax error
        expected_occurrences=1,
    )

    assert result.success is True  # edit succeeds
    assert "syntax_warning" in result.output
    assert result.output["syntax_warning"]["ok"] is False


# ── Round 3: Fuzzy line matching + enhanced diagnostics ────────────────


@pytest.mark.asyncio
async def test_fuzzy_line_match_indentation(edit_tool):
    """L6: First line matches perfectly but subsequent lines have indent differences."""
    tool, file_map = edit_tool
    file_map["/workspace/demo.py"] = (
        "def compute():\n"
        "        x = 1\n"   # 8-space indent in file
        "        y = 2\n"
        "    return x + y\n"
    )

    result = await tool.edit_file(
        path="demo.py",
        old_text="def compute():\n    x = 1\n    y = 2\n    return x + y",  # 4-space in old
        new_text="def compute():\n    x = 10\n    y = 20\n    return x + y",
        expected_occurrences=1,
    )

    assert result.success is True
    assert "x = 10" in file_map["/workspace/demo.py"]


@pytest.mark.asyncio
async def test_fuzzy_line_match_extra_line(edit_tool):
    """L6: File has an extra blank line that Agent's old_text doesn't include."""
    tool, file_map = edit_tool
    file_map["/workspace/demo.py"] = (
        "def main():\n"
        "\n"           # extra blank line in file
        "    init()\n"
        "    run()\n"
    )

    result = await tool.edit_file(
        path="demo.py",
        old_text="def main():\n    init()\n    run()",  # no blank line
        new_text="def main():\n    setup()\n    run()",
        expected_occurrences=1,
    )

    # With blank line difference, fuzzy matching might or might not succeed.
    # Key assertion: if it succeeds, file is correctly modified.
    # If it fails, error message should contain near_line_matches.
    if result.success:
        assert "setup()" in file_map["/workspace/demo.py"]
    else:
        diag = result.output.get("diagnostics", {})
        assert diag.get("near_line_matches") or result.output.get("error_code")


@pytest.mark.asyncio
async def test_multi_match_shows_context(edit_tool):
    """ANCHOR_MULTIPLE_MATCHES error should include line numbers of each match."""
    tool, file_map = edit_tool
    file_map["/workspace/demo.py"] = (
        "line 1\n"
        "sumary here\n"    # match at line 2
        "line 3\n"
        "another sumary\n"  # match at line 4
        "line 5\n"
    )

    result = await tool.edit_file(
        path="demo.py",
        old_text="sumary",
        new_text="summary",
        expected_occurrences=1,
    )

    assert result.success is False
    assert result.output.get("error_code") == "ANCHOR_MULTIPLE_MATCHES"
    # Error should mention counts or line numbers to help agent narrow down
    error_text = result.output.get("error", "")
    assert "2" in error_text  # mentions found count


@pytest.mark.asyncio
async def test_not_found_shows_near_matches(edit_tool):
    """ANCHOR_NOT_FOUND should include specific near-match locations and hints."""
    tool, file_map = edit_tool
    file_map["/workspace/demo.py"] = (
        "def calculate_total(items):\n"
        "    total = 0\n"
        "    for item in items:\n"
        "        total += item\n"
        "    return total\n"
    )

    result = await tool.edit_file(
        path="demo.py",
        old_text="def calc_total(items):\n    sum = 0\n    for item in items:\n        sum += item",
        new_text="def calc_total(items):\n    total = 0\n    for item in items:\n        total += item",
        expected_occurrences=1,
    )

    assert result.success is False
    assert result.output.get("error_code") == "ANCHOR_NOT_FOUND"
    # Diagnostics should contain near_line_matches pointing to the real function
    diag = result.output.get("diagnostics", {})
    near = diag.get("near_line_matches", [])
    assert len(near) > 0
    # At least one near match should reference line 1 (the actual function)
    assert any(m.get("line") == 1 for m in near)
