import importlib.util
import json
import sys
from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = BACKEND_ROOT / "agentscope_integration" / "streaming" / "sse_adapter.py"
spec = importlib.util.spec_from_file_location("sse_adapter_module", MODULE_PATH)
module = importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules["sse_adapter_module"] = module
spec.loader.exec_module(module)

SSEAdapter = module.SSEAdapter


class _DummyMsg:
    def __init__(self, *, role: str = "assistant", blocks: list[dict] | None = None, text: str = "") -> None:
        self.role = role
        self._blocks = blocks or []
        self._text = text

    def get_content_blocks(self) -> list[dict]:
        return self._blocks

    def get_text_content(self) -> str:
        return self._text


def _metadata(payload: dict) -> dict:
    return json.loads(payload["metadata"])


def _content(payload: dict) -> dict:
    return json.loads(payload["content"])


def test_mixed_thinking_and_text_uses_chunk_status() -> None:
    adapter = SSEAdapter("thread-1", "run-1")
    msg = _DummyMsg(
        blocks=[
            {"type": "thinking", "thinking": "Need to reason first."},
            {"type": "text", "text": "Final answer starts now."},
        ],
        text="Final answer starts now.",
    )

    payload = adapter.convert(msg, is_last=False)

    assert _metadata(payload)["stream_status"] == "chunk"
    assert _content(payload)["content"] == "Final answer starts now."
    assert _content(payload)["reasoning_content"] == "Need to reason first."


def test_thinking_only_keeps_reasoning_chunk_status() -> None:
    adapter = SSEAdapter("thread-1", "run-1")
    msg = _DummyMsg(
        blocks=[{"type": "thinking", "thinking": "Still thinking..."}],
        text="",
    )

    payload = adapter.convert(msg, is_last=False)

    assert _metadata(payload)["stream_status"] == "reasoning_chunk"
    assert _content(payload)["reasoning_content"] == "Still thinking..."


def test_tool_use_takes_precedence_over_text_chunk_status() -> None:
    adapter = SSEAdapter("thread-1", "run-1")
    msg = _DummyMsg(
        blocks=[
            {"type": "tool_use", "id": "call_1", "name": "search_web", "input": {"query": "x"}},
            {"type": "text", "text": "I will call a tool."},
        ],
        text="I will call a tool.",
    )

    payload = adapter.convert(msg, is_last=False)

    assert _metadata(payload)["stream_status"] == "tool_call_chunk"


def test_tool_call_metadata_prefers_raw_arguments_when_available() -> None:
    adapter = SSEAdapter("thread-1", "run-1")
    msg = _DummyMsg(
        blocks=[
            {
                "type": "tool_use",
                "id": "call_1",
                "name": "write_file",
                "input": {"path": "/workspace/demo.py", "content": "print(1)"},
                "raw_arguments": '{"path":"/workspace/demo.py","content":"print(1',
                "arguments_source": "raw_preview",
            },
        ],
        text="",
    )

    payload = adapter.convert(msg, is_last=False)
    metadata = _metadata(payload)
    tool_call = metadata["tool_calls"][0]
    assert tool_call["function"]["arguments"] == '{"path":"/workspace/demo.py","content":"print(1'
    trace = tool_call["trace"]
    assert trace["trace_args_source"] == "raw_preview"
    assert trace["trace_raw_args_len"] > 0


def test_tool_result_takes_precedence_and_uses_tool_type() -> None:
    adapter = SSEAdapter("thread-1", "run-1")
    msg = _DummyMsg(
        role="tool",
        blocks=[
            {
                "type": "tool_result",
                "id": "call_1",
                "name": "search_web",
                "output": [{"text": "result"}],
            },
        ],
        text="result",
    )

    payload = adapter.convert(msg, is_last=False)

    assert payload["type"] == "tool"
    assert _metadata(payload)["stream_status"] == "tool_result_chunk"


def test_last_chunk_with_text_and_reasoning_marks_complete() -> None:
    adapter = SSEAdapter("thread-1", "run-1")
    msg = _DummyMsg(
        blocks=[
            {"type": "thinking", "thinking": "Done thinking."},
            {"type": "text", "text": "Final answer."},
        ],
        text="Final answer.",
    )

    payload = adapter.convert(msg, is_last=True)

    assert _metadata(payload)["stream_status"] == "complete"
    assert _content(payload)["content"] == "Final answer."
    assert _content(payload)["reasoning_content"] == "Done thinking."


def test_route_metadata_is_attached_to_stream_events() -> None:
    adapter = SSEAdapter(
        "thread-1",
        "run-1",
        route_metadata={
            "qwen_stage": "B",
            "qwen_route_mode": "stage_b_dashscope_execution",
            "qwen_endpoint_family": "multimodal_conversation",
        },
    )
    msg = _DummyMsg(
        blocks=[{"type": "text", "text": "writing..."}],
        text="writing...",
    )

    payload = adapter.convert(msg, is_last=False)
    metadata = _metadata(payload)

    assert metadata["stream_status"] == "chunk"
    assert metadata["qwen_stage"] == "B"
    assert metadata["qwen_route_mode"] == "stage_b_dashscope_execution"
    assert metadata["qwen_endpoint_family"] == "multimodal_conversation"
