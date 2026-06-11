import asyncio
import importlib.util
import json
import sys
import types
import uuid
from pathlib import Path

from agentscope.message import Msg, TextBlock, ToolResultBlock, ToolUseBlock

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

    dummy_structlog = types.SimpleNamespace(
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
    sys.modules["structlog"] = dummy_structlog

MODULE_PATH = BACKEND_ROOT / "agentscope_integration" / "memory" / "messages_memory.py"
spec = importlib.util.spec_from_file_location("messages_memory_nul", MODULE_PATH)
module = importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules["messages_memory_nul"] = module
spec.loader.exec_module(module)

MessagesTableMemory = module.MessagesTableMemory
safe_json_dumps = module.safe_json_dumps


class _FakeResult:
    def __init__(self, data):
        self.data = data


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    async def insert(self, payload):
        row = dict(payload)
        row.setdefault("message_id", str(uuid.uuid4()))
        self._rows.append(row)
        return _FakeResult([row])


class _FakeDB:
    def __init__(self, rows):
        self.rows = rows

    def table(self, _name):
        return _FakeQuery(self.rows)


def test_safe_json_dumps_strips_nul_chars_recursively() -> None:
    payload = {
        "te\x00xt": "hello\x00world",
        "nested": [{"ke\x00y": "va\x00lue"}, ["\x00prefix", "suf\x00fix"]],
        "blob": b"bi\x00n",
    }

    serialized = safe_json_dumps(payload)

    assert "\\u0000" not in serialized
    parsed = json.loads(serialized)
    assert parsed["text"] == "helloworld"
    assert parsed["nested"][0]["key"] == "value"
    assert parsed["nested"][1][0] == "prefix"
    assert parsed["nested"][1][1] == "suffix"
    assert parsed["blob"] == "bin"


def test_memory_add_sanitizes_nul_chars_in_content_and_metadata() -> None:
    db = _FakeDB([])
    memory = MessagesTableMemory(
        db_client=db,
        thread_id="thread-1",
    )

    msg = Msg(
        "user",
        "he\x00llo",
        "user",
        metadata={"so\x00urce": "uploa\x00d", "nested": {"va\x00lue": "x\x00y"}},
    )

    result = asyncio.run(memory.add(msg))

    assert result is not None
    row = db.rows[0]
    assert "\\u0000" not in row["content"]
    assert "\\u0000" not in row["metadata"]

    content = json.loads(row["content"])
    metadata = json.loads(row["metadata"])
    assert content["content"] == "hello"
    assert metadata["source"] == "upload"
    assert metadata["nested"]["value"] == "xy"


def test_memory_add_sanitizes_tool_result_output_with_nul_chars() -> None:
    db = _FakeDB([])
    memory = MessagesTableMemory(
        db_client=db,
        thread_id="thread-2",
    )

    tool_result_msg = Msg(
        "assistant",
        [
            ToolResultBlock(
                type="tool_result",
                id="call-1",
                name="execute_command",
                output=[TextBlock(type="text", text="std\x00out")],
            )
        ],
        "assistant",
        metadata={"tool_na\x00me": "execute_\x00command"},
    )

    result = asyncio.run(memory.add(tool_result_msg))

    assert result is not None
    row = db.rows[0]
    assert row["type"] == "tool"
    assert "\\u0000" not in row["content"]
    assert "\\u0000" not in row["metadata"]

    content = json.loads(row["content"])
    metadata = json.loads(row["metadata"])
    assert content["result"] == "stdout"
    assert metadata["tool_name"] == "execute_command"


def test_row_to_msg_restores_provider_metadata_for_next_round_replay() -> None:
    db = _FakeDB([])
    memory = MessagesTableMemory(
        db_client=db,
        thread_id="thread-3",
    )

    row = {
        "type": "assistant",
        "content": json.dumps(
            {
                "role": "assistant",
                "content": "",
                "reasoning_content": "intermediate reasoning",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {
                            "name": "write_file",
                            "arguments": json.dumps(
                                {
                                    "file_path": "/workspace/demo.txt",
                                    "content": "hello",
                                }
                            ),
                        },
                    }
                ],
            }
        ),
        "metadata": json.dumps(
            {
                "_openrouter_reasoning_details": [
                    {"type": "reasoning.text", "text": "provider replay payload"}
                ],
                "custom_flag": "kept",
            }
        ),
    }

    msg = memory._row_to_msg(row)

    assert msg is not None
    assert getattr(msg, "metadata", {})["_openrouter_reasoning_details"][0]["text"] == "provider replay payload"
    assert getattr(msg, "metadata", {})["custom_flag"] == "kept"


# ---------------------------------------------------------------------------
# _row_to_msg placeholder replacement for time_compacted tool rows
# ---------------------------------------------------------------------------

# Import PRUNE_PLACEHOLDER from micro_compact (same package as messages_memory)
_mc_spec = importlib.util.spec_from_file_location(
    "micro_compact_test",
    BACKEND_ROOT / "agentscope_integration" / "memory" / "micro_compact.py",
)
_mc_module = importlib.util.module_from_spec(_mc_spec)
assert _mc_spec and _mc_spec.loader
sys.modules["micro_compact_test"] = _mc_module
_mc_spec.loader.exec_module(_mc_module)
PRUNE_PLACEHOLDER = _mc_module.PRUNE_PLACEHOLDER


def test_row_to_msg_replaces_compacted_tool_content_with_placeholder() -> None:
    """_row_to_msg replaces tool result with PRUNE_PLACEHOLDER when time_compacted is set."""
    db = _FakeDB([])
    memory = MessagesTableMemory(db_client=db, thread_id="thread-compact-1")

    row = {
        "type": "tool",
        "content": json.dumps({
            "tool_name": "execute_command",
            "result": "very long command output that should be hidden",
            "tool_call_id": "call-abc-123",
        }),
        "metadata": json.dumps({
            "time_compacted": "2026-01-01T00:00:00+00:00",
        }),
    }

    msg = memory._row_to_msg(row)

    assert msg is not None
    # ToolResultBlock is a TypedDict, so at runtime it's a plain dict
    tool_blocks = [b for b in msg.content if isinstance(b, dict) and b.get("type") == "tool_result"]
    assert len(tool_blocks) == 1
    assert tool_blocks[0]["name"] == "execute_command"
    assert tool_blocks[0]["id"] == "call-abc-123"
    assert len(tool_blocks[0]["output"]) == 1
    assert tool_blocks[0]["output"][0]["text"] == PRUNE_PLACEHOLDER


def test_row_to_msg_keeps_original_tool_content_when_not_compacted() -> None:
    """_row_to_msg returns original result when time_compacted is absent."""
    db = _FakeDB([])
    memory = MessagesTableMemory(db_client=db, thread_id="thread-compact-2")

    original_result = "ls -la output: total 42\ndrwxr-xr-x ..."
    row = {
        "type": "tool",
        "content": json.dumps({
            "tool_name": "execute_command",
            "result": original_result,
            "tool_call_id": "call-def-456",
        }),
        "metadata": json.dumps({"marks": "default"}),
    }

    msg = memory._row_to_msg(row)

    assert msg is not None
    tool_blocks = [b for b in msg.content if isinstance(b, dict) and b.get("type") == "tool_result"]
    assert len(tool_blocks) == 1
    assert tool_blocks[0]["output"][0]["text"] == original_result


def test_row_to_msg_placeholder_only_affects_tool_rows() -> None:
    """Non-tool rows with time_compacted metadata are unaffected."""
    db = _FakeDB([])
    memory = MessagesTableMemory(db_client=db, thread_id="thread-compact-3")

    compacted_meta = json.dumps({"time_compacted": "2026-01-01T00:00:00+00:00"})

    # User row
    user_row = {
        "type": "user",
        "content": json.dumps({"content": "hello world"}),
        "metadata": compacted_meta,
    }
    user_msg = memory._row_to_msg(user_row)
    assert user_msg is not None
    user_text_blocks = [b for b in user_msg.content if isinstance(b, dict) and b.get("type") == "text"]
    assert any("hello world" in b["text"] for b in user_text_blocks)

    # Assistant row with text content
    asst_row = {
        "type": "assistant",
        "content": json.dumps({
            "role": "assistant",
            "content": "I will help with that",
        }),
        "metadata": compacted_meta,
    }
    asst_msg = memory._row_to_msg(asst_row)
    assert asst_msg is not None
    asst_text_blocks = [b for b in asst_msg.content if isinstance(b, dict) and b.get("type") == "text"]
    assert any("I will help with that" in b["text"] for b in asst_text_blocks)


def test_row_to_msg_preserves_non_compaction_metadata() -> None:
    """Metadata fields other than time_compacted are preserved."""
    db = _FakeDB([])
    memory = MessagesTableMemory(db_client=db, thread_id="thread-compact-4")

    row = {
        "type": "tool",
        "content": json.dumps({
            "tool_name": "read_file",
            "result": "file contents here",
            "tool_call_id": "call-ghi-789",
        }),
        "metadata": json.dumps({
            "time_compacted": "2026-01-01T00:00:00+00:00",
            "marks": "important",
            "custom_key": "custom_value",
        }),
    }

    msg = memory._row_to_msg(row)
    assert msg is not None

    msg_meta = getattr(msg, "metadata", {})
    assert msg_meta.get("time_compacted") is not None
    assert msg_meta.get("marks") == "important"
    assert msg_meta.get("custom_key") == "custom_value"


def test_row_to_msg_handles_missing_result_with_placeholder() -> None:
    """When time_compacted is set and result is missing/empty, placeholder still works."""
    db = _FakeDB([])
    memory = MessagesTableMemory(db_client=db, thread_id="thread-compact-5")

    # Missing result key entirely
    row_no_result = {
        "type": "tool",
        "content": json.dumps({
            "tool_name": "web_search",
            "tool_call_id": "call-jkl-000",
        }),
        "metadata": json.dumps({"time_compacted": "2026-01-01T00:00:00+00:00"}),
    }

    msg = memory._row_to_msg(row_no_result)
    assert msg is not None
    tool_blocks = [b for b in msg.content if isinstance(b, dict) and b.get("type") == "tool_result"]
    assert len(tool_blocks) == 1
    assert tool_blocks[0]["output"][0]["text"] == PRUNE_PLACEHOLDER

    # Empty result
    row_empty_result = {
        "type": "tool",
        "content": json.dumps({
            "tool_name": "web_search",
            "result": "",
            "tool_call_id": "call-mno-111",
        }),
        "metadata": json.dumps({"time_compacted": "2026-01-01T00:00:00+00:00"}),
    }

    msg2 = memory._row_to_msg(row_empty_result)
    assert msg2 is not None
    tool_blocks2 = [b for b in msg2.content if isinstance(b, dict) and b.get("type") == "tool_result"]
    assert len(tool_blocks2) == 1
    assert tool_blocks2[0]["output"][0]["text"] == PRUNE_PLACEHOLDER
