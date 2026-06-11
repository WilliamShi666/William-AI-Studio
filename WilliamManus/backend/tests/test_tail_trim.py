"""Unit tests for TailTrim."""

import pytest
from agentscope_integration.cache.tail_trim import (
    TailTrimConfig,
    tail_trim,
)


class TestTailTrim:
    def test_under_threshold_noop(self):
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"},
        ]
        result = tail_trim(msgs, TailTrimConfig(
            trigger_message_count=100, keep_tail_turns=2,
        ))
        assert result.removed_count == 0
        assert result.prefix_preserved

    def test_prefix_preserved(self):
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"},
            {"role": "tool", "content": "t1", "tool_name": "read_file"},
            {"role": "user", "content": "q2"},
            {"role": "assistant", "content": "a2"},
            {"role": "tool", "content": "t2", "tool_name": "read_file"},
            {"role": "user", "content": "q3"},
            {"role": "assistant", "content": "a3"},
        ]
        result = tail_trim(msgs, TailTrimConfig(
            trigger_message_count=5, target_max_messages=4,
            keep_tail_turns=1, min_prefix_messages=2,
        ))
        assert result.prefix_preserved
        # Prefix (first 2 messages: sys + user) should be preserved
        assert result.trimmed_messages[0]["role"] == "system"
        assert result.trimmed_messages[1]["role"] == "user"

    def test_tail_preserved(self):
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"},
            {"role": "tool", "content": "t1", "tool_name": "read_file"},
            {"role": "user", "content": "q2"},
            {"role": "assistant", "content": "a2"},
        ]
        result = tail_trim(msgs, TailTrimConfig(
            trigger_message_count=3, target_max_messages=3,
            keep_tail_turns=1, min_prefix_messages=1,
        ))
        # Last user message (q2) and assistant response (a2) should be in tail
        trimmed_roles = [m["role"] for m in result.trimmed_messages]
        assert "user" in trimmed_roles[-2:]
        assert len(result.trimmed_messages) < len(msgs)
        assert result.removed_count > 0

    def test_result_summary_info(self):
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "q1"},
            {"role": "assistant", "content": "a1"},
        ]
        result = tail_trim(msgs, TailTrimConfig(
            trigger_message_count=1, target_max_messages=1,
        ))
        assert isinstance(result.summary, str)
        assert isinstance(result.removed_range, tuple)
        assert len(result.removed_range) == 2
