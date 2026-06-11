import sys
import types
import importlib.util
import asyncio
import inspect
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

from agentpress.tool import ToolResult

MODULE_PATH = BACKEND_ROOT / "agentscope_integration" / "tools" / "toolkit_adapter.py"
spec = importlib.util.spec_from_file_location("toolkit_adapter_module", MODULE_PATH)
toolkit_adapter_module = importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules["toolkit_adapter_module"] = toolkit_adapter_module
spec.loader.exec_module(toolkit_adapter_module)


class _FakeCodeTool:
    calls = []

    @classmethod
    def reset(cls):
        cls.calls = []

    @staticmethod
    async def _ok(**kwargs):
        _FakeCodeTool.calls.append(kwargs)
        return ToolResult(success=True, output={"output": "ok", "arguments": kwargs})

    execute_command = _ok
    read_file = _ok
    write_file = _ok
    edit_file = _ok
    list_dir = _ok
    make_dir = _ok
    upload_file = _ok
    download_file = _ok
    expose_port = _ok




class _FakeMediaTool:
    @staticmethod
    async def understand_media(**kwargs):
        return ToolResult(success=True, output={"output": "ok", "arguments": kwargs})

class _WriteGateFakeCodeTool(_FakeCodeTool):
    write_called = False
    status_events = []

    @classmethod
    def reset(cls):
        cls.write_called = False
        cls.status_events = []

    async def write_file(self, **kwargs):
        _WriteGateFakeCodeTool.write_called = True
        return ToolResult(success=True, output={"output": "ok", "arguments": kwargs})

    async def _emit_write_file_status(self, status_type, path, *, message=None, extra=None):
        _WriteGateFakeCodeTool.status_events.append(
            {
                "status_type": status_type,
                "path": path,
                "message": message,
                "extra": extra or {},
            }
        )


def _extract_tool_names(schemas):
    names = set()
    for schema in schemas:
        if isinstance(schema, dict):
            if isinstance(schema.get("function"), dict):
                name = schema["function"].get("name")
                if name:
                    names.add(name)
                continue
            name = schema.get("name")
            if name:
                names.add(name)
    return names


def _find_tool_schema(schemas, name):
    for schema in schemas:
        if isinstance(schema, dict):
            fn = schema.get("function") if isinstance(schema.get("function"), dict) else schema
            if fn.get("name") == name:
                return fn
    return None


def test_toolkit_adapter_registers_edit_file_tool(monkeypatch):
    def _fake_init_tool_instances(self):
        self._tool_instances = {"code": _FakeCodeTool()}

    monkeypatch.setattr(
        toolkit_adapter_module.ToolkitAdapter,
        "_init_tool_instances",
        _fake_init_tool_instances,
    )

    adapter = toolkit_adapter_module.ToolkitAdapter(
        project_id="project-1",
        thread_id="thread-1",
        thread_manager=None,
    )

    tool_names = _extract_tool_names(adapter.toolkit.get_json_schemas())
    assert "edit_file" in tool_names


def test_toolkit_adapter_lazy_code_backend_init_on_first_use(monkeypatch):
    created = []

    class _LazyCodeTool(_FakeCodeTool):
        @staticmethod
        async def execute_command(**kwargs):
            return ToolResult(success=True, output={"output": "ok", "arguments": kwargs})

    def _fake_init_tool_instances(self):
        self._tool_instances = {}
        self._tool_factories = {
            "code": lambda: created.append("code") or _LazyCodeTool(),
        }

    monkeypatch.setattr(
        toolkit_adapter_module.ToolkitAdapter,
        "_init_tool_instances",
        _fake_init_tool_instances,
    )

    adapter = toolkit_adapter_module.ToolkitAdapter(
        project_id="project-1",
        thread_id="thread-1",
        thread_manager=None,
    )

    assert created == []
    assert "execute_command" in _extract_tool_names(adapter.toolkit.get_json_schemas())

    result = asyncio.run(adapter._execute_command_guarded(command="pwd"))

    assert result.success is True
    assert created == ["code"]
    assert adapter.get_tool_instance("code") is not None


def test_toolkit_adapter_clone_toolkit_replays_registered_tools(monkeypatch):
    def _fake_init_tool_instances(self):
        self._tool_instances = {"code": _FakeCodeTool()}

    monkeypatch.setattr(
        toolkit_adapter_module.ToolkitAdapter,
        "_init_tool_instances",
        _fake_init_tool_instances,
    )

    adapter = toolkit_adapter_module.ToolkitAdapter(
        project_id="project-1",
        thread_id="thread-1",
        thread_manager=None,
    )

    cloned = adapter.clone_toolkit()
    cloned_tool_names = _extract_tool_names(cloned.get_json_schemas())
    assert "execute_command" in cloned_tool_names
    assert "read_file" in cloned_tool_names
    assert "edit_file" in cloned_tool_names


def test_edit_file_guarded_rejects_noop_with_consistent_error_code(monkeypatch):
    class _NoopFakeCodeTool(_FakeCodeTool):
        edit_called = False

        async def edit_file(self, **kwargs):
            del kwargs
            _NoopFakeCodeTool.edit_called = True
            return ToolResult(success=True, output={"output": "should not call backend"})

    def _fake_init_tool_instances(self):
        self._tool_instances = {"code": _NoopFakeCodeTool()}

    monkeypatch.setattr(
        toolkit_adapter_module.ToolkitAdapter,
        "_init_tool_instances",
        _fake_init_tool_instances,
    )

    adapter = toolkit_adapter_module.ToolkitAdapter(
        project_id="project-1",
        thread_id="thread-1",
        thread_manager=None,
    )
    adapter.file_read_cache.set(
        "demo.py",
        toolkit_adapter_module.FileReadEntry("same text"),
    )

    result = asyncio.run(
        adapter._edit_file_guarded(
            path="demo.py",
            old_text="same text",
            new_text="same text",
        ),
    )

    assert result.success is False
    assert result.output["error_code"] == "EDIT_FILE_NOOP"
    assert _NoopFakeCodeTool.edit_called is False


def test_execute_command_read_only_inspection_primes_edit_file_cache(monkeypatch):
    class _InspectFakeCodeTool(_FakeCodeTool):
        edited = False

        @staticmethod
        async def execute_command(**kwargs):
            return ToolResult(
                success=True,
                output={
                    "command": kwargs["command"],
                    "stdout": "111:add_thin_border_table(sumary_table)\n",
                    "stderr": "",
                    "output": "111:add_thin_border_table(sumary_table)\n",
                    "exit_code": 0,
                },
            )

        async def edit_file(self, **kwargs):
            _InspectFakeCodeTool.edited = True
            return ToolResult(success=True, output={"output": "updated", **kwargs})

        @staticmethod
        def _normalize_path(path):
            path = str(path or "")
            return path if path.startswith("/workspace/") else f"/workspace/{path}"

    def _fake_init_tool_instances(self):
        self._tool_instances = {"code": _InspectFakeCodeTool()}

    monkeypatch.setattr(
        toolkit_adapter_module.ToolkitAdapter,
        "_init_tool_instances",
        _fake_init_tool_instances,
    )

    adapter = toolkit_adapter_module.ToolkitAdapter(
        project_id="project-1",
        thread_id="thread-1",
        thread_manager=None,
    )

    inspect_result = asyncio.run(
        adapter._execute_command_guarded(
            command="grep -n 'sumary\\|summary' /workspace/generate_grading_report.py",
        ),
    )
    edit_result = asyncio.run(
        adapter._edit_file_guarded(
            path="/workspace/generate_grading_report.py",
            old_text="add_thin_border_table(sumary_table)",
            new_text="add_thin_border_table(summary_table)",
        ),
    )

    assert inspect_result.success is True
    assert edit_result.success is True
    assert _InspectFakeCodeTool.edited is True


def test_toolkit_adapter_preserves_read_file_required_args(monkeypatch):
    def _fake_init_tool_instances(self):
        self._tool_instances = {"code": _FakeCodeTool()}

    monkeypatch.setattr(
        toolkit_adapter_module.ToolkitAdapter,
        "_init_tool_instances",
        _fake_init_tool_instances,
    )

    adapter = toolkit_adapter_module.ToolkitAdapter(
        project_id="project-1",
        thread_id="thread-1",
        thread_manager=None,
    )

    schema = _find_tool_schema(adapter.toolkit.get_json_schemas(), "read_file")
    assert schema is not None
    parameters = schema.get("parameters", {})
    assert "path" in parameters.get("properties", {})
    assert "path" in parameters.get("required", [])

    read_file_tool = adapter.toolkit.tools["read_file"].original_func
    signature = inspect.signature(read_file_tool)
    assert "path" in signature.parameters


def test_toolkit_adapter_preserves_execute_command_required_args(monkeypatch):
    def _fake_init_tool_instances(self):
        self._tool_instances = {"code": _FakeCodeTool()}

    monkeypatch.setattr(
        toolkit_adapter_module.ToolkitAdapter,
        "_init_tool_instances",
        _fake_init_tool_instances,
    )

    adapter = toolkit_adapter_module.ToolkitAdapter(
        project_id="project-1",
        thread_id="thread-1",
        thread_manager=None,
    )

    schema = _find_tool_schema(adapter.toolkit.get_json_schemas(), "execute_command")
    assert schema is not None
    parameters = schema.get("parameters", {})
    assert "command" in parameters.get("properties", {})
    assert "command" in parameters.get("required", [])


@pytest.mark.parametrize(
    ("tool_name", "kwargs", "missing"),
    [
        ("read_file", {}, ["path"]),
        ("read_file", {"path": "   "}, ["path"]),
        ("execute_command", {}, ["command"]),
        ("execute_command", {"command": ""}, ["command"]),
        ("edit_file", {"path": "demo.py", "old_text": "x"}, ["new_text"]),
        ("make_dir", {}, ["path"]),
        ("download_file", {}, ["path"]),
        ("expose_port", {}, ["port"]),
    ],
)
def test_registered_tools_reject_missing_required_params_before_backend(
    monkeypatch,
    tool_name,
    kwargs,
    missing,
):
    _FakeCodeTool.reset()

    def _fake_init_tool_instances(self):
        self._tool_instances = {"code": _FakeCodeTool()}

    monkeypatch.setattr(
        toolkit_adapter_module.ToolkitAdapter,
        "_init_tool_instances",
        _fake_init_tool_instances,
    )

    adapter = toolkit_adapter_module.ToolkitAdapter(
        project_id="project-1",
        thread_id="thread-1",
        thread_manager=None,
    )

    tool_func = adapter.toolkit.tools[tool_name].original_func
    response = asyncio.run(tool_func(**kwargs))
    first_block = response.content[0]
    text = (
        first_block.get("text")
        if isinstance(first_block, dict)
        else first_block.text
    )

    assert "MISSING_REQUIRED_PARAMS" in text
    for param in missing:
        assert param in text
    assert _FakeCodeTool.calls == []


def test_execute_command_guard_blocks_ocr_when_enabled(monkeypatch):
    class _GuardFakeCodeTool(_FakeCodeTool):
        called = False

        @staticmethod
        async def execute_command(**kwargs):
            _GuardFakeCodeTool.called = True
            return ToolResult(success=True, output={"output": "ok", "arguments": kwargs})

    def _fake_init_tool_instances(self):
        self._tool_instances = {"code": _GuardFakeCodeTool()}

    monkeypatch.setattr(
        toolkit_adapter_module.ToolkitAdapter,
        "_init_tool_instances",
        _fake_init_tool_instances,
    )

    adapter = toolkit_adapter_module.ToolkitAdapter(
        project_id="project-1",
        thread_id="thread-1",
        thread_manager=None,
    )
    adapter.set_multimodal_guard_context(
        has_image_media_refs=True,
        enforce_ocr_block=True,
    )

    result = asyncio.run(
        adapter._execute_command_guarded(command="tesseract input.png output.txt"),
    )

    assert result.success is False
    assert _GuardFakeCodeTool.called is False
    assert "blocked" in str(result.output).lower() or "ocr" in str(result.output).lower()


def test_execute_command_guard_allows_non_ocr(monkeypatch):
    class _GuardFakeCodeTool(_FakeCodeTool):
        @staticmethod
        async def execute_command(**kwargs):
            return ToolResult(success=True, output={"output": "ok", "arguments": kwargs})

    def _fake_init_tool_instances(self):
        self._tool_instances = {"code": _GuardFakeCodeTool()}

    monkeypatch.setattr(
        toolkit_adapter_module.ToolkitAdapter,
        "_init_tool_instances",
        _fake_init_tool_instances,
    )

    adapter = toolkit_adapter_module.ToolkitAdapter(
        project_id="project-1",
        thread_id="thread-1",
        thread_manager=None,
    )
    adapter.set_multimodal_guard_context(
        has_image_media_refs=True,
        enforce_ocr_block=True,
    )

    result = asyncio.run(
        adapter._execute_command_guarded(command="python3 script.py"),
    )

    assert result.success is True


def test_execute_command_guard_rejects_empty_command_before_backend(monkeypatch):
    class _GuardFakeCodeTool(_FakeCodeTool):
        called = False

        @staticmethod
        async def execute_command(**kwargs):
            del kwargs
            _GuardFakeCodeTool.called = True
            return ToolResult(success=True, output={"output": "should not call backend"})

    def _fake_init_tool_instances(self):
        self._tool_instances = {"code": _GuardFakeCodeTool()}

    monkeypatch.setattr(
        toolkit_adapter_module.ToolkitAdapter,
        "_init_tool_instances",
        _fake_init_tool_instances,
    )

    adapter = toolkit_adapter_module.ToolkitAdapter(
        project_id="project-1",
        thread_id="thread-1",
        thread_manager=None,
    )

    result = asyncio.run(adapter._execute_command_guarded(command="   "))

    assert result.success is False
    assert "non-empty" in result.output["error"]
    assert _GuardFakeCodeTool.called is False


def test_execute_command_guard_adds_character_loss_hint(monkeypatch):
    class _CharLossFakeCodeTool(_FakeCodeTool):
        @staticmethod
        async def execute_command(**kwargs):
            return ToolResult(
                success=False,
                output={"stderr": "SyntaxError: invalid syntax near 'for in range(3)'"},
            )

    def _fake_init_tool_instances(self):
        self._tool_instances = {"code": _CharLossFakeCodeTool()}

    monkeypatch.setattr(
        toolkit_adapter_module.ToolkitAdapter,
        "_init_tool_instances",
        _fake_init_tool_instances,
    )

    adapter = toolkit_adapter_module.ToolkitAdapter(
        project_id="project-1",
        thread_id="thread-1",
        thread_manager=None,
    )

    result = asyncio.run(adapter._execute_command_guarded(command="python demo.py"))

    assert result.success is False
    assert "CHARACTER LOSS DETECTED" in result.output["char_loss_hint"]


def test_read_file_guard_blocks_uploaded_image_path(monkeypatch):
    class _GuardFakeCodeTool(_FakeCodeTool):
        called = False

        @staticmethod
        async def read_file(**kwargs):
            _GuardFakeCodeTool.called = True
            return ToolResult(success=True, output={"output": "ok", "arguments": kwargs})

    def _fake_init_tool_instances(self):
        self._tool_instances = {"code": _GuardFakeCodeTool()}

    monkeypatch.setattr(
        toolkit_adapter_module.ToolkitAdapter,
        "_init_tool_instances",
        _fake_init_tool_instances,
    )

    adapter = toolkit_adapter_module.ToolkitAdapter(
        project_id="project-1",
        thread_id="thread-1",
        thread_manager=None,
    )
    adapter.set_multimodal_guard_context(
        has_image_media_refs=True,
        enforce_ocr_block=True,
        image_media_refs=[
            {
                "kind": "image",
                "path": "/workspace/image.png",
                "mime_type": "image/png",
                "filename": "image.png",
            }
        ],
    )

    result = asyncio.run(
        adapter._read_file_guarded(path="./image.png"),
    )

    assert result.success is False
    assert _GuardFakeCodeTool.called is False
    assert isinstance(result.output, dict)
    assert result.output.get("blocked_path") == "/workspace/image.png"


def test_read_file_guard_allows_non_uploaded_path(monkeypatch):
    class _GuardFakeCodeTool(_FakeCodeTool):
        called = False

        @staticmethod
        async def read_file(**kwargs):
            _GuardFakeCodeTool.called = True
            return ToolResult(success=True, output={"output": "ok", "arguments": kwargs})

    def _fake_init_tool_instances(self):
        self._tool_instances = {"code": _GuardFakeCodeTool()}

    monkeypatch.setattr(
        toolkit_adapter_module.ToolkitAdapter,
        "_init_tool_instances",
        _fake_init_tool_instances,
    )

    adapter = toolkit_adapter_module.ToolkitAdapter(
        project_id="project-1",
        thread_id="thread-1",
        thread_manager=None,
    )
    adapter.set_multimodal_guard_context(
        has_image_media_refs=True,
        enforce_ocr_block=True,
        image_media_refs=[
            {
                "kind": "image",
                "path": "/workspace/image.png",
                "mime_type": "image/png",
                "filename": "image.png",
            }
        ],
    )

    result = asyncio.run(
        adapter._read_file_guarded(path="notes.md"),
    )

    assert result.success is True
    assert _GuardFakeCodeTool.called is True


def test_write_file_streaming_gate_rejects_empty_content(monkeypatch):
    _WriteGateFakeCodeTool.reset()

    def _fake_init_tool_instances(self):
        self._tool_instances = {"code": _WriteGateFakeCodeTool()}

    monkeypatch.setattr(
        toolkit_adapter_module.ToolkitAdapter,
        "_init_tool_instances",
        _fake_init_tool_instances,
    )

    adapter = toolkit_adapter_module.ToolkitAdapter(
        project_id="project-1",
        thread_id="thread-1",
        thread_manager=None,
    )

    write_fn = adapter.toolkit.tools["write_file"].original_func

    async def _collect():
        chunks = []
        async for chunk in write_fn(path="/workspace/demo.py", content=""):
            chunks.append(chunk)
        return chunks

    chunks = asyncio.run(_collect())

    assert _WriteGateFakeCodeTool.write_called is False
    assert len(chunks) == 1
    first_chunk = chunks[0]
    first_block = first_chunk.content[0]
    text = first_block.get("text") if isinstance(first_block, dict) else first_block.text
    assert "WRITE_FILE_INCOMPLETE_PAYLOAD" in text
    assert _WriteGateFakeCodeTool.status_events
    last_status = _WriteGateFakeCodeTool.status_events[-1]
    assert last_status["status_type"] == "tool_failed"
    assert last_status["extra"]["exec_gate_reason"] == "empty_content"


def test_write_file_streaming_gate_allows_non_empty_content(monkeypatch):
    _WriteGateFakeCodeTool.reset()

    def _fake_init_tool_instances(self):
        self._tool_instances = {"code": _WriteGateFakeCodeTool()}

    monkeypatch.setattr(
        toolkit_adapter_module.ToolkitAdapter,
        "_init_tool_instances",
        _fake_init_tool_instances,
    )

    adapter = toolkit_adapter_module.ToolkitAdapter(
        project_id="project-1",
        thread_id="thread-1",
        thread_manager=None,
    )

    write_fn = adapter.toolkit.tools["write_file"].original_func

    async def _collect():
        chunks = []
        async for chunk in write_fn(path="/workspace/demo.py", content="print(1)"):
            chunks.append(chunk)
        return chunks

    chunks = asyncio.run(_collect())

    assert _WriteGateFakeCodeTool.write_called is True
    assert len(chunks) >= 1
    combined_text_parts = []
    for chunk in chunks:
        for block in chunk.content:
            combined_text_parts.append(
                block.get("text", "")
                if isinstance(block, dict)
                else getattr(block, "text", ""),
            )
    combined_text = "".join(combined_text_parts)
    assert "WRITE_FILE_INCOMPLETE_PAYLOAD" not in combined_text


def test_execute_command_reraises_shadow_clone_fatal_payload(monkeypatch):
    helper_calls = []

    class _FatalPayloadCodeTool(_FakeCodeTool):
        @staticmethod
        async def execute_command(**kwargs):
            return ToolResult(
                success=False,
                output={
                    "detail": {
                        "message": "Shadow Clone sandbox could not be reattached for run",
                        "error_code": "SHADOW_CLONE_SANDBOX_REATTACH_FAILED",
                    },
                },
            )

    def _fake_init_tool_instances(self):
        self._tool_instances = {"code": _FatalPayloadCodeTool()}

    async def _fake_raise(**kwargs):
        helper_calls.append(kwargs)
        raise toolkit_adapter_module.ShadowCloneSandboxFatalToolError(
            run_id="run-1",
            project_id="project-1",
            tool_name=kwargs["tool_name"],
            detail="fatal payload",
            error_code="SHADOW_CLONE_SANDBOX_REATTACH_FAILED",
            recoverable=False,
            binding_state="lost",
            recovery_kind="replan",
        )

    monkeypatch.setattr(
        toolkit_adapter_module.ToolkitAdapter,
        "_init_tool_instances",
        _fake_init_tool_instances,
    )
    monkeypatch.setattr(
        toolkit_adapter_module,
        "maybe_raise_shadow_clone_fatal_tool_error",
        _fake_raise,
    )

    adapter = toolkit_adapter_module.ToolkitAdapter(
        project_id="project-1",
        thread_id="thread-1",
        thread_manager=None,
        shadow_clone_run_id="run-1",
        strict_sandbox=True,
        shadow_clone_fail_fast=True,
    )

    execute_fn = adapter.toolkit.tools["execute_command"].original_func

    with pytest.raises(toolkit_adapter_module.ShadowCloneSandboxFatalToolError):
        asyncio.run(execute_fn(command="pwd"))

    assert helper_calls
    assert helper_calls[-1]["tool_name"] == "execute_command"
    assert helper_calls[-1]["payload"]["detail"]["error_code"] == (
        "SHADOW_CLONE_SANDBOX_REATTACH_FAILED"
    )
    assert helper_calls[-1]["error"] is None


def test_execute_command_reraises_shadow_clone_fatal_exception(monkeypatch):
    helper_calls = []

    class _FatalExceptionCodeTool(_FakeCodeTool):
        @staticmethod
        async def execute_command(**kwargs):
            raise RuntimeError("shadow clone sandbox attach failed")

    def _fake_init_tool_instances(self):
        self._tool_instances = {"code": _FatalExceptionCodeTool()}

    async def _fake_raise(**kwargs):
        helper_calls.append(kwargs)
        raise toolkit_adapter_module.ShadowCloneSandboxFatalToolError(
            run_id="run-1",
            project_id="project-1",
            tool_name=kwargs["tool_name"],
            detail="fatal exception",
            error_code="SHADOW_CLONE_SANDBOX_REATTACH_FAILED",
            recoverable=False,
            binding_state="lost",
            recovery_kind="replan",
        )

    monkeypatch.setattr(
        toolkit_adapter_module.ToolkitAdapter,
        "_init_tool_instances",
        _fake_init_tool_instances,
    )
    monkeypatch.setattr(
        toolkit_adapter_module,
        "maybe_raise_shadow_clone_fatal_tool_error",
        _fake_raise,
    )

    adapter = toolkit_adapter_module.ToolkitAdapter(
        project_id="project-1",
        thread_id="thread-1",
        thread_manager=None,
        shadow_clone_run_id="run-1",
        strict_sandbox=True,
        shadow_clone_fail_fast=True,
    )

    execute_fn = adapter.toolkit.tools["execute_command"].original_func

    with pytest.raises(toolkit_adapter_module.ShadowCloneSandboxFatalToolError):
        asyncio.run(execute_fn(command="pwd"))

    assert helper_calls
    assert helper_calls[-1]["tool_name"] == "execute_command"
    assert isinstance(helper_calls[-1]["error"], RuntimeError)
    assert helper_calls[-1]["payload"] is None


def test_toolkit_adapter_skips_computer_use_for_strict_non_desktop_shadow_clone(monkeypatch):
    class _FakeTool:
        def __init__(self, *args, **kwargs):
            self.args = args
            self.kwargs = kwargs

    class _FakeTaskListTool(_FakeTool):
        async def create_tasks(self, **kwargs):
            return ToolResult(success=True, output=kwargs)

        async def view_tasks(self, **kwargs):
            return ToolResult(success=True, output=kwargs)

        async def update_tasks(self, **kwargs):
            return ToolResult(success=True, output=kwargs)

        async def delete_tasks(self, **kwargs):
            return ToolResult(success=True, output=kwargs)

    fake_modules = {
        "agent.tools.sandbox_code_tool": types.SimpleNamespace(SandboxCodeTool=_FakeTool),
        "agent.tools.sandbox_web_search_tool": types.SimpleNamespace(SandboxWebSearchTool=_FakeTool),
        "agent.tools.sandbox_skill_tool": types.SimpleNamespace(SandboxSkillTool=_FakeTool),
        "agent.tools.computer_use_tool": types.SimpleNamespace(ComputerUseTool=_FakeTool),
        "agent.tools.task_list_tool": types.SimpleNamespace(TaskListTool=_FakeTaskListTool),
    }
    for module_name, fake_module in fake_modules.items():
        monkeypatch.setitem(sys.modules, module_name, fake_module)

    monkeypatch.setattr(toolkit_adapter_module.ToolkitAdapter, "_register_all_tools", lambda self: None)
    monkeypatch.setattr(
        toolkit_adapter_module.config,
        "get_shadow_clone_sandbox_type",
        lambda: "code",
    )

    adapter = toolkit_adapter_module.ToolkitAdapter(
        project_id="project-1",
        thread_id="thread-1",
        thread_manager=None,
        shadow_clone_run_id="run-1",
        strict_sandbox=True,
    )

    assert "computer_use" not in adapter._tool_instances
    assert adapter._strict_shadow_clone_sandbox_type == "code"


def test_toolkit_adapter_registers_understand_media_tool(monkeypatch):
    def _fake_init_tool_instances(self):
        self._tool_instances = {"code": _FakeCodeTool()}
        self._tool_factories = {"media": lambda: _FakeMediaTool()}

    monkeypatch.setattr(
        toolkit_adapter_module.ToolkitAdapter,
        "_init_tool_instances",
        _fake_init_tool_instances,
    )

    adapter = toolkit_adapter_module.ToolkitAdapter(
        project_id="project-1",
        thread_id="thread-1",
        thread_manager=None,
    )

    tool_names = _extract_tool_names(adapter.toolkit.get_json_schemas())
    assert "understand_media" in tool_names

    schema = _find_tool_schema(adapter.toolkit.get_json_schemas(), "understand_media")
    assert schema is not None
    assert "Moonshot/Kimi external API" in schema.get("description", "")
    assert "path" in schema.get("parameters", {}).get("required", [])


def test_understand_media_guarded_lazily_calls_media_backend(monkeypatch):
    created = []

    def _fake_init_tool_instances(self):
        self._tool_instances = {"code": _FakeCodeTool()}
        self._tool_factories = {"media": lambda: created.append("media") or _FakeMediaTool()}

    monkeypatch.setattr(
        toolkit_adapter_module.ToolkitAdapter,
        "_init_tool_instances",
        _fake_init_tool_instances,
    )

    adapter = toolkit_adapter_module.ToolkitAdapter(
        project_id="project-1",
        thread_id="thread-1",
        thread_manager=None,
    )

    assert created == []
    result = asyncio.run(adapter._understand_media_guarded(path="demo.png"))

    assert result.success is True
    assert created == ["media"]
    assert result.output["arguments"]["path"] == "demo.png"
