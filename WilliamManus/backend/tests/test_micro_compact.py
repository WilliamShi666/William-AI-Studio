"""Unit tests for Micro Compact module."""

import pytest
from datetime import datetime, timezone
from agentscope_integration.memory.micro_compact import (
    PRUNE_MINIMUM,
    PRUNE_PROTECT,
    PRUNE_PLACEHOLDER,
    COMPACTABLE_TOOL_KINDS,
    PROTECTED_TOOL_NAMES,
    _tool_kind_from_name,
    _is_compactable,
    make_micro_compact_hook,
    prune_tool_outputs,
)


class TestToolClassification:
    def test_code_tools_compactable(self):
        for name in ("execute_command", "read_file", "write_file", "edit_file",
                     "list_dir", "make_dir", "upload_file", "download_file"):
            assert _is_compactable(name), f"{name} should be compactable"

    def test_skill_tools_protected(self):
        for name in ("run_skill_script", "get_available_skills", "load_skill",
                     "load_reference", "list_skill_scripts"):
            assert not _is_compactable(name), f"{name} should NOT be compactable"

    def test_delegation_and_ltm_protected(self):
        for name in ("delegate_to_worker", "retrieve_from_memory",
                     "send_peer_note", "read_full_result"):
            assert not _is_compactable(name), f"{name} should NOT be compactable"

    def test_task_list_protected(self):
        for name in ("create_tasks", "view_tasks", "update_tasks", "delete_tasks"):
            assert not _is_compactable(name), f"{name} should NOT be compactable"

    def test_unknown_tool_not_compactable(self):
        assert not _is_compactable("some_mystery_tool")
        assert not _is_compactable("")

    def test_compactable_kinds_cover_all_backends(self):
        assert "code" in COMPACTABLE_TOOL_KINDS
        assert "web_search" in COMPACTABLE_TOOL_KINDS
        assert "media" in COMPACTABLE_TOOL_KINDS
        assert "computer_use" in COMPACTABLE_TOOL_KINDS

    def test_protected_set_includes_all_skills(self):
        assert "run_skill_script" in PROTECTED_TOOL_NAMES
        assert "get_available_skills" in PROTECTED_TOOL_NAMES


class TestPruneToolOutputs:
    def test_empty_input(self):
        result = prune_tool_outputs([])
        assert result == []

    def test_non_tool_rows_ignored(self):
        rows = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
        ]
        result = prune_tool_outputs(rows, protect=100, minimum=10)
        for r in result:
            meta = r.get("metadata", {})
            assert not meta.get("time_compacted")

    def test_skill_never_pruned(self):
        rows = [
            {"role": "tool", "tool_name": "run_skill_script",
             "content": "CRITICAL SKILL OUTPUT" * 100, "metadata": {}},
            {"role": "tool", "tool_name": "read_file",
             "content": "file content" * 100, "metadata": {}},
        ]
        result = prune_tool_outputs(rows, protect=10, minimum=10)
        skill_meta = result[0].get("metadata", {})
        assert not skill_meta.get("time_compacted"), "skill must never be pruned"

    def test_protection_zone_respected(self):
        rows = [
            {"role": "tool", "tool_name": "read_file",
             "content": "x" * 500, "metadata": {}},   # old
            {"role": "tool", "tool_name": "execute_command",
             "content": "y" * 500, "metadata": {}},   # old
            {"role": "tool", "tool_name": "read_file",
             "content": "z" * 2000, "metadata": {}},  # fills protect zone
            {"role": "tool", "tool_name": "execute_command",
             "content": "w" * 3000, "metadata": {}},  # newest, protected
        ]
        result = prune_tool_outputs(rows, protect=3000, minimum=100)
        # Row 3 (newest, 3000 chars) fills protect zone → protected
        meta3 = result[3].get("metadata", {})
        assert not meta3.get("time_compacted"), "newest should be protected"

        # Rows 0,1 (oldest) should be pruned
        meta0 = result[0].get("metadata", {})
        assert meta0.get("time_compacted"), "oldest should be pruned"

    def test_minimum_threshold_respected(self):
        rows = [
            {"role": "tool", "tool_name": "read_file",
             "content": "x" * 50, "metadata": {}},
        ]
        result = prune_tool_outputs(rows, protect=0, minimum=1000)
        meta = result[0].get("metadata", {})
        assert not meta.get("time_compacted"), (
            "should not prune when savings < minimum"
        )

    def test_already_compacted_is_boundary(self):
        rows = [
            {"role": "tool", "tool_name": "read_file",
             "content": "x" * 500, "metadata": {}},
            {"role": "tool", "tool_name": "read_file",
             "content": "y" * 500,
             "metadata": {"time_compacted": "2024-01-01T00:00:00+00:00"}},
            {"role": "tool", "tool_name": "read_file",
             "content": "z" * 2000, "metadata": {}},
        ]
        result = prune_tool_outputs(rows, protect=100, minimum=10)
        # Walk backwards: row 2 (2000 chars) → row 1 (HAS time_compacted → BREAK)
        # Row 0 is never reached because the algorithm stops at the boundary.
        # Row 2 is within protect zone (2000 > 100, but protect accumulates),
        # so it's protected. Row 0 should NOT be compacted (never reached).
        meta0 = result[0].get("metadata", {})
        assert not meta0.get("time_compacted"), (
            "row before boundary should NOT be reached by backward walk"
        )
        # Row 1 boundary is intact
        meta1 = result[1].get("metadata", {})
        assert meta1.get("time_compacted") is not None
        # Row 2 is protected (within protect zone from backward walk start)
        meta2 = result[2].get("metadata", {})
        assert not meta2.get("time_compacted"), "newest row within protect zone"


class TestHookFactory:
    def test_disabled_hook_is_noop(self):
        hook = make_micro_compact_hook(enabled=False)

        class FakeAgent:
            memory = None

        async def _run():
            result = await hook(FakeAgent(), {})
            assert result is None

        import asyncio
        asyncio.run(_run())

    def test_enabled_hook_no_memory(self):
        hook = make_micro_compact_hook(enabled=True)

        class FakeAgent:
            memory = None

        async def _run():
            result = await hook(FakeAgent(), {})
            assert result is None

        import asyncio
        asyncio.run(_run())


class TestPruneReasoningContent:
    def test_strips_reasoning_from_non_tool_call_assistant(self):
        """Assistant rows without tool_calls should have reasoning_content removed."""
        from agentscope_integration.memory.micro_compact import prune_reasoning_content

        rows = [
            {"role": "user", "content": "hello", "metadata": {}},
            {"role": "assistant", "content": "Hi there!", "metadata": {},
             "reasoning_content": "Simple greeting, no tools needed."},
            {"role": "user", "content": "what's the weather?", "metadata": {}},
        ]
        result = prune_reasoning_content(rows)
        assert "reasoning_content" not in result[1], (
            "non-tool-call assistant reasoning_content must be stripped"
        )

    def test_preserves_reasoning_for_tool_call_assistant(self):
        """Assistant rows WITH tool_calls MUST keep reasoning_content."""
        from agentscope_integration.memory.micro_compact import prune_reasoning_content

        rows = [
            {"role": "user", "content": "calculate 2+2", "metadata": {}},
            {"role": "assistant", "content": "",
             "tool_calls": [{"id": "c1", "name": "execute_command"}],
             "reasoning_content": "Need to calculate using a tool.",
             "metadata": {}},
        ]
        result = prune_reasoning_content(rows)
        assert result[1].get("reasoning_content") == "Need to calculate using a tool.", (
            "tool-call assistant reasoning_content must be preserved"
        )

    def test_handles_mixed_tool_and_non_tool_messages(self):
        """Full conversation: some assistants have tool_calls, some don't."""
        from agentscope_integration.memory.micro_compact import prune_reasoning_content

        rows = [
            {"role": "user", "content": "question", "metadata": {}},
            {"role": "assistant", "content": "",
             "tool_calls": [{"id": "c1", "name": "get_date"}],
             "reasoning_content": "Need to get today's date first.",
             "metadata": {}},
            {"role": "tool", "content": "2026-05-14", "metadata": {}},
            {"role": "assistant", "content": "Today is 2026-05-14.",
             "reasoning_content": "Now I have the date, wrap up the answer.",
             "metadata": {}},
        ]
        result = prune_reasoning_content(rows)
        # Row 1: has tool_calls → keep reasoning
        assert result[1].get("reasoning_content") == "Need to get today's date first."
        # Row 3: NO tool_calls → strip reasoning
        assert "reasoning_content" not in result[3]

    def test_empty_and_non_assistant_rows_unchanged(self):
        """Non-assistant rows and empty lists should be safe."""
        from agentscope_integration.memory.micro_compact import prune_reasoning_content

        assert prune_reasoning_content([]) == []
        rows = [
            {"role": "user", "content": "hello", "metadata": {}},
            {"role": "tool", "content": "result", "metadata": {}},
        ]
        result = prune_reasoning_content(rows)
        assert result == rows

    def test_no_reasoning_content_present(self):
        """Rows without reasoning_content should be unchanged."""
        from agentscope_integration.memory.micro_compact import prune_reasoning_content

        rows = [
            {"role": "assistant", "content": "Hello!", "metadata": {}},
        ]
        result = prune_reasoning_content(rows)
        assert result[0] == rows[0]


class TestConstants:
    def test_placeholder_is_string(self):
        assert isinstance(PRUNE_PLACEHOLDER, str)
        assert len(PRUNE_PLACEHOLDER) > 0

    def test_protect_positive(self):
        assert PRUNE_PROTECT > 0

    def test_minimum_positive(self):
        assert PRUNE_MINIMUM > 0
