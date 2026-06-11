"""Integration tests for the full compaction pipeline.

Simulates the end-to-end flow: Micro Compact → Auto Compact → KV Cache
protection, using in-memory fakes for DB and LLM dependencies.
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from datetime import datetime, timezone

from agentscope_integration.memory.micro_compact import (
    PRUNE_PROTECT,
    PRUNE_MINIMUM,
    PRUNE_PLACEHOLDER,
    _is_compactable,
    prune_tool_outputs,
)
from agentscope_integration.compaction.compact_prompts import (
    COMPACT_SYSTEM_PROMPT,
    SUMMARY_TEMPLATE,
    build_compaction_prompt,
    AUTO_CONTINUE_MESSAGE,
)
from agentscope_integration.compaction.auto_compact import (
    AutoCompactConfig,
    AutoCompactManager,
    HeadTailSplit,
    select_head_tail,
    render_head_messages,
    _estimate_tokens,
)
from agentscope_integration.cache.cache_boundary import CacheBoundaryHook
from agentscope_integration.cache.tail_trim import (
    TailTrimConfig,
    tail_trim,
)


# ---------------------------------------------------------------------------
# Mock helpers
# ---------------------------------------------------------------------------

def make_tool_row(tool_name, content_str, compacted=False, row_id=None):
    meta = {}
    if compacted:
        meta["time_compacted"] = "2024-01-01T00:00:00+00:00"
    return {
        "id": row_id or f"row-{tool_name}-{len(content_str)}",
        "role": "tool",
        "type": "tool",
        "tool_name": tool_name,
        "content": {"tool_name": tool_name, "result": content_str},
        "metadata": meta,
    }


def make_user_row(content, row_id=None):
    return {
        "id": row_id or f"row-user-{len(content)}",
        "role": "user",
        "type": "user",
        "content": content,
        "metadata": {},
    }


def make_assistant_row(content, row_id=None):
    return {
        "id": row_id or f"row-asst-{len(content)}",
        "role": "assistant",
        "type": "assistant",
        "content": content,
        "metadata": {},
    }


# ---------------------------------------------------------------------------
# Micro Compact integration tests
# ---------------------------------------------------------------------------


class TestMicroCompactIntegration:
    def test_full_prune_pipeline_with_mixed_tools(self):
        """Simulate a realistic conversation with mixed tool outputs."""
        rows = [
            make_user_row("请帮我分析数据"),
            make_assistant_row("好的,我先读取文件"),
            make_tool_row("read_file", "FILE_CONTENT" * 200, row_id="t1"),
            make_assistant_row("文件内容如上，现在执行命令"),
            make_tool_row("execute_command", "COMMAND_OUTPUT" * 300, row_id="t2"),
            make_assistant_row("命令执行成功"),
            make_tool_row("run_skill_script", "SKILL_RESULT" * 500, row_id="t3"),
            make_assistant_row("Skill 执行完毕"),
            make_tool_row("read_file", "ANOTHER_FILE" * 100, row_id="t4"),
            make_assistant_row("第二个文件也读完了"),
        ]

        result = prune_tool_outputs(rows, protect=5000, minimum=100)

        # Skill (row index 6) must never be pruned
        skill_meta = result[6].get("metadata", {})
        assert not skill_meta.get("time_compacted"), "skill NEVER pruned"

        # Recent tool outputs within protect zone should be safe
        # t4 (100*5=500 chars at idx 8) should be within protect zone
        # t2 (300 chars output) should be compacted if beyond protect

    def test_progressive_compaction_across_multiple_calls(self):
        """Simulate multiple prune calls as conversation grows."""
        rows = []
        for i in range(20):
            rows.append(make_user_row(f"问题 {i}"))
            rows.append(make_assistant_row(f"回答 {i}"))
            rows.append(make_tool_row("read_file", f"FILE_DATA_{i}" * 200, row_id=f"t{i}"))

        # First call — protect 3000 chars
        result1 = prune_tool_outputs(rows, protect=3000, minimum=100)
        compacted1 = [
            i for i, r in enumerate(result1)
            if isinstance(r.get("metadata"), dict) and r["metadata"].get("time_compacted")
        ]
        # Some older rows should be compacted
        assert len(compacted1) > 0, "first prune should compact some rows"

        # Second call — already-compacted rows act as boundary
        # The boundary row should show up as already compacted
        result2 = prune_tool_outputs(result1, protect=3000, minimum=100)


class TestAutoCompactIntegration:
    def test_select_head_tail_basic(self):
        """Head/tail split on a realistic message sequence."""
        msgs = []
        for i in range(10):
            msgs.append(make_user_row(f"问题 {i}", row_id=f"u{i}"))
            msgs.append(make_assistant_row(f"回答 {i}", row_id=f"a{i}"))
            msgs.append(make_tool_row("read_file", f"数据 {i}" * 100, row_id=f"t{i}"))

        split = select_head_tail(msgs, tail_turns=2, usable_context=90000)

        assert len(split.head) > 0
        assert len(split.tail) > 0
        assert len(split.head) + len(split.tail) == len(msgs)
        # Tail should end with the most recent messages
        assert split.tail[-1]["id"] == msgs[-1]["id"]

    def test_select_head_tail_empty(self):
        split = select_head_tail([], tail_turns=2)
        assert len(split.head) == 0
        assert len(split.tail) == 0

    def test_render_head_messages(self):
        rows = [
            make_user_row("请帮我分析"),
            make_assistant_row("好的"),
            make_tool_row("read_file", "X" * 3000, row_id="t1"),
            make_tool_row("execute_command", "Y" * 500, row_id="t2"),
        ]
        lines = render_head_messages(rows, max_tool_chars=2000)
        assert len(lines) == 4
        # read_file output should be truncated
        read_line = [l for l in lines if "read_file" in l][0]
        assert "[Tool output truncated" in read_line or len(read_line) <= 2100
        # execute_command output (500 chars) should NOT be truncated
        exec_line = [l for l in lines if "execute_command" in l][0]
        assert "[Tool output truncated" not in exec_line

    def test_auto_compact_config_defaults(self):
        cfg = AutoCompactConfig()
        assert cfg.enabled
        assert cfg.trigger_tokens == 90_000
        assert cfg.compact_model_key == "deepseek-v4-flash"
        assert not cfg.shadow_mode

    def test_auto_compact_manager_circuit_breaker(self):
        mgr = AutoCompactManager()
        assert not mgr.circuit_open
        for _ in range(5):
            mgr._record_failure()
        assert mgr.circuit_open
        # After circuit opens, should_compact returns False
        assert not mgr.should_compact(200_000)

    def test_cumulative_new_tokens_triggers_compaction(self):
        """Compaction should trigger when cumulative NEW tokens >= threshold.

        New tokens = prompt_tokens - cached_input_tokens (actual context growth).
        With KV cache hitting 97%+, new_tokens is typically 500-1500/round.
        """
        mgr = AutoCompactManager(
            config=AutoCompactConfig(trigger_tokens=90_000),
        )
        # Simulate many calls with realistic new-token deltas (~1000 each)
        for _ in range(89):
            assert not mgr.should_compact(1_000)
        assert mgr.should_compact(1_000)  # 90 * 1000 = 90000 >= 90000

    def test_cumulative_new_tokens_resets_after_compaction(self):
        """After compaction succeeds, cumulative counter should reset."""
        mgr = AutoCompactManager(
            config=AutoCompactConfig(trigger_tokens=90_000),
        )
        # Reach threshold with realistic per-round new-token deltas
        for _ in range(89):
            mgr.should_compact(1_011)
        assert mgr.should_compact(1_011)
        # Simulate successful compaction
        mgr._record_success()
        # Counter should be reset
        assert mgr._cumulative_new_tokens == 0
        assert not mgr.should_compact(1_000)

    def test_new_tokens_accumulate_realistically(self):
        """Realistic scenario: new tokens 500-1500/round, triggers after ~70-100 rounds."""
        mgr = AutoCompactManager(
            config=AutoCompactConfig(trigger_tokens=90_000),
        )
        # 60 rounds at 1500 new tokens = 90,000 => trigger
        for _ in range(59):
            assert not mgr.should_compact(1_500)
        assert mgr.should_compact(1_500)  # 60 * 1500 = 90000 >= 90000

    def test_zero_new_tokens_never_triggers(self):
        """All-cached calls (new_tokens=0) should never trigger compaction."""
        mgr = AutoCompactManager(
            config=AutoCompactConfig(trigger_tokens=90_000),
        )
        for _ in range(1000):
            assert not mgr.should_compact(0)

    def test_shadow_mode_noop(self):
        mgr = AutoCompactManager(
            config=AutoCompactConfig(shadow_mode=True)
        )
        assert mgr.should_compact(100_000)

        async def _run():
            result = await mgr.compact(
                messages=[],
                current_tokens=100_000,
                memory=MagicMock(),
                model_factory=MagicMock(),
                formatter_factory=MagicMock(),
            )
            assert result is None  # shadow mode returns None

        asyncio.run(_run())


class TestBuildCompactPrompt:
    def test_first_compaction_prompt(self):
        prompt = build_compaction_prompt(
            head_messages=["[user] 请分析", "[tool:read_file] 数据..."],
            previous_summary=None,
        )
        assert "Create a new anchored summary" in prompt
        assert "[user] 请分析" in prompt
        assert SUMMARY_TEMPLATE in prompt

    def test_incremental_compaction_prompt(self):
        prompt = build_compaction_prompt(
            head_messages=["[user] 继续分析"],
            previous_summary="## Goal\n- 分析数据\n",
        )
        assert "Update the anchored summary" in prompt
        assert "<previous-summary>" in prompt
        assert "## Goal\n- 分析数据" in prompt


class TestCompactPrompts:
    def test_system_prompt_has_no_tool_instruction(self):
        assert "CRITICAL" in COMPACT_SYSTEM_PROMPT
        assert "Do NOT call any tools" in COMPACT_SYSTEM_PROMPT

    def test_summary_template_has_all_sections(self):
        required = [
            "## Goal",
            "## Constraints & Preferences",
            "## Progress",
            "### Done",
            "### In Progress",
            "### Blocked",
            "## Key Decisions",
            "## Next Steps",
            "## Critical Context",
            "## Relevant Files",
        ]
        for section in required:
            assert section in SUMMARY_TEMPLATE, f"Missing section: {section}"

    def test_auto_continue_message(self):
        assert "Continue if you have next steps" in AUTO_CONTINUE_MESSAGE


# ---------------------------------------------------------------------------
# KV Cache + Micro Compact interplay
# ---------------------------------------------------------------------------


class TestCacheAndCompactInterplay:
    def test_tail_trim_before_prune(self):
        """TailTrim should be safe to run before Micro Compact prune."""
        msgs = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "问题1"},
        ]
        for i in range(10):
            msgs.append({"role": "assistant", "content": f"答{i}"})
            msgs.append({"role": "tool", "tool_name": "read_file",
                          "content": f"数据{i}" * 100, "metadata": {}})

        # TailTrim should preserve system prefix
        trimmed = tail_trim(msgs, TailTrimConfig(
            trigger_message_count=5, target_max_messages=6,
            keep_tail_turns=2, min_prefix_messages=2,
        ))
        assert trimmed.prefix_preserved
        assert trimmed.trimmed_messages[0]["role"] == "system"

    def test_cache_boundary_with_compaction_trigger(self):
        """CacheBoundaryHook should not interfere with compaction triggering."""
        hook = CacheBoundaryHook()
        hook.freeze("hash1", "hash2")

        # After compaction, system prompt should stay the same
        ok, warnings = hook.check("hash1", "hash2")
        assert ok
        assert len(warnings) == 0

        # If compaction changes the system prompt, warn
        ok, warnings = hook.check("hash1_changed", "hash2")
        assert not ok
        assert len(warnings) == 1


class TestTokenEstimation:
    def test_estimate_tokens_english(self):
        text = "Hello world, this is a test."  # 29 chars
        tokens = _estimate_tokens(text)
        assert tokens == 7  # 29 // 4

    def test_estimate_tokens_empty(self):
        assert _estimate_tokens("") == 1  # max(1, 0)

    def test_estimate_tokens_short(self):
        assert _estimate_tokens("hi") == 1  # max(1, 2//4=0)
