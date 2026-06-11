import sys
import types
import importlib
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

if "agentpress.adk_thread_manager" not in sys.modules:
    sys.modules["agentpress.adk_thread_manager"] = types.SimpleNamespace(
        ADKThreadManager=object,
    )

if "sandbox.tool_base" not in sys.modules:

    class _StubSandboxToolsBase:
        def __init__(
            self,
            project_id: str,
            thread_manager=None,
            sandbox_type: str = "code",
            **kwargs,
        ):
            del kwargs
            self.project_id = project_id
            self.thread_manager = thread_manager
            self.sandbox_type = sandbox_type
            self.workspace_path = "/workspace"
            self.sandbox = None

        @staticmethod
        def clean_path(path: str) -> str:
            normalized = str(path).strip()
            if normalized == "/workspace":
                return "."
            if normalized.startswith("/workspace/"):
                return normalized.removeprefix("/workspace/")
            return normalized.lstrip("/")

        async def _ensure_sandbox(self):
            return None

        async def _run_blocking_sandbox_call(
            self, _operation, call, timeout_seconds=None
        ):
            del timeout_seconds
            return call()

        async def _persist_workspace_artifact(
            self, path, content, source="", content_type=None
        ):
            del path, content, source, content_type
            return None

        async def _read_workspace_artifact_bytes(self, path):
            del path
            return None

        async def _list_workspace_artifact_entries(self, path):
            del path
            return []

        async def _sync_workspace_artifacts_from_sandbox(self, source=""):
            del source
            return None

        async def _emit_tool_status(self, *args, **kwargs):
            del args, kwargs
            return None

        async def _emit_write_file_status(self, *args, **kwargs):
            del args, kwargs
            return None

        async def _emit_guard_stage(self, *args, **kwargs):
            del args, kwargs
            return None

        def _should_apply_code_guard(self, path: str, content: str) -> bool:
            del path, content
            return False

        async def _prepare_python_write_validation(self, path, content):
            del path, content
            return {"syntax_ok": True}

        async def _finalize_python_write_validation(self, path, validation_payload):
            del path
            return validation_payload

    sys.modules["sandbox.tool_base"] = types.SimpleNamespace(
        SandboxToolsBase=_StubSandboxToolsBase,
    )


async def _noop(*args, **kwargs):
    del args, kwargs
    return 1


if "services.redis" not in sys.modules:
    sys.modules["services.redis"] = types.SimpleNamespace(
        rpush=_noop,
        expire=_noop,
        publish=_noop,
        REDIS_KEY_TTL=3600,
    )

_existing_sandbox_code_tool = sys.modules.get("agent.tools.sandbox_code_tool")
if _existing_sandbox_code_tool is not None and not hasattr(
    _existing_sandbox_code_tool,
    "_detect_fix_script_anti_pattern",
):
    del sys.modules["agent.tools.sandbox_code_tool"]
    agent_tools_package = sys.modules.get("agent.tools")
    if (
        agent_tools_package is not None
        and getattr(agent_tools_package, "sandbox_code_tool", None)
        is _existing_sandbox_code_tool
    ):
        delattr(agent_tools_package, "sandbox_code_tool")

sandbox_code_tool_module = importlib.import_module("agent.tools.sandbox_code_tool")


class _FakeCommands:
    def __init__(self, content):
        self._content = content
        self.commands = []

    def run(self, command: str, *args, **kwargs):
        del args, kwargs
        self.commands.append(command)
        if " && " in command and command.startswith("cd "):
            command = command.split(" && ", 1)[1]
        if command.startswith("stat -c %s "):
            if isinstance(self._content, (bytes, bytearray)):
                size = len(self._content)
            else:
                size = len(str(self._content).encode("utf-8", errors="replace"))
            return types.SimpleNamespace(exit_code=0, stdout=str(size), stderr="")

        if (
            command.startswith("head -c ")
            or command.startswith("dd if=")
            or command.startswith("grep ")
            or command.startswith("sed -i ")
            or command.startswith("cat ")
            or command.startswith("nl -ba ")
            or command.startswith("python ")
            or command.startswith("python3 ")
        ):
            return types.SimpleNamespace(exit_code=0, stdout=self._content, stderr="")

        if command.startswith("sed -n "):
            return types.SimpleNamespace(exit_code=0, stdout=self._content, stderr="")

        return types.SimpleNamespace(
            exit_code=127, stdout="", stderr="unsupported command"
        )


@pytest.mark.asyncio
async def test_read_file_returns_binary_safe_payload_for_png_bytes(monkeypatch):
    tool = sandbox_code_tool_module.SandboxCodeTool(
        project_id="project-1", thread_manager=None
    )
    png_like_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x07z\x03\xc4\x08\x06"
    tool.sandbox = types.SimpleNamespace(commands=_FakeCommands(png_like_bytes))

    async def _ensure_sandbox():
        return None

    async def _run_blocking_sandbox_call(_operation, call, timeout_seconds=None):
        del timeout_seconds
        return call()

    monkeypatch.setattr(tool, "_ensure_sandbox", _ensure_sandbox)
    monkeypatch.setattr(tool, "_run_blocking_sandbox_call", _run_blocking_sandbox_call)

    result = await tool.read_file(path="image.png")

    assert result.success is True
    assert result.output["is_binary"] is True
    assert result.output["content"] == ""
    assert "Binary file detected" in result.output["message"]


@pytest.mark.asyncio
async def test_read_file_keeps_text_preview_for_text_files(monkeypatch):
    tool = sandbox_code_tool_module.SandboxCodeTool(
        project_id="project-2", thread_manager=None
    )
    text_bytes = b"hello world\nsecond line\n"
    tool.sandbox = types.SimpleNamespace(commands=_FakeCommands(text_bytes))

    async def _ensure_sandbox():
        return None

    async def _run_blocking_sandbox_call(_operation, call, timeout_seconds=None):
        del timeout_seconds
        return call()

    monkeypatch.setattr(tool, "_ensure_sandbox", _ensure_sandbox)
    monkeypatch.setattr(tool, "_run_blocking_sandbox_call", _run_blocking_sandbox_call)

    result = await tool.read_file(path="notes.txt")

    assert result.success is True
    assert result.output["is_binary"] is False
    assert "hello world" in result.output["content"]


@pytest.mark.asyncio
async def test_read_file_supports_line_range_for_targeted_debugging(monkeypatch):
    tool = sandbox_code_tool_module.SandboxCodeTool(
        project_id="project-line-range", thread_manager=None
    )
    fake_commands = _FakeCommands("002 beta\n003 gamma\n")
    tool.sandbox = types.SimpleNamespace(commands=fake_commands)

    async def _ensure_sandbox():
        return None

    async def _run_blocking_sandbox_call(_operation, call, timeout_seconds=None):
        del timeout_seconds
        return call()

    monkeypatch.setattr(tool, "_ensure_sandbox", _ensure_sandbox)
    monkeypatch.setattr(tool, "_run_blocking_sandbox_call", _run_blocking_sandbox_call)

    result = await tool.read_file(
        path="demo.py",
        line_start=2,
        line_end=3,
        max_bytes=1000,
    )

    assert result.success is True
    assert result.output["line_start"] == 2
    assert result.output["line_end"] == 3
    assert result.output["offset_mode"] == "line_range"
    assert "002 beta" in result.output["content"]
    assert "003 gamma" in result.output["content"]
    assert "sed -n '2,3p'" in fake_commands.commands[-1]


@pytest.mark.asyncio
async def test_read_file_large_extraction_range_includes_completion_hint(monkeypatch):
    tool = sandbox_code_tool_module.SandboxCodeTool(
        project_id="project-large-extraction-range", thread_manager=None
    )
    text = "line\n" * 6000
    tool.sandbox = types.SimpleNamespace(commands=_FakeCommands(text))

    async def _ensure_sandbox():
        return None

    async def _run_blocking_sandbox_call(_operation, call, timeout_seconds=None):
        del timeout_seconds
        return call()

    monkeypatch.setattr(tool, "_ensure_sandbox", _ensure_sandbox)
    monkeypatch.setattr(tool, "_run_blocking_sandbox_call", _run_blocking_sandbox_call)

    result = await tool.read_file(
        path="full_extracted_text.txt",
        line_start=100,
        line_end=200,
        max_bytes=1000,
    )

    assert result.success is True
    assert "Document workflow reminder" in result.output["output"]
    assert "do not read every chunk" in result.output["output"]
    assert "write the requested deliverable" in result.output["output"]


@pytest.mark.asyncio
async def test_repeated_large_extraction_reads_escalate_to_stop_hint(monkeypatch):
    tool = sandbox_code_tool_module.SandboxCodeTool(
        project_id="project-large-extraction-repeat", thread_manager=None
    )
    text = "line\n" * 6000
    tool.sandbox = types.SimpleNamespace(commands=_FakeCommands(text))

    async def _ensure_sandbox():
        return None

    async def _run_blocking_sandbox_call(_operation, call, timeout_seconds=None):
        del timeout_seconds
        return call()

    monkeypatch.setattr(tool, "_ensure_sandbox", _ensure_sandbox)
    monkeypatch.setattr(tool, "_run_blocking_sandbox_call", _run_blocking_sandbox_call)

    await tool.read_file(path="all_extracted_questions.txt", line_start=1, line_end=100)
    await tool.read_file(
        path="all_extracted_questions.txt", line_start=101, line_end=200
    )
    result = await tool.read_file(
        path="all_extracted_questions.txt", line_start=201, line_end=300
    )

    assert result.success is True
    assert "STOP reading this file chunk by chunk" in result.output["output"]
    assert "write/verify the requested deliverable" in result.output["output"]


@pytest.mark.asyncio
async def test_write_file_repairs_ew_chart_token_loss_inside_html_script(monkeypatch):
    files = _FakeFiles()
    tool = sandbox_code_tool_module.SandboxCodeTool(
        project_id="project-html-token-repair", thread_manager=None
    )
    tool.sandbox = types.SimpleNamespace(files=files)

    async def _ensure_sandbox():
        return None

    async def _emit_write_file_status(*args, **kwargs):
        del args, kwargs
        return None

    async def _persist_workspace_artifact(*args, **kwargs):
        del args, kwargs
        return None

    monkeypatch.setattr(tool, "_ensure_sandbox", _ensure_sandbox)
    monkeypatch.setattr(tool, "_emit_write_file_status", _emit_write_file_status)
    monkeypatch.setattr(
        tool, "_persist_workspace_artifact", _persist_workspace_artifact, raising=False
    )

    html = "<html><body><h1>报告</h1><script>ew Chart(document.getElementById('c'), {type:'bar'});</script></body></html>"
    result = await tool.write_file(path="/workspace/report.html", content=html)

    assert result.success is True
    written = files.files["report.html"]
    assert "new Chart(document" in written
    assert "<script>ew Chart(document" not in written
    assert result.output["tokenizer_loss_repairs"] == ["ew Chart( -> new Chart("]


@pytest.mark.asyncio
async def test_execute_command_nameerror_new_ew_returns_tokenizer_loss_hint(
    monkeypatch,
):
    class _NameErrorCommands:
        def run(self, command: str, *args, **kwargs):
            del command, args, kwargs
            return types.SimpleNamespace(
                exit_code=1,
                stdout="",
                stderr="NameError: name 'new1' is not defined. Did you mean: 'ew1'?\n",
            )

    tool = sandbox_code_tool_module.SandboxCodeTool(
        project_id="project-nameerror-new-ew", thread_manager=None
    )
    tool.sandbox = types.SimpleNamespace(commands=_NameErrorCommands())

    async def _ensure_sandbox():
        return None

    async def _emit_tool_status(*args, **kwargs):
        del args, kwargs
        return None

    async def _run_blocking_sandbox_call(_operation, call, timeout_seconds=None):
        del _operation, timeout_seconds
        return call()

    async def _sync_workspace_artifacts_from_sandbox(*args, **kwargs):
        del args, kwargs
        return None

    monkeypatch.setattr(tool, "_ensure_sandbox", _ensure_sandbox)
    monkeypatch.setattr(tool, "_emit_tool_status", _emit_tool_status)
    monkeypatch.setattr(tool, "_run_blocking_sandbox_call", _run_blocking_sandbox_call)
    monkeypatch.setattr(
        tool,
        "_sync_workspace_artifacts_from_sandbox",
        _sync_workspace_artifacts_from_sandbox,
        raising=False,
    )

    result = await tool.execute_command(
        command="python3 << 'PY'\n\new1 = 'x'\nprint(new1)\nPY"
    )

    assert result.success is False
    assert result.output["error_code"] == "TOKENIZER_LOSS_SUSPECTED"
    assert "Tokenizer-sensitive expression fallback" in result.output["recovery_hint"]
    assert "3 failed attempts" in result.output["recovery_hint"]
    assert "rename or rephrase" in result.output["recovery_hint"]


@pytest.mark.asyncio
async def test_edit_file_noop_on_html_report_distinguishes_static_and_dynamic(
    monkeypatch,
):
    html = "<html><body><h1>胡宜含错题报告</h1><p>static report layer exists</p><script>new Chart()</script></body></html>"
    files = _FakeFiles({"/workspace/report.html": html})
    tool = sandbox_code_tool_module.SandboxCodeTool(
        project_id="project-html-noop", thread_manager=None
    )
    tool.sandbox = types.SimpleNamespace(files=files)

    async def _ensure_sandbox():
        return None

    async def _run_blocking_sandbox_call(_operation, call, timeout_seconds=None):
        del _operation, timeout_seconds
        return call()

    monkeypatch.setattr(tool, "_ensure_sandbox", _ensure_sandbox)
    monkeypatch.setattr(tool, "_run_blocking_sandbox_call", _run_blocking_sandbox_call)

    result = await tool.edit_file(
        path="/workspace/report.html",
        old_text="new Chart()",
        new_text="new Chart()",
    )

    assert result.success is False
    assert result.output["error_code"] == "EDIT_FILE_NOOP"
    assert "static report layer" in result.output["error"]
    assert "dynamic/interactive layer" in result.output["error"]
    assert "If the user explicitly asked for dynamic" in result.output["error"]


@pytest.mark.asyncio
async def test_write_file_rejects_empty_content_before_sandbox(monkeypatch):
    tool = sandbox_code_tool_module.SandboxCodeTool(
        project_id="project-3", thread_manager=None
    )

    ensure_called = {"value": False}
    captured_status = []

    async def _ensure_sandbox():
        ensure_called["value"] = True
        return None

    async def _emit_write_file_status(status_type, path, *, message=None, extra=None):
        captured_status.append(
            {
                "status_type": status_type,
                "path": path,
                "message": message,
                "extra": extra or {},
            }
        )

    monkeypatch.setattr(tool, "_ensure_sandbox", _ensure_sandbox)
    monkeypatch.setattr(tool, "_emit_write_file_status", _emit_write_file_status)

    result = await tool.write_file(path="demo.py", content="")

    assert result.success is False
    assert result.output["error_code"] == "WRITE_FILE_INCOMPLETE_PAYLOAD"
    assert ensure_called["value"] is False
    assert captured_status
    assert captured_status[-1]["status_type"] == "tool_failed"
    assert captured_status[-1]["extra"]["exec_gate_reason"] == "empty_content"


class _FakeFiles:
    def __init__(self, files=None):
        self.files = {
            str(k).removeprefix("/workspace/"): v for k, v in dict(files or {}).items()
        }
        self.writes = []

    def read(self, path, *args, **kwargs):
        del args, kwargs
        key = str(path).removeprefix("/workspace/")
        if key not in self.files:
            raise FileNotFoundError(path)
        return self.files[key]

    def write(self, path, content):
        self.writes.append((path, content))
        self.files[str(path).removeprefix("/workspace/")] = content
        return None

    def list(self, path):
        raw = str(path)
        prefix = (
            "" if raw == "/workspace" else raw.removeprefix("/workspace/").strip("/")
        )
        entries = []
        for key in self.files:
            if prefix and not key.startswith(prefix + "/"):
                continue
            name = key[len(prefix) + 1 :] if prefix else key
            if "/" in name:
                continue
            entries.append({"name": name, "path": f"/workspace/{key}"})
        return entries


class _WriteInvisibleFakeFiles(_FakeFiles):
    def write(self, path, content):
        self.writes.append((path, content))
        return None


def _large_python_builder(label: str) -> str:
    body = "\n".join(f"SLIDE_{index} = {index!r}" for index in range(700))
    return f"#!/usr/bin/env python3\n# {label}\nOUTPUT = '/workspace/ds_real_deck.pptx'\n{body}\n"


@pytest.mark.asyncio
async def test_write_file_blocks_rewrite_of_existing_workspace_file(monkeypatch):
    original = _large_python_builder("original")
    replacement = _large_python_builder("replacement")
    files = _FakeFiles({"/workspace/ds_real_deck_builder.py": original})
    tool = sandbox_code_tool_module.SandboxCodeTool(
        project_id="project-rewrite-same", thread_manager=None
    )
    tool.sandbox = types.SimpleNamespace(files=files)
    captured_status = []

    async def _ensure_sandbox():
        return None

    async def _emit_write_file_status(status_type, path, *, message=None, extra=None):
        captured_status.append(
            {
                "status_type": status_type,
                "path": path,
                "message": message,
                "extra": extra or {},
            }
        )

    monkeypatch.setattr(tool, "_ensure_sandbox", _ensure_sandbox)
    monkeypatch.setattr(tool, "_emit_write_file_status", _emit_write_file_status)
    monkeypatch.setattr(tool, "_persist_workspace_artifact", _noop, raising=False)

    result = await tool.write_file(
        path="/workspace/ds_real_deck_builder.py",
        content=replacement,
    )

    assert result.success is False
    assert result.output["error_code"] == "WORKSPACE_REWRITE_BLOCKED"
    assert "edit_file" in result.output["error"]
    assert "read_file" in result.output["error"]
    assert "Do not rewrite" in result.output["error"]
    assert files.files["ds_real_deck_builder.py"] == original
    assert files.writes == []
    assert captured_status[-1]["status_type"] == "tool_rejected_rewrite"


@pytest.mark.asyncio
async def test_write_file_allows_overwriting_generated_report_deliverable(monkeypatch):
    partial = "# CIE A2 Economics 错题分析报告\n\n## 一、概览\n\n草稿\n"
    complete = (
        "# CIE A2 Economics 错题分析报告\n\n"
        "本报告围绕错题、知识点和复习建议展开。\n\n"
        "| 试卷 | 错题 |\n|---|---|\n"
        + "\n".join(f"| 9708_s24_qp_33 | Q{i} 错题分析 |" for i in range(80))
        + "\n\n## 复习建议\n请按模块复盘。"
    )
    files = _FakeFiles({"/workspace/wrong_questions_analysis.md": partial})
    tool = sandbox_code_tool_module.SandboxCodeTool(
        project_id="project-report-overwrite", thread_manager=None
    )
    tool.sandbox = types.SimpleNamespace(files=files)

    async def _ensure_sandbox():
        return None

    async def _emit_write_file_status(*args, **kwargs):
        del args, kwargs
        return None

    monkeypatch.setattr(tool, "_ensure_sandbox", _ensure_sandbox)
    monkeypatch.setattr(tool, "_emit_write_file_status", _emit_write_file_status)
    monkeypatch.setattr(tool, "_persist_workspace_artifact", _noop, raising=False)

    result = await tool.write_file(
        path="/workspace/wrong_questions_analysis.md",
        content=complete,
    )

    assert result.success is True
    assert files.files["wrong_questions_analysis.md"] == complete
    assert files.writes == [("/workspace/wrong_questions_analysis.md", complete)]


@pytest.mark.asyncio
async def test_write_file_fails_if_written_file_is_not_visible_in_sandbox(monkeypatch):
    files = _WriteInvisibleFakeFiles()
    tool = sandbox_code_tool_module.SandboxCodeTool(
        project_id="project-write-invisible", thread_manager=None
    )
    tool.sandbox = types.SimpleNamespace(files=files)
    persisted = []
    captured_status = []

    async def _ensure_sandbox():
        return None

    async def _emit_write_file_status(status_type, path, *, message=None, extra=None):
        captured_status.append(
            {
                "status_type": status_type,
                "path": path,
                "message": message,
                "extra": extra or {},
            }
        )

    async def _persist_workspace_artifact(path, content, source="", content_type=None):
        persisted.append((path, content, source, content_type))

    monkeypatch.setattr(tool, "_ensure_sandbox", _ensure_sandbox)
    monkeypatch.setattr(tool, "_emit_write_file_status", _emit_write_file_status)
    monkeypatch.setattr(
        tool, "_persist_workspace_artifact", _persist_workspace_artifact, raising=False
    )

    result = await tool.write_file(
        path="/workspace/generated.py",
        content="print('ok')\n",
    )

    assert result.success is False
    assert result.output["error_code"] == "SANDBOX_FILE_NOT_VISIBLE"
    assert "not visible in the live sandbox" in result.output["error"]
    assert (
        "do not write expressions with repeated adjacent letters or digits"
        in result.output["error"]
    )
    assert files.writes == [("/workspace/generated.py", "print('ok')\n")]
    assert persisted == []
    assert captured_status[-1]["status_type"] == "tool_failed"


@pytest.mark.asyncio
async def test_write_file_blocks_same_content_rewrite_under_new_workspace_filename(
    monkeypatch,
):
    original = _large_python_builder("original")
    renamed_rewrite = original.replace("# original", "# rewritten under another name")
    files = _FakeFiles({"/workspace/ds_real_deck_builder.py": original})
    tool = sandbox_code_tool_module.SandboxCodeTool(
        project_id="project-rewrite-renamed", thread_manager=None
    )
    tool.sandbox = types.SimpleNamespace(files=files)
    captured_status = []

    async def _ensure_sandbox():
        return None

    async def _emit_write_file_status(status_type, path, *, message=None, extra=None):
        captured_status.append(
            {
                "status_type": status_type,
                "path": path,
                "message": message,
                "extra": extra or {},
            }
        )

    monkeypatch.setattr(tool, "_ensure_sandbox", _ensure_sandbox)
    monkeypatch.setattr(tool, "_emit_write_file_status", _emit_write_file_status)
    monkeypatch.setattr(tool, "_persist_workspace_artifact", _noop, raising=False)

    result = await tool.write_file(
        path="/workspace/ds_real_deck_builder_v2.py",
        content=renamed_rewrite,
    )

    assert result.success is False
    assert result.output["error_code"] == "WORKSPACE_REWRITE_BLOCKED"
    assert result.output["rewrite_target_path"] == "/workspace/ds_real_deck_builder.py"
    assert "edit_file" in result.output["error"]
    assert "Do not rewrite" in result.output["error"]
    assert "/workspace/ds_real_deck_builder.py" in result.output["error"]
    assert "ds_real_deck_builder_v2.py" not in files.files
    assert files.writes == []
    assert captured_status[-1]["status_type"] == "tool_rejected_rewrite"


@pytest.mark.asyncio
async def test_execute_command_blocks_helper_script_creation(monkeypatch):
    tool = sandbox_code_tool_module.SandboxCodeTool(
        project_id="project-command-helper-create", thread_manager=None
    )
    tool.sandbox = types.SimpleNamespace(commands=_FakeCommands(""))
    captured_status = []

    async def _ensure_sandbox():
        return None

    async def _emit_tool_status(
        status_type, tool_name, *, arguments=None, message=None, extra=None
    ):
        captured_status.append(
            {
                "status_type": status_type,
                "tool_name": tool_name,
                "arguments": arguments or {},
                "message": message,
                "extra": extra or {},
            }
        )

    monkeypatch.setattr(tool, "_ensure_sandbox", _ensure_sandbox)
    monkeypatch.setattr(tool, "_emit_tool_status", _emit_tool_status)

    result = await tool.execute_command(
        command=(
            "cat > /workspace/_fix.py << 'PYEOF'\n"
            "open('/workspace/ds_real_deck_builder.py', 'w').write('x')\n"
            "PYEOF\n"
            "python3 /workspace/_fix.py"
        )
    )

    assert result.success is False
    assert result.output["error_code"] == "HELPER_SCRIPT_COMMAND_BLOCKED"
    assert "edit_file" in result.output["error"]
    assert "/workspace/ds_real_deck_builder.py" in result.output["error"]
    assert captured_status[-1]["status_type"] == "tool_rejected_helper_script"


@pytest.mark.asyncio
async def test_execute_command_blocks_heredoc_script_creation_even_without_helper_name(
    monkeypatch,
):
    tool = sandbox_code_tool_module.SandboxCodeTool(
        project_id="project-command-heredoc-create", thread_manager=None
    )
    tool.sandbox = types.SimpleNamespace(commands=_FakeCommands(""))

    async def _ensure_sandbox():
        return None

    async def _emit_tool_status(*args, **kwargs):
        del args, kwargs
        return None

    async def _sync_workspace_artifacts_from_sandbox(*args, **kwargs):
        del args, kwargs
        return None

    monkeypatch.setattr(tool, "_ensure_sandbox", _ensure_sandbox)
    monkeypatch.setattr(tool, "_emit_tool_status", _emit_tool_status)
    monkeypatch.setattr(
        tool,
        "_sync_workspace_artifacts_from_sandbox",
        _sync_workspace_artifacts_from_sandbox,
        raising=False,
    )

    result = await tool.execute_command(
        command=(
            "cat > /workspace/generate_grading_report.py << 'PY'\n"
            "from docx import Document\n"
            "Document().save('/workspace/report.docx')\n"
            "PY"
        )
    )

    assert result.success is False
    assert result.output["error_code"] == "LONG_SCRIPT_COMMAND_BLOCKED"
    assert "write_file" in result.output["error"]
    assert 'execute_command("python /workspace/script.py")' in result.output["error"]


@pytest.mark.asyncio
async def test_execute_command_blocks_python3_heredoc_workspace_write_without_dash(
    monkeypatch,
):
    tool = sandbox_code_tool_module.SandboxCodeTool(
        project_id="project-command-python3-heredoc-write", thread_manager=None
    )
    tool.sandbox = types.SimpleNamespace(commands=_FakeCommands(""))

    async def _ensure_sandbox():
        return None

    async def _emit_tool_status(*args, **kwargs):
        del args, kwargs
        return None

    monkeypatch.setattr(tool, "_ensure_sandbox", _ensure_sandbox)
    monkeypatch.setattr(tool, "_emit_tool_status", _emit_tool_status)

    result = await tool.execute_command(
        command=(
            "python3 << 'PYEOF'\n"
            "with open('/workspace/grade_report.py', 'w') as f:\n"
            "    f.write('from docx import Document\\n')\n"
            "PYEOF"
        )
    )

    assert result.success is False
    assert result.output["error_code"] == "LONG_SCRIPT_COMMAND_BLOCKED"
    assert "heredocs" in result.output["error"]
    assert "write_file" in result.output["error"]
    assert "minimal DOCX template" in result.output["error"]
    assert "Do not use Word style names" in result.output["error"]


@pytest.mark.asyncio
async def test_execute_command_allows_short_python_heredoc_for_pdf_report_work(
    monkeypatch,
):
    tool = sandbox_code_tool_module.SandboxCodeTool(
        project_id="project-command-short-python-heredoc-report", thread_manager=None
    )
    fake_commands = _FakeCommands("found 10 papers\n")
    tool.sandbox = types.SimpleNamespace(commands=fake_commands)

    async def _ensure_sandbox():
        return None

    async def _emit_tool_status(*args, **kwargs):
        del args, kwargs
        return None

    async def _run_blocking_sandbox_call(_operation, call, timeout_seconds=None):
        del _operation, timeout_seconds
        return call()

    async def _sync_workspace_artifacts_from_sandbox(*args, **kwargs):
        del args, kwargs
        return None

    monkeypatch.setattr(tool, "_ensure_sandbox", _ensure_sandbox)
    monkeypatch.setattr(tool, "_emit_tool_status", _emit_tool_status)
    monkeypatch.setattr(tool, "_run_blocking_sandbox_call", _run_blocking_sandbox_call)
    monkeypatch.setattr(
        tool,
        "_sync_workspace_artifacts_from_sandbox",
        _sync_workspace_artifacts_from_sandbox,
        raising=False,
    )

    command = (
        "python3 << 'PY'\n"
        "from pathlib import Path\n"
        "papers = sorted(Path('/workspace').glob('9708_*_qp_*.pdf'))\n"
        "text = 'found ' + str(len(papers)) + ' papers\\n'\n"
        "Path('/workspace/wrong_mcq_report.md').write_text(text)\n"
        "Path('/workspace/wrong_mcq_report.html').write_text('<html><body>' + text + '</body></html>')\n"
        "print(text)\n"
        "PY"
    )

    result = await tool.execute_command(command=command)

    assert result.success is True
    assert result.output["exit_code"] == 0
    assert fake_commands.commands
    assert "python3 << 'PY'" in fake_commands.commands[-1]


class _SyntaxErrorFakeCommands:
    def run(self, command: str, *args, **kwargs):
        del command, args, kwargs
        return types.SimpleNamespace(
            exit_code=1,
            stdout="",
            stderr='  File "<stdin>", line 4\nSyntaxError: f-string: single } is not allowed\n',
        )


@pytest.mark.asyncio
async def test_execute_command_python_syntax_error_redirects_exploratory_document_work(
    monkeypatch,
):
    tool = sandbox_code_tool_module.SandboxCodeTool(
        project_id="project-python-syntax-doc", thread_manager=None
    )
    tool.sandbox = types.SimpleNamespace(commands=_SyntaxErrorFakeCommands())

    async def _ensure_sandbox():
        return None

    async def _emit_tool_status(*args, **kwargs):
        del args, kwargs
        return None

    async def _run_blocking_sandbox_call(_operation, call, timeout_seconds=None):
        del _operation, timeout_seconds
        return call()

    async def _sync_workspace_artifacts_from_sandbox(*args, **kwargs):
        del args, kwargs
        return None

    monkeypatch.setattr(tool, "_ensure_sandbox", _ensure_sandbox)
    monkeypatch.setattr(tool, "_emit_tool_status", _emit_tool_status)
    monkeypatch.setattr(tool, "_run_blocking_sandbox_call", _run_blocking_sandbox_call)
    monkeypatch.setattr(
        tool,
        "_sync_workspace_artifacts_from_sandbox",
        _sync_workspace_artifacts_from_sandbox,
        raising=False,
    )

    result = await tool.execute_command(
        command="python3 << 'PY'\ntext = open('extracted/questions.txt').read()\nprint(f'{broken}')\nPY"
    )

    assert result.success is False
    assert result.output["error_code"] == "PYTHON_EXPLORATORY_SYNTAX_REDIRECT"
    assert "exploratory document extraction" in result.output["recovery_hint"]
    assert "write the requested deliverable" in result.output["output"]


@pytest.mark.asyncio
async def test_execute_command_python_c_syntax_error_redirects_wrong_mcq_report_work(
    monkeypatch,
):
    tool = sandbox_code_tool_module.SandboxCodeTool(
        project_id="project-python-c-syntax-report", thread_manager=None
    )
    tool.sandbox = types.SimpleNamespace(commands=_SyntaxErrorFakeCommands())

    async def _ensure_sandbox():
        return None

    async def _emit_tool_status(*args, **kwargs):
        del args, kwargs
        return None

    async def _run_blocking_sandbox_call(_operation, call, timeout_seconds=None):
        del _operation, timeout_seconds
        return call()

    async def _sync_workspace_artifacts_from_sandbox(*args, **kwargs):
        del args, kwargs
        return None

    monkeypatch.setattr(tool, "_ensure_sandbox", _ensure_sandbox)
    monkeypatch.setattr(tool, "_emit_tool_status", _emit_tool_status)
    monkeypatch.setattr(tool, "_run_blocking_sandbox_call", _run_blocking_sandbox_call)
    monkeypatch.setattr(
        tool,
        "_sync_workspace_artifacts_from_sandbox",
        _sync_workspace_artifacts_from_sandbox,
        raising=False,
    )

    result = await tool.execute_command(
        command=(
            "python3 -c \"papers=['9708_s24_qp_33']; "
            "print('wrong mcq report for /workspace PDFs')\""
        )
    )

    assert result.success is False
    assert result.output["error_code"] == "PYTHON_EXPLORATORY_SYNTAX_REDIRECT"
    assert "write the requested deliverable" in result.output["output"]


@pytest.mark.asyncio
async def test_execute_command_blocks_pdf_dependency_install_for_document_tasks(
    monkeypatch,
):
    tool = sandbox_code_tool_module.SandboxCodeTool(
        project_id="project-pdf-install-block", thread_manager=None
    )
    tool.sandbox = types.SimpleNamespace(commands=_FakeCommands(""))

    async def _ensure_sandbox():
        return None

    async def _emit_tool_status(*args, **kwargs):
        del args, kwargs
        return None

    monkeypatch.setattr(tool, "_ensure_sandbox", _ensure_sandbox)
    monkeypatch.setattr(tool, "_emit_tool_status", _emit_tool_status)

    result = await tool.execute_command(
        command="pip install pdfplumber pypdf 2>&1 | tail -5",
        timeout=120,
    )

    assert result.success is False
    assert (
        result.output["error_code"] == "DOCUMENT_EXTRACTION_DEPENDENCY_INSTALL_REDIRECT"
    )
    assert "pdftotext" in result.output["error"]
    assert "Installing packages is slow" in result.output["error"]


class _MissingPdfPackageCommands:
    def run(self, command: str, *args, **kwargs):
        del command, args, kwargs
        return types.SimpleNamespace(
            exit_code=1,
            stdout="",
            stderr=(
                "Traceback (most recent call last):\n"
                '  File "<stdin>", line 1, in <module>\n'
                "ModuleNotFoundError: No module named 'pdfplumber'\n"
            ),
        )


@pytest.mark.asyncio
async def test_execute_command_missing_pdf_package_redirects_to_pdftotext(monkeypatch):
    tool = sandbox_code_tool_module.SandboxCodeTool(
        project_id="project-pdf-import-redirect", thread_manager=None
    )
    tool.sandbox = types.SimpleNamespace(commands=_MissingPdfPackageCommands())

    async def _ensure_sandbox():
        return None

    async def _emit_tool_status(*args, **kwargs):
        del args, kwargs
        return None

    async def _run_blocking_sandbox_call(_operation, call, timeout_seconds=None):
        del _operation, timeout_seconds
        return call()

    async def _sync_workspace_artifacts_from_sandbox(*args, **kwargs):
        del args, kwargs
        return None

    monkeypatch.setattr(tool, "_ensure_sandbox", _ensure_sandbox)
    monkeypatch.setattr(tool, "_emit_tool_status", _emit_tool_status)
    monkeypatch.setattr(tool, "_run_blocking_sandbox_call", _run_blocking_sandbox_call)
    monkeypatch.setattr(
        tool,
        "_sync_workspace_artifacts_from_sandbox",
        _sync_workspace_artifacts_from_sandbox,
        raising=False,
    )

    result = await tool.execute_command(
        command=(
            "python3 << 'PY'\n"
            "import pdfplumber\n"
            "print('extract 9708_s24_qp_33.pdf from /workspace')\n"
            "PY"
        )
    )

    assert result.success is False
    assert result.output["error_code"] == "PDF_EXTRACTION_DEPENDENCY_REDIRECT"
    assert "do not install missing PDF packages" in result.output["recovery_hint"]
    assert "pdftotext /workspace/file.pdf -" in result.output["output"]


@pytest.mark.asyncio
async def test_execute_command_redirects_repeated_document_inspection_loop(
    monkeypatch,
):
    tool = sandbox_code_tool_module.SandboxCodeTool(
        project_id="project-document-loop", thread_manager=None
    )
    fake_commands = _FakeCommands("question text\n")
    tool.sandbox = types.SimpleNamespace(commands=fake_commands)

    async def _ensure_sandbox():
        return None

    async def _emit_tool_status(*args, **kwargs):
        del args, kwargs
        return None

    async def _run_blocking_sandbox_call(_operation, call, timeout_seconds=None):
        del _operation, timeout_seconds
        return call()

    async def _sync_workspace_artifacts_from_sandbox(*args, **kwargs):
        del args, kwargs
        return None

    monkeypatch.setattr(tool, "_ensure_sandbox", _ensure_sandbox)
    monkeypatch.setattr(tool, "_emit_tool_status", _emit_tool_status)
    monkeypatch.setattr(tool, "_run_blocking_sandbox_call", _run_blocking_sandbox_call)
    monkeypatch.setattr(
        tool,
        "_sync_workspace_artifacts_from_sandbox",
        _sync_workspace_artifacts_from_sandbox,
        raising=False,
    )
    monkeypatch.setattr(
        sandbox_code_tool_module,
        "DOCUMENT_INSPECTION_REDIRECT_AFTER",
        2,
    )

    cmd = "grep -n '^[0-9]' /workspace/extracted_text/source_document.txt | head"
    first = await tool.execute_command(command=cmd)
    second = await tool.execute_command(command=cmd)
    third = await tool.execute_command(command=cmd)

    assert first.success is True
    assert second.success is True
    assert third.success is False
    assert third.output["error_code"] == "DOCUMENT_INSPECTION_LOOP_REDIRECT"
    assert "write the requested deliverable(s) directly" in third.output["error"]


def test_python_pdf_extraction_counts_as_document_inspection() -> None:
    assert sandbox_code_tool_module._looks_like_document_inspection_command(
        "python3 << 'PY'\n"
        "import pypdf\n"
        "r=pypdf.PdfReader('/workspace/source_document.pdf')\n"
        "PY"
    )
    assert not sandbox_code_tool_module._looks_like_document_inspection_command(
        "python3 << 'PY'\n"
        "open('/workspace/report.html','w').write('<html></html>')\n"
        "PY"
    )
    assert sandbox_code_tool_module._looks_like_document_inspection_command(
        "awk '/^12 /{print}' /workspace/txt_source_document.txt"
    )
    assert sandbox_code_tool_module._looks_like_document_inspection_command(
        "pdftotext /workspace/9708_m23_qp_32.pdf - > /workspace/txt_9708_m23_qp_32.txt"
    )
    assert sandbox_code_tool_module._looks_like_document_inspection_command(
        "grep -n '^12 ' /workspace/txt_9708_m23_qp_32.txt > /workspace/all_extracts.json"
    )
    assert sandbox_code_tool_module._looks_like_document_inspection_command(
        "cd /tmp && grep -A 15 '^17 ' m24_32.txt | head -20"
    )
    assert not sandbox_code_tool_module._looks_like_document_inspection_command(
        "cat /tmp/report.html > /workspace/wrong_mcq_report.html"
    )


def test_extracted_text_path_counts_as_document_inspection() -> None:
    assert sandbox_code_tool_module._looks_like_document_inspection_path(
        "/workspace/pdf_text/source_document.txt"
    )
    assert not sandbox_code_tool_module._looks_like_document_inspection_path(
        "/workspace/student_report.html"
    )


@pytest.mark.asyncio
async def test_execute_command_redirects_repeated_intermediate_helper_execution(
    monkeypatch,
):
    tool = sandbox_code_tool_module.SandboxCodeTool(
        project_id="project-helper-execution-loop", thread_manager=None
    )
    fake_commands = _FakeCommands("helper output\n")
    tool.sandbox = types.SimpleNamespace(commands=fake_commands)

    async def _ensure_sandbox():
        return None

    async def _emit_tool_status(*args, **kwargs):
        del args, kwargs
        return None

    async def _run_blocking_sandbox_call(_operation, call, timeout_seconds=None):
        del _operation, timeout_seconds
        return call()

    async def _sync_workspace_artifacts_from_sandbox(*args, **kwargs):
        del args, kwargs
        return None

    monkeypatch.setattr(tool, "_ensure_sandbox", _ensure_sandbox)
    monkeypatch.setattr(tool, "_emit_tool_status", _emit_tool_status)
    monkeypatch.setattr(tool, "_run_blocking_sandbox_call", _run_blocking_sandbox_call)
    monkeypatch.setattr(
        tool,
        "_sync_workspace_artifacts_from_sandbox",
        _sync_workspace_artifacts_from_sandbox,
        raising=False,
    )

    cmd = "cd /workspace && python3 extract_questions.py 2>&1 | head -20"
    first = await tool.execute_command(command=cmd)
    second = await tool.execute_command(command=cmd)
    third = await tool.execute_command(command=cmd)

    assert first.success is True
    assert second.success is True
    assert third.success is False
    assert third.output["error_code"] == "INTERMEDIATE_HELPER_EXECUTION_REDIRECT"
    assert "/workspace/extract_questions.py" in third.output["error"]
    assert "write the requested deliverable directly" in third.output["error"]


@pytest.mark.asyncio
async def test_execute_command_blocks_inline_python_workspace_write(monkeypatch):
    tool = sandbox_code_tool_module.SandboxCodeTool(
        project_id="project-command-python-c-write", thread_manager=None
    )
    tool.sandbox = types.SimpleNamespace(commands=_FakeCommands(""))

    async def _ensure_sandbox():
        return None

    async def _emit_tool_status(*args, **kwargs):
        del args, kwargs
        return None

    monkeypatch.setattr(tool, "_ensure_sandbox", _ensure_sandbox)
    monkeypatch.setattr(tool, "_emit_tool_status", _emit_tool_status)

    result = await tool.execute_command(
        command=(
            "python3 -c \"p='/workspace/generate_grading_report.py'; "
            "open(p, 'w').write('print(1)')\""
        )
    )

    assert result.success is False
    assert result.output["error_code"] == "LONG_SCRIPT_COMMAND_BLOCKED"
    assert "python -c" in result.output["error"]
    assert "edit_file" in result.output["error"]


@pytest.mark.asyncio
async def test_execute_command_allows_grep_sed_n_and_correct_sed_i(monkeypatch):
    tool = sandbox_code_tool_module.SandboxCodeTool(
        project_id="project-command-read-and-sed", thread_manager=None
    )
    fake_commands = _FakeCommands("111:add_thin_border_table(sumary_table)\n")
    tool.sandbox = types.SimpleNamespace(commands=fake_commands)

    async def _ensure_sandbox():
        return None

    async def _emit_tool_status(*args, **kwargs):
        del args, kwargs
        return None

    async def _sync_workspace_artifacts_from_sandbox(*args, **kwargs):
        del args, kwargs
        return None

    monkeypatch.setattr(tool, "_ensure_sandbox", _ensure_sandbox)
    monkeypatch.setattr(tool, "_emit_tool_status", _emit_tool_status)
    monkeypatch.setattr(
        tool,
        "_sync_workspace_artifacts_from_sandbox",
        _sync_workspace_artifacts_from_sandbox,
        raising=False,
    )

    grep = await tool.execute_command(
        command="grep -n 'sumary\\|summary' /workspace/generate_grading_report.py"
    )
    sed_n = await tool.execute_command(
        command="sed -n '109,113p' /workspace/generate_grading_report.py"
    )
    sed_i = await tool.execute_command(
        command=(
            "sed -i 's/sumary_table/summary_table/g' "
            "/workspace/generate_grading_report.py"
        )
    )

    assert grep.success is True
    assert sed_n.success is True
    assert sed_i.success is True
    assert grep.output["exit_code"] == 0
    assert sed_n.output["exit_code"] == 0
    assert sed_i.output["exit_code"] == 0
    assert len(fake_commands.commands) == 3


@pytest.mark.asyncio
async def test_execute_command_blocks_running_workspace_helper_script(monkeypatch):
    tool = sandbox_code_tool_module.SandboxCodeTool(
        project_id="project-command-helper-run", thread_manager=None
    )
    tool.sandbox = types.SimpleNamespace(commands=_FakeCommands(""))

    async def _ensure_sandbox():
        return None

    async def _emit_tool_status(*args, **kwargs):
        del args, kwargs
        return None

    monkeypatch.setattr(tool, "_ensure_sandbox", _ensure_sandbox)
    monkeypatch.setattr(tool, "_emit_tool_status", _emit_tool_status)

    result = await tool.execute_command(
        command="python3 /workspace/_fix_acent.py && python3 -c 'import ds_real_deck_builder'"
    )

    assert result.success is False
    assert result.output["error_code"] == "HELPER_SCRIPT_COMMAND_BLOCKED"
    assert "/workspace/_fix_acent.py" in result.output["error"]
    assert "edit_file" in result.output["error"]


@pytest.mark.asyncio
async def test_execute_command_blocks_workspace_delete_rewrite_bypass(monkeypatch):
    tool = sandbox_code_tool_module.SandboxCodeTool(
        project_id="project-command-delete-rewrite-bypass", thread_manager=None
    )
    tool.sandbox = types.SimpleNamespace(commands=_FakeCommands(""))

    async def _ensure_sandbox():
        return None

    async def _emit_tool_status(*args, **kwargs):
        del args, kwargs
        return None

    monkeypatch.setattr(tool, "_ensure_sandbox", _ensure_sandbox)
    monkeypatch.setattr(tool, "_emit_tool_status", _emit_tool_status)

    result = await tool.execute_command(
        command="rm /workspace/ds_real_deck_builder.py && echo deleted"
    )

    assert result.success is False
    assert result.output["error_code"] == "WORKSPACE_DELETE_REWRITE_BLOCKED"
    assert "/workspace/ds_real_deck_builder.py" in result.output["error"]
    assert "edit_file" in result.output["error"]


@pytest.mark.asyncio
async def test_execute_command_blocks_noop_sed_in_place_loop(monkeypatch):
    tool = sandbox_code_tool_module.SandboxCodeTool(
        project_id="project-command-noop-sed", thread_manager=None
    )
    tool.sandbox = types.SimpleNamespace(commands=_FakeCommands(""))

    async def _ensure_sandbox():
        return None

    async def _emit_tool_status(*args, **kwargs):
        del args, kwargs
        return None

    monkeypatch.setattr(tool, "_ensure_sandbox", _ensure_sandbox)
    monkeypatch.setattr(tool, "_emit_tool_status", _emit_tool_status)

    result = await tool.execute_command(
        command="sed -i 's/MIDLE/MIDLE/g' /workspace/ds_real_deck_builder.py"
    )

    assert result.success is False
    assert result.output["error_code"] == "NOOP_IN_PLACE_EDIT_BLOCKED"
    assert "no-op" in result.output["error"].lower()
    assert "edit_file" in result.output["error"]
    assert "fix_quotes.py" in result.output["error"]
    assert "do not write" in result.output["error"].lower()


@pytest.mark.asyncio
async def test_execute_command_blocks_noop_sed_after_cd_workspace(monkeypatch):
    tool = sandbox_code_tool_module.SandboxCodeTool(
        project_id="project-command-noop-sed-cd", thread_manager=None
    )
    tool.sandbox = types.SimpleNamespace(commands=_FakeCommands(""))

    async def _ensure_sandbox():
        return None

    async def _emit_tool_status(*args, **kwargs):
        del args, kwargs
        return None

    monkeypatch.setattr(tool, "_ensure_sandbox", _ensure_sandbox)
    monkeypatch.setattr(tool, "_emit_tool_status", _emit_tool_status)

    result = await tool.execute_command(
        command="cd /workspace && sed -i 's/bulets/bulets/g' build_report.py && python build_report.py"
    )

    assert result.success is False
    assert result.output["error_code"] == "NOOP_IN_PLACE_EDIT_BLOCKED"
    assert result.output["target_path"] == "/workspace/build_report.py"
    assert "old_text and new_text are identical" in result.output["error"]


@pytest.mark.asyncio
async def test_execute_command_blocks_noop_sed_via_shell_variable(monkeypatch):
    tool = sandbox_code_tool_module.SandboxCodeTool(
        project_id="project-command-noop-sed-variable", thread_manager=None
    )
    tool.sandbox = types.SimpleNamespace(commands=_FakeCommands(""))

    async def _ensure_sandbox():
        return None

    async def _emit_tool_status(*args, **kwargs):
        del args, kwargs
        return None

    monkeypatch.setattr(tool, "_ensure_sandbox", _ensure_sandbox)
    monkeypatch.setattr(tool, "_emit_tool_status", _emit_tool_status)

    result = await tool.execute_command(
        command="cd /workspace && S='s/bulets/bulets/g' && sed -i \"$S\" build_report.py && python build_report.py"
    )

    assert result.success is False
    assert result.output["error_code"] == "NOOP_IN_PLACE_EDIT_BLOCKED"
    assert result.output["target_path"] == "/workspace/build_report.py"
    assert "bulets" in result.output["error"]


@pytest.mark.asyncio
async def test_second_extraction_helper_creation_redirects_to_deliverable(monkeypatch):
    files = _FakeFiles()
    tool = sandbox_code_tool_module.SandboxCodeTool(
        project_id="project-second-extract-helper", thread_manager=None
    )
    tool.sandbox = types.SimpleNamespace(files=files)

    async def _ensure_sandbox():
        return None

    async def _emit_write_file_status(*args, **kwargs):
        del args, kwargs
        return None

    async def _persist_workspace_artifact(*args, **kwargs):
        del args, kwargs
        return None

    monkeypatch.setattr(tool, "_ensure_sandbox", _ensure_sandbox)
    monkeypatch.setattr(tool, "_emit_write_file_status", _emit_write_file_status)
    monkeypatch.setattr(
        tool, "_persist_workspace_artifact", _persist_workspace_artifact, raising=False
    )

    first = await tool.write_file(
        path="/workspace/extract_questions.py", content="print('first')\n"
    )
    second = await tool.write_file(
        path="/workspace/extract_questions_v2.py", content="print('second')\n"
    )

    assert first.success is True
    assert second.success is False
    assert second.output["error_code"] == "INTERMEDIATE_HELPER_CREATION_REDIRECT"
    assert (
        "already created an intermediate extraction/helper file"
        in second.output["error"]
    )
    assert "write the requested deliverable" in second.output["error"]


@pytest.mark.asyncio
async def test_write_file_rewrite_of_extraction_helper_redirects_to_deliverable(
    monkeypatch,
):
    files = _FakeFiles({"/workspace/extract_q.py": "print('old')\n"})
    tool = sandbox_code_tool_module.SandboxCodeTool(
        project_id="project-rewrite-extract-helper", thread_manager=None
    )
    tool.sandbox = types.SimpleNamespace(files=files)

    async def _ensure_sandbox():
        return None

    async def _emit_write_file_status(*args, **kwargs):
        del args, kwargs
        return None

    monkeypatch.setattr(tool, "_ensure_sandbox", _ensure_sandbox)
    monkeypatch.setattr(tool, "_emit_write_file_status", _emit_write_file_status)

    result = await tool.write_file(
        path="/workspace/extract_q.py", content="print('new')\n"
    )

    assert result.success is False
    assert result.output["error_code"] == "WORKSPACE_REWRITE_BLOCKED"
    assert "intermediate extraction/helper file" in result.output["error"]
    assert "write the requested deliverable" in result.output["error"]
    assert "Do not delete" in result.output["error"]


@pytest.mark.asyncio
async def test_execute_command_delete_extraction_helper_redirects_to_deliverable(
    monkeypatch,
):
    tool = sandbox_code_tool_module.SandboxCodeTool(
        project_id="project-delete-extract-helper", thread_manager=None
    )
    tool.sandbox = types.SimpleNamespace(commands=_FakeCommands(""))

    async def _ensure_sandbox():
        return None

    async def _emit_tool_status(*args, **kwargs):
        del args, kwargs
        return None

    monkeypatch.setattr(tool, "_ensure_sandbox", _ensure_sandbox)
    monkeypatch.setattr(tool, "_emit_tool_status", _emit_tool_status)

    result = await tool.execute_command(command="rm /workspace/extract_q.py")

    assert result.success is False
    assert result.output["error_code"] == "WORKSPACE_DELETE_REWRITE_BLOCKED"
    assert "intermediate extraction/helper file" in result.output["error"]
    assert "write the requested deliverable" in result.output["error"]


@pytest.mark.asyncio
async def test_upload_file_blocks_helper_script_upload(monkeypatch):
    import base64

    tool = sandbox_code_tool_module.SandboxCodeTool(
        project_id="project-upload-helper", thread_manager=None
    )
    tool.sandbox = types.SimpleNamespace(files=_FakeFiles())
    helper = (
        "with open('/workspace/ds_real_deck_builder.py') as f:\n"
        "    content = f.read()\n"
        "open('/workspace/ds_real_deck_builder.py', 'w').write(content)\n"
    )

    async def _ensure_sandbox():
        return None

    monkeypatch.setattr(tool, "_ensure_sandbox", _ensure_sandbox)
    monkeypatch.setattr(tool, "_persist_workspace_artifact", _noop, raising=False)

    result = await tool.upload_file(
        path="/workspace/_fix_acent.py",
        content_base64=base64.b64encode(helper.encode()).decode(),
    )

    assert result.success is False
    assert result.output["error_code"] == "FIX_SCRIPT_BLOCKED"
    assert "edit_file" in result.output["error"]
    assert "/workspace/ds_real_deck_builder.py" in result.output["error"]
    assert "_fix_acent.py" not in tool.sandbox.files.files


@pytest.mark.asyncio
async def test_write_file_rejects_relative_fix_script_before_sandbox(monkeypatch):
    files = _FakeFiles()
    tool = sandbox_code_tool_module.SandboxCodeTool(
        project_id="project-relative-fix-script", thread_manager=None
    )
    tool.sandbox = types.SimpleNamespace(files=files)

    async def _ensure_sandbox():
        return None

    async def _emit_write_file_status(*args, **kwargs):
        del args, kwargs
        return None

    monkeypatch.setattr(tool, "_ensure_sandbox", _ensure_sandbox)
    monkeypatch.setattr(tool, "_emit_write_file_status", _emit_write_file_status)
    monkeypatch.setattr(tool, "_persist_workspace_artifact", _noop, raising=False)

    result = await tool.write_file(
        path="fix_text.py",
        content=(
            "with open('ds_real_manim_scene.py') as f:\n"
            "    content = f.read()\n"
            "content = content.replace('bad', 'good')\n"
            "with open('ds_real_manim_scene.py', 'w') as f:\n"
            "    f.write(content)\n"
        ),
    )

    assert result.success is False
    assert result.output["error_code"] == "FIX_SCRIPT_BLOCKED"
    assert result.output["path"] == "/workspace/fix_text.py"
    assert "edit_file" in result.output["error"]
    assert "fix_text.py" not in files.files


@pytest.mark.asyncio
async def test_write_file_allows_initial_workspace_file_creation(monkeypatch):
    content = _large_python_builder("initial")
    files = _FakeFiles()
    tool = sandbox_code_tool_module.SandboxCodeTool(
        project_id="project-initial-create", thread_manager=None
    )
    tool.sandbox = types.SimpleNamespace(files=files)

    async def _ensure_sandbox():
        return None

    async def _emit_write_file_status(*args, **kwargs):
        del args, kwargs
        return None

    monkeypatch.setattr(tool, "_ensure_sandbox", _ensure_sandbox)
    monkeypatch.setattr(tool, "_emit_write_file_status", _emit_write_file_status)
    monkeypatch.setattr(tool, "_persist_workspace_artifact", _noop, raising=False)

    result = await tool.write_file(
        path="/workspace/ds_real_deck_builder.py",
        content=content,
    )

    assert result.success is True
    assert files.files["ds_real_deck_builder.py"] == content
    assert files.writes == [("/workspace/ds_real_deck_builder.py", content)]


@pytest.mark.asyncio
async def test_write_file_rejects_missing_path_before_sandbox(monkeypatch):
    tool = sandbox_code_tool_module.SandboxCodeTool(
        project_id="project-4", thread_manager=None
    )

    ensure_called = {"value": False}
    captured_status = []

    async def _ensure_sandbox():
        ensure_called["value"] = True
        return None

    async def _emit_write_file_status(status_type, path, *, message=None, extra=None):
        captured_status.append(
            {
                "status_type": status_type,
                "path": path,
                "message": message,
                "extra": extra or {},
            }
        )

    monkeypatch.setattr(tool, "_ensure_sandbox", _ensure_sandbox)
    monkeypatch.setattr(tool, "_emit_write_file_status", _emit_write_file_status)

    result = await tool.write_file(path="   ", content="print(1)")

    assert result.success is False
    assert result.output["error_code"] == "WRITE_FILE_INCOMPLETE_PAYLOAD"
    assert ensure_called["value"] is False
    assert captured_status
    assert captured_status[-1]["status_type"] == "tool_failed"
    assert captured_status[-1]["extra"]["exec_gate_reason"] == "missing_path"


@pytest.mark.asyncio
async def test_write_file_rejects_rename_helper_script_before_sandbox(monkeypatch):
    tool = sandbox_code_tool_module.SandboxCodeTool(
        project_id="project-5", thread_manager=None
    )

    ensure_called = {"value": False}
    captured_status = []

    async def _ensure_sandbox():
        ensure_called["value"] = True
        return None

    async def _emit_write_file_status(status_type, path, *, message=None, extra=None):
        captured_status.append(
            {
                "status_type": status_type,
                "path": path,
                "message": message,
                "extra": extra or {},
            }
        )

    monkeypatch.setattr(tool, "_ensure_sandbox", _ensure_sandbox)
    monkeypatch.setattr(tool, "_emit_write_file_status", _emit_write_file_status)

    result = await tool.write_file(
        path="/workspace/rename_file.py",
        content=(
            "from pathlib import Path\n"
            "Path('/workspace/depseek_edit_target.txt').rename('/workspace/deepseek_edit_target.txt')\n"
        ),
    )

    assert result.success is False
    assert result.output["error_code"] == "FIX_SCRIPT_BLOCKED"
    assert ensure_called["value"] is False
    assert captured_status[-1]["status_type"] == "tool_rejected_fix_script"


@pytest.mark.asyncio
async def test_write_file_rejects_fix_script_that_rewrites_workspace_html_before_sandbox(
    monkeypatch,
):
    tool = sandbox_code_tool_module.SandboxCodeTool(
        project_id="project-6", thread_manager=None
    )

    ensure_called = {"value": False}
    captured_status = []

    async def _ensure_sandbox():
        ensure_called["value"] = True
        return None

    async def _emit_write_file_status(status_type, path, *, message=None, extra=None):
        captured_status.append(
            {
                "status_type": status_type,
                "path": path,
                "message": message,
                "extra": extra or {},
            }
        )

    monkeypatch.setattr(tool, "_ensure_sandbox", _ensure_sandbox)
    monkeypatch.setattr(tool, "_emit_write_file_status", _emit_write_file_status)

    result = await tool.write_file(
        path="/workspace/fix2.py",
        content=(
            "p = '/workspace/ds_static.html'\n"
            "c = open(p).read()\n"
            "c = c.replace('#ef2ff;', '#eef2ff;')\n"
            "open(p, 'w').write(c)\n"
        ),
    )

    assert result.success is False
    assert result.output["error_code"] == "FIX_SCRIPT_BLOCKED"
    assert ensure_called["value"] is False
    assert captured_status[-1]["status_type"] == "tool_rejected_fix_script"


def test_fix_script_block_feedback_requires_edit_file_not_rewrite() -> None:
    hint = sandbox_code_tool_module._detect_fix_script_anti_pattern(
        "/workspace/fix_bulet.py",
        (
            "with open('/workspace/ds_real_deck_builder.py', 'r') as f:\n"
            "    content = f.read()\n"
            "content = content.replace(\"bullet_char='\\u202'\", \"bullet_char='•'\")\n"
            "with open('/workspace/ds_real_deck_builder.py', 'w') as f:\n"
            "    f.write(content)\n"
        ),
    )

    assert hint is not None
    assert "read_file" in hint
    assert "edit_file" in hint
    assert "/workspace/ds_real_deck_builder.py" in hint
    assert "Do not rewrite" in hint
    assert "do not call write_file" in hint
    assert "fix_quotes.py" in hint
    assert "return to read_file" in hint.lower()
    assert "correct one-shot sed" in hint.lower()


def test_fix_script_detector_blocks_sed_patch_file() -> None:
    hint = sandbox_code_tool_module._detect_fix_script_anti_pattern(
        "/workspace/fix198.sed",
        '308s/(", Pt/("", Pt/\n',
    )

    assert hint is not None
    assert "fix198.sed" in hint
    assert "sed -i" in hint
    assert "do not write" in hint.lower()


@pytest.mark.asyncio
async def test_write_file_rejects_check_script_before_sandbox(monkeypatch):
    tool = sandbox_code_tool_module.SandboxCodeTool(
        project_id="project-command-check-script", thread_manager=None
    )

    ensure_called = {"value": False}

    async def _ensure_sandbox():
        ensure_called["value"] = True
        return None

    async def _emit_write_file_status(*args, **kwargs):
        del args, kwargs
        return None

    monkeypatch.setattr(tool, "_ensure_sandbox", _ensure_sandbox)
    monkeypatch.setattr(tool, "_emit_write_file_status", _emit_write_file_status)

    result = await tool.write_file(
        path="/workspace/check_lines.py",
        content=(
            "with open('/workspace/ds_real_deck_builder.py') as f:\n"
            "    lines = f.readlines()\n"
            "print(lines[68])\n"
        ),
    )

    assert result.success is False
    assert result.output["error_code"] == "FIX_SCRIPT_BLOCKED"
    assert ensure_called["value"] is False
    assert "check_lines.py" in result.output["error"]


def test_fix_script_detector_allows_legitimate_project_python_file() -> None:
    hint = sandbox_code_tool_module._detect_fix_script_anti_pattern(
        "/workspace/fixed_scene.py",
        "from manim import Scene\n\nclass FixedScene(Scene):\n    pass\n",
    )

    assert hint is None


def test_python_syntax_failure_advice_steers_long_one_off_scripts_to_shorter_tactics():
    advice = sandbox_code_tool_module._python_syntax_failure_recovery_advice(
        "/workspace/extract_wrong.py",
        "\n".join(f"print({i})" for i in range(150)),
    )

    assert "long Python script" in advice
    assert "≤100 line python3 heredoc" in advice
    assert "write the deliverable instead" in advice


@pytest.mark.asyncio
async def test_write_file_redirects_intermediate_helper_after_syntax_validation_failure(
    monkeypatch,
):
    tool = sandbox_code_tool_module.SandboxCodeTool(
        project_id="project-intermediate-syntax", thread_manager=None
    )

    async def _ensure_sandbox():
        return None

    async def _emit_write_file_status(*args, **kwargs):
        del args, kwargs
        return None

    async def _emit_guard_stage(*args, **kwargs):
        del args, kwargs
        return None

    async def _prepare_python_write_validation(path, content):
        del path, content
        return {
            "processed_content": "print('unterminated)\n",
            "syntax_ok": False,
            "error": {
                "error_type": "SyntaxError",
                "message": "unterminated string literal",
                "line": 1,
            },
        }

    monkeypatch.setattr(tool, "_ensure_sandbox", _ensure_sandbox)
    monkeypatch.setattr(tool, "_emit_write_file_status", _emit_write_file_status)
    monkeypatch.setattr(tool, "_emit_guard_stage", _emit_guard_stage)
    monkeypatch.setattr(tool, "_should_apply_code_guard", lambda path, content: True)
    monkeypatch.setattr(
        tool,
        "_prepare_python_write_validation",
        _prepare_python_write_validation,
    )

    result = await tool.write_file(
        path="/workspace/extract_questions.py",
        content="print('unterminated)\n",
    )

    assert result.success is False
    assert result.output["error_code"] == "INTERMEDIATE_HELPER_SYNTAX_REDIRECT"
    assert "intermediate extraction/helper" in result.output["error"]
    assert (
        "write the requested Markdown/HTML deliverable directly"
        in result.output["error"]
    )
    assert tool._intermediate_helper_write_count == 1
