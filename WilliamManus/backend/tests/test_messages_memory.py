"""Tests for MessagesTableMemory — role assignment, reasoning_content preservation."""

import json
import sys
from pathlib import Path

import pytest

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from agentscope.message import (
    Msg,
    ThinkingBlock,
    ToolUseBlock,
    ToolResultBlock,
    TextBlock,
)

from agentscope_integration.memory.messages_memory import MessagesTableMemory


class _MockDBClient:
    """Minimal DB client stub for unit-testing MessagesTableMemory."""

    pass


def _make_memory(**overrides) -> MessagesTableMemory:
    kwargs = {
        "db_client": _MockDBClient(),
        "thread_id": "test-thread",
        "project_id": "test-project",
    }
    kwargs.update(overrides)
    return MessagesTableMemory(**kwargs)


class TestRowToMsg:
    def test_tool_message_has_reasoning_preserved_and_tool_result_block(self) -> None:
        """Tool rows must preserve tool_result blocks and reasoning in content.

        Tool rows use role='user' at the Msg level so replayed tool observations
        have high salience for OpenAI-compatible reasoning models. The formatter
        still handles tool_result blocks inline and emits provider role='tool'.
        """
        memory = _make_memory()
        row = {
            "type": "tool",
            "content": json.dumps(
                {
                    "tool_call_id": "call_abc",
                    "tool_name": "web_search",
                    "result": "found it",
                }
            ),
            "metadata": "{}",
        }
        msg = memory._row_to_msg(row)
        assert msg is not None
        assert msg.role == "user"

    def test_assistant_message_has_role_assistant(self) -> None:
        """Assistant rows must keep role='assistant'."""
        memory = _make_memory()
        row = {
            "type": "assistant",
            "content": json.dumps(
                {
                    "content": "Here is the answer.",
                }
            ),
            "metadata": "{}",
        }
        msg = memory._row_to_msg(row)
        assert msg is not None
        assert msg.role == "assistant"

    def test_user_message_has_role_user(self) -> None:
        """User rows must keep role='user'."""
        memory = _make_memory()
        row = {
            "type": "user",
            "content": json.dumps(
                {
                    "content": "Hello",
                }
            ),
            "metadata": "{}",
        }
        msg = memory._row_to_msg(row)
        assert msg is not None
        assert msg.role == "user"

    def test_assistant_preserves_reasoning_content(self) -> None:
        """Assistant rows with reasoning_content must produce ThinkingBlock."""
        memory = _make_memory()
        row = {
            "type": "assistant",
            "content": json.dumps(
                {
                    "content": "Let me think...",
                    "reasoning_content": "Step 1: analyze. Step 2: respond.",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "web_search",
                                "arguments": '{"query": "test"}',
                            },
                        },
                    ],
                }
            ),
            "metadata": "{}",
        }
        msg = memory._row_to_msg(row)
        assert msg is not None

        blocks = msg.get_content_blocks()
        thinking_blocks = [b for b in blocks if b.get("type") == "thinking"]
        assert len(thinking_blocks) == 1
        assert thinking_blocks[0].get("thinking") == "Step 1: analyze. Step 2: respond."

        tool_blocks = [b for b in blocks if b.get("type") == "tool_use"]
        assert len(tool_blocks) == 1


class TestDropToolCallOnly:
    def test_preserves_message_with_reasoning_content_but_no_text(self) -> None:
        """drop_tool_call_only must NOT drop messages that have reasoning_content."""
        memory = _make_memory(drop_tool_call_only=True)

        row = {
            "type": "assistant",
            "content": json.dumps(
                {
                    "content": "",
                    "reasoning_content": "Need to search for this.",
                    "tool_calls": [
                        {
                            "id": "call_search",
                            "type": "function",
                            "function": {
                                "name": "web_search",
                                "arguments": '{"query": "deepseek"}',
                            },
                        },
                    ],
                }
            ),
            "metadata": json.dumps({}),
        }

        content = json.loads(row["content"])
        tool_calls = content.get("tool_calls")
        has_tool_calls = isinstance(tool_calls, list) and len(tool_calls) > 0
        assert has_tool_calls is True

        content_text = str(content.get("content") or "")
        reasoning_text = str(content.get("reasoning_content") or "")
        should_drop = not content_text.strip() and not reasoning_text.strip()
        assert (
            should_drop is False
        ), "Message with reasoning_content but no text should NOT be dropped"


class TestSkillRowProtection:
    """Skill-related rows must survive exclude_tool_calls filtering."""

    def test_skill_tool_result_is_skill_related(self):
        mem = _make_memory()
        row = {
            "type": "tool",
            "role": "assistant",
            "content": {"tool_name": "load_skill", "result": "skill content here"},
            "metadata": {},
        }
        assert mem._is_skill_related_row(row) is True

    def test_skill_assistant_tool_call_is_skill_related(self):
        mem = _make_memory()
        row = {
            "type": "assistant",
            "role": "assistant",
            "content": {
                "tool_calls": [
                    {"name": "load_skill", "arguments": '{"skill_name":"manim"}'}
                ],
            },
            "metadata": {},
        }
        assert mem._is_skill_related_row(row) is True

    def test_normal_tool_is_not_skill_related(self):
        mem = _make_memory()
        row = {
            "type": "tool",
            "role": "assistant",
            "content": {"tool_name": "execute_command", "result": "ok"},
            "metadata": {},
        }
        assert mem._is_skill_related_row(row) is False

    def test_run_skill_script_is_skill_related(self):
        mem = _make_memory()
        row = {
            "type": "tool",
            "role": "assistant",
            "content": {"tool_name": "run_skill_script", "result": "script output"},
            "metadata": {},
        }
        assert mem._is_skill_related_row(row) is True

    def test_non_dict_content_not_skill_related(self):
        mem = _make_memory()
        row = {
            "type": "tool",
            "role": "assistant",
            "content": "plain text",
            "metadata": {},
        }
        assert mem._is_skill_related_row(row) is False


class TestSkillExemptMultiRun:
    """E2E: skill rows from old runs survive exclude_tool_calls filtering."""

    @pytest.mark.asyncio
    async def test_old_skill_tool_result_exempted_from_filtering(self):
        """Old run's load_skill tool call + result must survive exclude_tool_calls."""
        old_run_id = "run-001"
        current_run_id = "run-002"

        mem = _make_memory(
            exclude_tool_calls=True,
            keep_tool_results=False,
            retain_complete_tool_runs=0,
            enable_tool_history_summary=False,
        )
        mem._save_thread_run_id = current_run_id

        rows = [
            # Old run — assistant with load_skill tool_call (should be EXEMPTED)
            {
                "type": "assistant",
                "content": {
                    "content": "Loading manim skill",
                    "tool_calls": [
                        {"id": "call_1", "name": "load_skill", "arguments": "{}"}
                    ],
                },
                "metadata": {"thread_run_id": old_run_id},
            },
            # Old run — load_skill tool result (should be EXEMPTED)
            {
                "type": "tool",
                "content": {
                    "tool_name": "load_skill",
                    "tool_call_id": "call_1",
                    "result": "manim skill loaded",
                },
                "metadata": {"thread_run_id": old_run_id},
            },
            # Old run — assistant with execute_command tool_call (should be FILTERED)
            {
                "type": "assistant",
                "content": {
                    "content": "Running command",
                    "tool_calls": [
                        {"id": "call_2", "name": "execute_command", "arguments": "{}"}
                    ],
                },
                "metadata": {"thread_run_id": old_run_id},
            },
            # Old run — execute_command tool result (should be FILTERED)
            {
                "type": "tool",
                "content": {
                    "tool_name": "execute_command",
                    "tool_call_id": "call_2",
                    "result": "ok",
                },
                "metadata": {"thread_run_id": old_run_id},
            },
            # Current run — user message (always kept)
            {
                "type": "user",
                "content": {"content": "What is the answer?"},
                "metadata": {"thread_run_id": current_run_id},
            },
        ]

        async def mock_load_rows(**kw):
            return rows

        def mock_row_to_msg(row):
            from agentscope.message import Msg

            role = "assistant" if row["type"] in ("tool", "assistant") else "user"
            return Msg(role, str(row.get("content", "")), role)

        mem._load_rows_from_db = mock_load_rows
        mem._row_to_msg = mock_row_to_msg

        msgs = await mem.get_memory()

        # Should have: exempted skill assistant + skill tool result + current user = 3 messages
        # (regular execute_command pair gets filtered entirely)
        assert (
            len(msgs) == 3
        ), f"Expected 3 messages (skill pair + user), got {len(msgs)}"

    @pytest.mark.asyncio
    async def test_skill_exempt_counter_increments(self):
        """skill_exempt counter should track exempted skill rows."""
        old_run_id = "run-a"
        current_run_id = "run-b"

        mem = _make_memory(
            exclude_tool_calls=True,
            keep_tool_results=False,
            retain_complete_tool_runs=0,
        )
        mem._save_thread_run_id = current_run_id

        rows = [
            {
                "type": "assistant",
                "content": {
                    "tool_calls": [
                        {"id": "c1", "name": "load_skill", "arguments": "{}"}
                    ]
                },
                "metadata": {"thread_run_id": old_run_id},
            },
            {
                "type": "tool",
                "content": {
                    "tool_name": "load_skill",
                    "tool_call_id": "c1",
                    "result": "x",
                },
                "metadata": {"thread_run_id": old_run_id},
            },
            {
                "type": "assistant",
                "content": {
                    "tool_calls": [
                        {"id": "c2", "name": "run_skill_script", "arguments": "{}"}
                    ]
                },
                "metadata": {"thread_run_id": old_run_id},
            },
            {
                "type": "tool",
                "content": {
                    "tool_name": "run_skill_script",
                    "tool_call_id": "c2",
                    "result": "y",
                },
                "metadata": {"thread_run_id": old_run_id},
            },
            {
                "type": "assistant",
                "content": {
                    "tool_calls": [
                        {"id": "c3", "name": "get_available_skills", "arguments": "{}"}
                    ]
                },
                "metadata": {"thread_run_id": old_run_id},
            },
            {
                "type": "tool",
                "content": {
                    "tool_name": "get_available_skills",
                    "tool_call_id": "c3",
                    "result": "z",
                },
                "metadata": {"thread_run_id": old_run_id},
            },
            {
                "type": "user",
                "content": {"content": "hi"},
                "metadata": {"thread_run_id": current_run_id},
            },
        ]

        async def mock_load(**kw):
            return rows

        def mock_to_msg(row):
            from agentscope.message import Msg

            return Msg(
                "user" if row["type"] == "user" else "assistant", "x", "assistant"
            )

        mem._load_rows_from_db = mock_load
        mem._row_to_msg = mock_to_msg
        msgs = await mem.get_memory()

        # 3 skill pairs (6 msgs) + 1 user = 7 messages (all 6 skill rows exempted)
        assert (
            len(msgs) == 7
        ), f"Expected 7 messages (6 skill + 1 user), got {len(msgs)}"


class _TerminalFenceQuery:
    def __init__(self, client, table_name):
        self._client = client
        self._table_name = table_name

    def select(self, *_args, **_kwargs):
        return self

    def eq(self, *_args, **_kwargs):
        return self

    def limit(self, *_args, **_kwargs):
        return self

    async def execute(self):
        if self._table_name == "agent_runs":
            return type(
                "Result",
                (),
                {"data": [{"status": self._client.agent_run_status}]},
            )()
        return type("Result", (), {"data": []})()

    async def insert(self, payload):
        self._client.inserted_messages.append(payload)
        return type("Result", (), {"data": [payload]})()


class _TerminalFenceDBClient:
    def __init__(self, status):
        self.agent_run_status = status
        self.inserted_messages = []

    def table(self, table_name):
        return _TerminalFenceQuery(self, table_name)


class TestTerminalRunFence:
    @pytest.mark.asyncio
    async def test_add_skips_message_when_agent_run_is_terminal(self):
        db_client = _TerminalFenceDBClient(status="stopped")
        mem = MessagesTableMemory(
            db_client=db_client,
            thread_id="thread-1",
            project_id="project-1",
            thread_run_id="run-1",
        )

        result = await mem.add(Msg("assistant", "late message", "assistant"))

        assert result is None
        assert db_client.inserted_messages == []


class TestTailTrimHumanTaskContract:
    def test_tail_trim_preserves_initial_human_user_when_tool_results_are_newer(self):
        mem = _make_memory()
        mem.tail_trim_keep_turns = 2

        messages = [
            Msg("system", "system prompt", "system"),
            Msg(
                "user",
                "ORIGINAL TASK: create markdown and html wrong-MCQ report",
                "user",
            ),
        ]
        for idx in range(12):
            tool_msg = Msg("tool", f"tool result {idx}", "user")
            tool_msg.metadata = {"tool_name": "execute_command"}
            messages.append(tool_msg)

        trimmed = mem._apply_tail_trim(messages)
        text = "\n".join(str(msg.get_text_content() or "") for msg in trimmed)

        assert "ORIGINAL TASK: create markdown and html wrong-MCQ report" in text
        assert len(trimmed) < len(messages)
        assert trimmed[0].role == "system"
