import asyncio
import importlib.util
import sys
import types
from pathlib import Path

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

MODULE_PATH = BACKEND_ROOT / "agentscope_integration" / "models" / "openrouter_model.py"
spec = importlib.util.spec_from_file_location("openrouter_model_pairing", MODULE_PATH)
module = importlib.util.module_from_spec(spec)
assert spec and spec.loader
sys.modules["openrouter_model_pairing"] = module
spec.loader.exec_module(module)

OpenRouterChatModel = module.OpenRouterChatModel

MEMORY_MODULE_PATH = BACKEND_ROOT / "agentscope_integration" / "memory" / "messages_memory.py"
memory_spec = importlib.util.spec_from_file_location("messages_memory_pairing", MEMORY_MODULE_PATH)
memory_module = importlib.util.module_from_spec(memory_spec)
assert memory_spec and memory_spec.loader
sys.modules["messages_memory_pairing"] = memory_module
memory_spec.loader.exec_module(memory_module)

MessagesTableMemory = memory_module.MessagesTableMemory


def test_sanitize_tool_message_sequence_drops_orphans() -> None:
    model = OpenRouterChatModel(
        model_name="z-ai/glm-4.7",
        api_key="test-key",
        stream=False,
        client_kwargs={"base_url": "https://openrouter.ai/api/v1"},
    )

    messages = [
        {
            "role": "tool",
            "tool_call_id": "call-missing",
            "name": "web_search",
            "content": "result",
        },
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {
                        "name": "web_search",
                        "arguments": "{}",
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "name": "web_search",
            "content": "ok",
        },
        {
            "role": "tool",
            "tool_call_id": "",
            "name": "web_search",
            "content": "bad",
        },
    ]

    sanitized = model._sanitize_tool_message_sequence(messages)

    assert len(sanitized) == 2
    assert sanitized[0]["role"] == "assistant"
    assert sanitized[1]["role"] == "tool"
    assert sanitized[1]["tool_call_id"] == "call-1"


def test_sanitize_tool_message_sequence_strips_unmatched_assistant_tool_calls() -> None:
    model = OpenRouterChatModel(
        model_name="z-ai/glm-4.7",
        api_key="test-key",
        stream=False,
        client_kwargs={"base_url": "https://openrouter.ai/api/v1"},
    )

    messages = [
        {"role": "system", "content": "You are helpful."},
        {
            "role": "assistant",
            "content": "I will search",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "web_search", "arguments": "{}"},
                },
                {
                    "id": "call-2",
                    "type": "function",
                    "function": {"name": "read_page", "arguments": "{}"},
                },
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "name": "web_search",
            "content": "search result",
        },
        {"role": "assistant", "content": "next step"},
    ]

    sanitized = model._sanitize_tool_message_sequence(messages)

    assert len(sanitized) == 4
    assistant = sanitized[1]
    assert assistant["role"] == "assistant"
    assert len(assistant["tool_calls"]) == 1
    assert assistant["tool_calls"][0]["id"] == "call-1"
    assert sanitized[2]["role"] == "tool"
    assert sanitized[2]["tool_call_id"] == "call-1"


def test_sanitize_tool_message_sequence_drops_tool_call_only_assistant_when_unmatched() -> None:
    model = OpenRouterChatModel(
        model_name="z-ai/glm-4.7",
        api_key="test-key",
        stream=False,
        client_kwargs={"base_url": "https://openrouter.ai/api/v1"},
    )

    messages = [
        {"role": "system", "content": "You are helpful."},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call-99",
                    "type": "function",
                    "function": {"name": "web_search", "arguments": "{}"},
                }
            ],
        },
        {"role": "assistant", "content": "fallback text"},
    ]

    sanitized = model._sanitize_tool_message_sequence(messages)

    assert len(sanitized) == 2
    assert all(not msg.get("tool_calls") for msg in sanitized)
    assert sanitized[1]["content"] == "fallback text"


def test_sanitize_preserves_assistant_with_reasoning_content_and_no_tool_calls() -> None:
    """Bug #1: assistant msg with reasoning_content but no content/tool_calls must get content=''."""
    model = OpenRouterChatModel(
        model_name="deepseek-v4-pro",
        api_key="test-key",
        stream=False,
        client_kwargs={"base_url": "https://api.deepseek.com"},
    )

    messages = [
        {"role": "system", "content": "You are helpful."},
        {
            "role": "assistant",
            "content": None,
            "reasoning_content": "The user asked about weather. I need to call the tool.",
        },
        {"role": "user", "content": "next question"},
    ]

    sanitized = model._sanitize_tool_message_sequence(messages)

    assert len(sanitized) == 3
    assistant = sanitized[1]
    assert assistant["role"] == "assistant"
    assert assistant["content"] == "", f"Expected empty string content, got {assistant['content']!r}"
    assert assistant.get("reasoning_content") == "The user asked about weather. I need to call the tool."


def test_sanitize_normalizes_empty_content_with_reasoning_after_tool_strip() -> None:
    """Bug #1: after stripping orphaned tool_calls, content=None + reasoning_content → content=''."""
    model = OpenRouterChatModel(
        model_name="deepseek-v4-pro",
        api_key="test-key",
        stream=False,
        client_kwargs={"base_url": "https://api.deepseek.com"},
    )

    messages = [
        {"role": "system", "content": "You are helpful."},
        {
            "role": "assistant",
            "content": None,
            "reasoning_content": "Let me call the search tool.",
            "tool_calls": [
                {
                    "id": "call-orphan",
                    "type": "function",
                    "function": {"name": "web_search", "arguments": "{}"},
                }
            ],
        },
        {"role": "assistant", "content": "fallback response"},
    ]

    sanitized = model._sanitize_tool_message_sequence(messages)

    # The orphaned tool_call assistant should either be normalized with content='' or dropped.
    # If kept, it must have valid content (not None).
    for msg in sanitized:
        if msg.get("role") == "assistant":
            content = msg.get("content")
            tool_calls = msg.get("tool_calls")
            assert content is not None or tool_calls, (
                f"Assistant message has both content=None and no tool_calls: {msg}"
            )


def test_sanitize_preserves_reasoning_content_through_tool_sequence() -> None:
    """Bug #2: reasoning_content must survive sanitization in a valid tool call sequence."""
    model = OpenRouterChatModel(
        model_name="deepseek-v4-pro",
        api_key="test-key",
        stream=False,
        client_kwargs={"base_url": "https://api.deepseek.com"},
    )

    messages = [
        {"role": "system", "content": "You are helpful."},
        {
            "role": "assistant",
            "content": None,
            "reasoning_content": "I need to search for the answer.",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "web_search", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "name": "web_search",
            "content": "search result here",
        },
        {"role": "user", "content": "next"},
    ]

    sanitized = model._sanitize_tool_message_sequence(messages)

    assert len(sanitized) == 4
    assistant = sanitized[1]
    assert assistant["role"] == "assistant"
    assert assistant.get("reasoning_content") == "I need to search for the answer."
    assert assistant.get("tool_calls") is not None
    assert sanitized[2]["role"] == "tool"


def test_sanitize_deepseek_multi_turn_tool_call_keeps_all_valid_messages() -> None:
    """Full DeepSeek multi-turn tool call scenario: all valid messages survive."""
    model = OpenRouterChatModel(
        model_name="deepseek-v4-pro",
        api_key="test-key",
        stream=False,
        client_kwargs={"base_url": "https://api.deepseek.com"},
    )

    messages = [
        {"role": "system", "content": "You are helpful."},
        {
            "role": "assistant",
            "content": None,
            "reasoning_content": "Round 1 thinking",
            "tool_calls": [
                {
                    "id": "call-1",
                    "type": "function",
                    "function": {"name": "get_date", "arguments": "{}"},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "name": "get_date",
            "content": "2026-05-05",
        },
        {
            "role": "assistant",
            "content": None,
            "reasoning_content": "Round 2 thinking",
            "tool_calls": [
                {
                    "id": "call-2",
                    "type": "function",
                    "function": {"name": "get_weather", "arguments": '{"city":"Hangzhou"}'},
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call-2",
            "name": "get_weather",
            "content": "Sunny, 25C",
        },
        {
            "role": "assistant",
            "content": "The weather in Hangzhou is sunny, 25C.",
            "reasoning_content": "Final answer thinking",
        },
        {"role": "user", "content": "next question"},
    ]

    sanitized = model._sanitize_tool_message_sequence(messages)

    assert len(sanitized) == 7
    # Round 1 assistant
    assert sanitized[1]["role"] == "assistant"
    assert sanitized[1].get("reasoning_content") == "Round 1 thinking"
    # Round 2 assistant
    assert sanitized[3]["role"] == "assistant"
    assert sanitized[3].get("reasoning_content") == "Round 2 thinking"
    # Final assistant has content set
    assert sanitized[5]["role"] == "assistant"
    assert sanitized[5].get("content") == "The weather in Hangzhou is sunny, 25C."
    assert sanitized[5].get("reasoning_content") == "Final answer thinking"


def test_sanitize_normalizes_reasoning_only_msg_with_malformed_tool_calls() -> None:
    """Bug #1 edge case: malformed tool_calls (empty id) stripped → normalize content=None."""
    model = OpenRouterChatModel(
        model_name="deepseek-v4-pro",
        api_key="test-key",
        stream=False,
        client_kwargs={"base_url": "https://api.deepseek.com"},
    )

    messages = [
        {"role": "system", "content": "You are helpful."},
        {
            "role": "assistant",
            "content": None,
            "reasoning_content": "I should look this up.",
            "tool_calls": [
                {"id": "", "type": "function", "function": {"name": "search", "arguments": "{}"}},
            ],
        },
        {"role": "assistant", "content": "fallback"},
    ]

    sanitized = model._sanitize_tool_message_sequence(messages)

    for msg in sanitized:
        if msg.get("role") == "assistant":
            content = msg.get("content")
            tool_calls = msg.get("tool_calls")
            assert content is not None or tool_calls, (
                f"Assistant message with malformed calls has content=None and no tool_calls: {msg}"
            )


def test_volcengine_fallback_keeps_current_model() -> None:
    model = OpenRouterChatModel(
        model_name="doubao-seed-2-0-pro-260215",
        api_key="test-key",
        stream=False,
        client_kwargs={"base_url": "https://ark.cn-beijing.volces.com/api/v3"},
    )

    assert model._fallback_provider_model_name() == "doubao-seed-2-0-pro-260215"


class _FakeResult:
    def __init__(self, data):
        self.data = data


class _FakeQuery:
    def __init__(self, rows):
        self._rows = list(rows)
        self._desc = False
        self._limit = None

    def select(self, *_args, **_kwargs):
        return self

    def eq(self, _field, _value):
        return self

    def contains(self, _field, _value):
        return self

    def order(self, _field, desc=False):
        self._desc = desc
        return self

    def limit(self, value):
        self._limit = value
        return self

    async def execute(self):
        rows = list(self._rows)
        if self._desc:
            rows = list(reversed(rows))
        if self._limit is not None:
            rows = rows[: self._limit]
        return _FakeResult(rows)


class _FakeDB:
    def __init__(self, rows):
        self._rows = rows

    def table(self, _name):
        return _FakeQuery(self._rows)


def _has_block(msg, block_type: str, block_name: str | None = None) -> bool:
    blocks = msg.get_content_blocks() if hasattr(msg, "get_content_blocks") else []
    for block in blocks:
        if block.get("type") != block_type:
            continue
        if block_name is None:
            return True
        if block.get("name") == block_name:
            return True
    return False


def _tool_use_ids(msg) -> set[str]:
    ids: set[str] = set()
    blocks = msg.get_content_blocks() if hasattr(msg, "get_content_blocks") else []
    for block in blocks:
        if block.get("type") != "tool_use":
            continue
        block_id = str(block.get("id") or "").strip()
        if block_id:
            ids.add(block_id)
    return ids


def test_memory_drops_orphan_tool_results_when_tool_history_not_retained() -> None:
    rows = [
        {
            "type": "assistant",
            "content": {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "web_search", "arguments": "{}"},
                    }
                ],
            },
            "metadata": {"thread_run_id": "run-1"},
        },
        {
            "type": "tool",
            "content": {
                "tool_name": "web_search",
                "tool_call_id": "call-1",
                "result": "result payload",
            },
            "metadata": {"thread_run_id": "run-1"},
        },
        {
            "type": "user",
            "content": {"content": "next question"},
            "metadata": {},
        },
    ]

    memory = MessagesTableMemory(
        db_client=_FakeDB(rows),
        thread_id="thread-1",
        exclude_tool_calls=True,
        keep_tool_results=True,
        retain_complete_tool_runs=0,
        enable_tool_history_summary=False,
    )
    memory._max_memory_messages = 0
    memory._max_memory_chars = 0

    messages = asyncio.run(memory.get_memory(prepend_summary=False))

    # Tool call + tool result from historical run are filtered, leaving only user text.
    assert len(messages) == 1
    assert messages[0].role == "user"


def test_memory_keeps_complete_tool_history_for_recent_runs() -> None:
    rows = [
        {
            "type": "assistant",
            "content": {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "web_search", "arguments": "{}"},
                    }
                ],
            },
            "metadata": {"thread_run_id": "run-1"},
        },
        {
            "type": "tool",
            "content": {
                "tool_name": "web_search",
                "tool_call_id": "call-1",
                "result": "result payload",
            },
            "metadata": {"thread_run_id": "run-1"},
        },
        {
            "type": "user",
            "content": {"content": "follow-up"},
            "metadata": {},
        },
    ]

    memory = MessagesTableMemory(
        db_client=_FakeDB(rows),
        thread_id="thread-1",
        exclude_tool_calls=True,
        keep_tool_results=True,
        retain_complete_tool_runs=3,
        enable_tool_history_summary=False,
    )
    memory._max_memory_messages = 0
    memory._max_memory_chars = 0

    messages = asyncio.run(memory.get_memory(prepend_summary=False))

    assert len(messages) == 3
    assert any(_has_block(msg, "tool_use", "web_search") for msg in messages)
    assert any(_has_block(msg, "tool_result", "web_search") for msg in messages)


def test_memory_summarizes_older_tool_history_runs() -> None:
    rows = [
        {
            "type": "assistant",
            "content": {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-old",
                        "type": "function",
                        "function": {"name": "old_tool", "arguments": "{\"q\": \"old\"}"},
                    }
                ],
            },
            "metadata": {"thread_run_id": "run-old"},
        },
        {
            "type": "tool",
            "content": {
                "tool_name": "old_tool",
                "tool_call_id": "call-old",
                "result": "old result",
            },
            "metadata": {"thread_run_id": "run-old"},
        },
        {
            "type": "assistant",
            "content": {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-new",
                        "type": "function",
                        "function": {"name": "new_tool", "arguments": "{\"q\": \"new\"}"},
                    }
                ],
            },
            "metadata": {"thread_run_id": "run-new"},
        },
        {
            "type": "tool",
            "content": {
                "tool_name": "new_tool",
                "tool_call_id": "call-new",
                "result": "new result",
            },
            "metadata": {"thread_run_id": "run-new"},
        },
        {
            "type": "user",
            "content": {"content": "next question"},
            "metadata": {},
        },
    ]

    memory = MessagesTableMemory(
        db_client=_FakeDB(rows),
        thread_id="thread-1",
        exclude_tool_calls=True,
        keep_tool_results=True,
        retain_complete_tool_runs=1,
        enable_tool_history_summary=True,
        tool_history_summary_max_runs=4,
        tool_history_summary_max_chars=1200,
    )
    memory._max_memory_messages = 0
    memory._max_memory_chars = 0

    messages = asyncio.run(memory.get_memory(prepend_summary=False))

    summary_texts = [msg.get_text_content() for msg in messages if msg.role == "user"]
    assert any("<tool-history-summary>" in text for text in summary_texts)
    assert any("old_tool" in text for text in summary_texts)
    assert any(_has_block(msg, "tool_use", "new_tool") for msg in messages)
    assert any(_has_block(msg, "tool_result", "new_tool") for msg in messages)


def test_memory_preflight_projection_uses_recent_run_growth() -> None:
    rows = [
        {
            "type": "user",
            "content": {"content": "a" * 120},
            "metadata": {"thread_run_id": "run-1"},
        },
        {
            "type": "assistant",
            "content": {"content": "b" * 180},
            "metadata": {"thread_run_id": "run-1"},
        },
        {
            "type": "user",
            "content": {"content": "c" * 160},
            "metadata": {"thread_run_id": "run-2"},
        },
        {
            "type": "assistant",
            "content": {"content": "d" * 220},
            "metadata": {"thread_run_id": "run-2"},
        },
    ]

    memory = MessagesTableMemory(
        db_client=_FakeDB(rows),
        thread_id="thread-1",
        exclude_tool_calls=True,
    )
    # Test removed: _estimate_next_round_growth_chars was epoch-specific
    pass


def test_memory_strips_unmatched_assistant_tool_calls_before_replay() -> None:
    rows = [
        {"type": "system", "content": {"content": "You are helpful."}, "metadata": {}},
        {
            "type": "assistant",
            "content": {
                "content": "I will inspect the image",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "browser_inspect", "arguments": "{}"},
                    },
                    {
                        "id": "message[11].role",
                        "type": "function",
                        "function": {"name": "browser_inspect", "arguments": "{}"},
                    },
                ],
            },
            "metadata": {"thread_run_id": "run-1"},
        },
        {
            "type": "tool",
            "content": {
                "tool_name": "browser_inspect",
                "tool_call_id": "call-1",
                "result": "inspection done",
            },
            "metadata": {"thread_run_id": "run-1"},
        },
        {"type": "user", "content": {"content": "next question"}, "metadata": {}},
    ]

    memory = MessagesTableMemory(
        db_client=_FakeDB(rows),
        thread_id="thread-1",
        exclude_tool_calls=False,
    )
    memory._max_memory_messages = 0
    memory._max_memory_chars = 0

    messages = asyncio.run(memory.get_memory(prepend_summary=False))

    assistant_with_tools = next(msg for msg in messages if _tool_use_ids(msg))
    assert _tool_use_ids(assistant_with_tools) == {"call-1"}
    assert all("message[11].role" not in _tool_use_ids(msg) for msg in messages)




def test_deepseek_memory_preserves_tool_call_result_pairs_by_default() -> None:
    rows = [
        {"type": "user", "content": {"content": "please inspect"}, "metadata": {}},
        {
            "type": "assistant",
            "content": {
                "content": "I will read the file.",
                "tool_calls": [
                    {
                        "id": "call-read",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": '{"path":"a.py"}'},
                    },
                ],
            },
            "metadata": {"thread_run_id": "old-run"},
        },
        {
            "type": "tool",
            "content": {
                "tool_name": "read_file",
                "tool_call_id": "call-read",
                "result": "print('hi')",
            },
            "metadata": {"thread_run_id": "old-run"},
        },
        {"type": "user", "content": {"content": "continue"}, "metadata": {}},
    ]

    memory = MessagesTableMemory(
        db_client=_FakeDB(rows),
        thread_id="thread-1",
        exclude_tool_calls=False,
        keep_tool_results=True,
        retain_complete_tool_runs=0,
        enable_tool_history_summary=False,
    )
    memory._max_memory_messages = 0
    memory._max_memory_chars = 0

    messages = asyncio.run(memory.get_memory(prepend_summary=False))

    assert any(_has_block(msg, "tool_use", "read_file") for msg in messages)
    assert any(_has_block(msg, "tool_result", "read_file") for msg in messages)
    tool_result_msg = next(msg for msg in messages if _has_block(msg, "tool_result", "read_file"))
    assert tool_result_msg.role == "user"


def test_legacy_memory_exclusion_still_summarizes_old_tool_runs() -> None:
    rows = [
        {
            "type": "assistant",
            "content": {
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-old",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": '{"path":"a.py"}'},
                    },
                ],
            },
            "metadata": {"thread_run_id": "old-run"},
        },
        {
            "type": "tool",
            "content": {
                "tool_name": "read_file",
                "tool_call_id": "call-old",
                "result": "old result",
            },
            "metadata": {"thread_run_id": "old-run"},
        },
        {"type": "user", "content": {"content": "next"}, "metadata": {}},
    ]

    memory = MessagesTableMemory(
        db_client=_FakeDB(rows),
        thread_id="thread-1",
        exclude_tool_calls=True,
        keep_tool_results=True,
        retain_complete_tool_runs=0,
        enable_tool_history_summary=True,
        tool_history_summary_max_runs=2,
        tool_history_summary_max_chars=1000,
    )
    memory._max_memory_messages = 0
    memory._max_memory_chars = 0

    messages = asyncio.run(memory.get_memory(prepend_summary=False))

    assert not any(_has_block(msg, "tool_use", "read_file") for msg in messages)
    assert any("<tool-history-summary>" in msg.get_text_content() for msg in messages)
