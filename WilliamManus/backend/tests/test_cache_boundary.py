"""Unit tests for CacheBoundaryHook."""

import pytest
from agentscope_integration.cache.cache_boundary import CacheBoundaryHook


class TestCacheBoundaryHook:
    def test_freeze_and_check_ok(self):
        hook = CacheBoundaryHook()
        assert not hook._frozen
        hook.freeze("abc123", "def456")
        assert hook._frozen
        ok, warnings = hook.check("abc123", "def456")
        assert ok
        assert len(warnings) == 0

    def test_system_drift_detected(self):
        hook = CacheBoundaryHook()
        hook.freeze("abc123", "def456")
        ok, warnings = hook.check("xyz789", "def456")
        assert not ok
        assert len(warnings) == 1
        assert "SYSTEM_DRIFT" in warnings[0]

    def test_tool_drift_detected(self):
        hook = CacheBoundaryHook()
        hook.freeze("abc123", "def456")
        ok, warnings = hook.check("abc123", "xyz789")
        assert not ok
        assert len(warnings) == 1
        assert "TOOL_DRIFT" in warnings[0]

    def test_both_drift(self):
        hook = CacheBoundaryHook()
        hook.freeze("abc123", "def456")
        ok, warnings = hook.check("CHANGED", "CHANGED")
        assert not ok
        assert len(warnings) == 2

    def test_before_freeze_always_ok(self):
        hook = CacheBoundaryHook()
        ok, warnings = hook.check("anything", "whatever")
        assert ok
        assert len(warnings) == 0

    def test_drift_count_increments(self):
        hook = CacheBoundaryHook()
        hook.freeze("a", "b")
        assert hook._drift_count == 0
        hook.check("c", "d")
        assert hook._drift_count == 2
        hook.check("e", "f")
        assert hook._drift_count == 4

    def test_hash_system_messages(self):
        msgs = [
            {"role": "system", "content": "You are a helpful assistant."},
            {"role": "user", "content": "Hello"},
        ]
        h1 = CacheBoundaryHook.hash_system_messages(msgs)
        h2 = CacheBoundaryHook.hash_system_messages(msgs)
        assert h1 == h2
        assert len(h1) == 16  # SHA256 truncated to 16 hex chars

    def test_hash_system_empty(self):
        assert CacheBoundaryHook.hash_system_messages([]) == ""
        assert CacheBoundaryHook.hash_system_messages(
            [{"role": "user", "content": "hi"}]
        ) == ""

    def test_hash_tools(self):
        tools = [
            {"function": {"name": "read_file", "description": "Read a file"}},
            {"function": {"name": "execute_command", "description": "Run command"}},
        ]
        h1 = CacheBoundaryHook.hash_tools(tools)
        h2 = CacheBoundaryHook.hash_tools(tools)
        assert h1 == h2
        assert len(h1) == 16

    def test_hash_tools_empty(self):
        assert CacheBoundaryHook.hash_tools(None) == ""
        assert CacheBoundaryHook.hash_tools([]) == ""

    def test_canonical_json_stable(self):
        obj = {"c": 3, "a": 1, "b": 2}
        j1 = CacheBoundaryHook.canonical_json_dumps(obj)
        j2 = CacheBoundaryHook.canonical_json_dumps({"b": 2, "a": 1, "c": 3})
        assert j1 == j2
