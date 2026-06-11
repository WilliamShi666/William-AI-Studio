from __future__ import annotations

import importlib.util
import io
import zipfile
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1].parent / "agent_eval" / "econ_docx_e2e_eval.py"
spec = importlib.util.spec_from_file_location("econ_docx_e2e_eval", SCRIPT)
module = importlib.util.module_from_spec(spec)
assert spec and spec.loader
import sys
sys.modules["econ_docx_e2e_eval"] = module
spec.loader.exec_module(module)


def _call(name: str, args: dict) -> dict:
    return {"name": name, "arguments": args}


def test_counts_modifications_after_first_docx_script_creation() -> None:
    calls = [
        _call("write_file", {"path": "/workspace/make_doc.py", "content": "from docx import Document"}),
        _call("execute_command", {"command": "grep -n 'sumary' /workspace/make_doc.py"}),
        _call("execute_command", {"command": "sed -n '10,12p' /workspace/make_doc.py"}),
        _call("edit_file", {"path": "/workspace/make_doc.py", "old_text": "sumary", "new_text": "tbl"}),
        _call("execute_command", {"command": "sed -i 's/foo/bar/g' /workspace/make_doc.py"}),
        _call("execute_command", {"command": "python /workspace/make_doc.py"}),
    ]

    count, script_idx = module.count_modifications_after_script(calls)

    assert script_idx == 0
    assert count == 2


def test_ignores_rejected_script_creation_and_rejected_modifications() -> None:
    calls = [
        _call(
            "write_file",
            {
                "path": "/workspace/bad.py",
                "content": "from docx import Document\nbroken(",
                "success": False,
                "error_code": "PYTHON_SYNTAX_VALIDATION_FAILED",
            },
        ),
        _call(
            "execute_command",
            {
                "command": "python3 -c \"open('/workspace/bad.py','w').write('x')\"",
                "success": False,
                "error_code": "LONG_SCRIPT_COMMAND_BLOCKED",
            },
        ),
        _call(
            "write_file",
            {
                "path": "/workspace/good.py",
                "content": "from docx import Document\nDocument().save('/workspace/out.docx')\n",
                "success": True,
            },
        ),
        _call(
            "execute_command",
            {
                "command": "sed -i 's/foo/foo/g' /workspace/good.py",
                "success": False,
                "error_code": "NOOP_IN_PLACE_EDIT_BLOCKED",
            },
        ),
        _call(
            "edit_file",
            {
                "path": "/workspace/good.py",
                "old_text": "out.docx",
                "new_text": "report.docx",
                "success": True,
            },
        ),
    ]

    count, script_idx = module.count_modifications_after_script(calls)

    assert script_idx == 2
    assert count == 1


def test_extract_tool_calls_marks_plain_syntax_error_results_as_failed() -> None:
    messages = [
        {
            "type": "assistant",
            "content": (
                '{"tool_calls":[{"id":"call_bad","type":"function","function":'
                '{"name":"write_file","arguments":"{\\"path\\":\\"/workspace/bad.py\\",'
                '\\"content\\":\\"from docx import Document\\"}"}}]}'
            ),
        },
        {
            "type": "tool",
            "content": (
                '{"tool_call_id":"call_bad","tool_name":"write_file",'
                '"result":"SyntaxError in /workspace/bad.py at line 1: bad. File was not written."}'
            ),
        },
        {
            "type": "assistant",
            "content": (
                '{"tool_calls":[{"id":"call_good","type":"function","function":'
                '{"name":"write_file","arguments":"{\\"path\\":\\"/workspace/good.py\\",'
                '\\"content\\":\\"from docx import Document\\"}"}}]}'
            ),
        },
        {
            "type": "tool",
            "content": '{"tool_call_id":"call_good","tool_name":"write_file","result":"Wrote file"}',
        },
    ]

    calls = module.extract_tool_calls(messages)
    count, script_idx = module.count_modifications_after_script(calls)

    assert calls[0]["success"] is False
    assert calls[1]["success"] is True
    assert script_idx == 1
    assert count == 0


def test_validate_docx_requires_word_document_entry() -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("[Content_Types].xml", "ok")
        zf.writestr("word/document.xml", "<w:document/>")

    assert module.validate_docx(buf.getvalue()) is True
    assert module.validate_docx(b"not a zip") is False


def test_all_econ_eval_prompts_use_fixed_two_question_user_task() -> None:
    assert len(module.ECON_PROMPTS) == 5
    for prompt in module.ECON_PROMPTS:
        assert "5(c) Analyse how an appreciation" in prompt
        assert "3(c) Analyse how a fall in the rate of interest" in prompt
        assert "Appreciation results in imports increases" in prompt


def test_eval_repeats_fixed_user_prompt_five_times() -> None:
    assert len(module.ECON_PROMPTS) == 5
    assert len(set(module.ECON_PROMPTS)) == 1
    prompt = module.ECON_PROMPTS[0]
    assert "Analyse how an appreciation of the exchange rate may reduce" in prompt
    assert "Analyse how a fall in the rate of interest may affect" in prompt
    assert "Appreciation results in imports increases" in prompt
    assert "Create /workspace/econ_marking_report.docx" in prompt
    assert "no List Bullet style" in prompt


def test_runner_requests_official_deepseek_v4_pro_max_reasoning() -> None:
    posted = {}

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"thread_id": "thread-1", "agent_run_id": "run-1"}

    class _Client:
        async def post(self, url, headers, data, timeout):
            posted["url"] = url
            posted["headers"] = headers
            posted["data"] = data
            posted["timeout"] = timeout
            return _Resp()

        async def get(self, url, headers, timeout):
            raise RuntimeError("stop after initiate")

    runner = module.Runner("http://local/api", "token", "deepseek-v4-pro-max")
    result = module.asyncio.run(runner.run_attempt(_Client(), "prompt", 1))

    assert result.status == "error"
    assert posted["data"]["model_name"] == "deepseek-v4-pro-max"
    assert posted["data"]["enable_thinking"] == "true"
    assert posted["data"]["reasoning_effort"] == "max"
    assert posted["data"]["shadow_clone_mode"] == "off"

