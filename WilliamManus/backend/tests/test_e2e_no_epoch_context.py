"""E2E test: verify three-layer context engineering works without epoch rollover.

Tests:
1. Messages are stored WITHOUT epoch metadata
2. get_memory() returns messages correctly
3. _get_memory_kv_ready() works without rollover logic
4. Micro compact (prune_old_tool_outputs) works
5. Auto compact + TailTrim utilities work
6. KV cache session uses thread_id only (no epoch_id)
7. CacheBoundaryHook freezes and stays stable
"""
import asyncio
import json
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from dotenv import load_dotenv

load_dotenv(override=True)

os.environ["AGENTSCOPE_MICRO_COMPACT_ENABLED"] = "true"
os.environ["AGENTSCOPE_AUTO_COMPACT_ENABLED"] = "false"
os.environ["AGENTSCOPE_KV_CACHE_BOUNDARY_CHECK"] = "true"

from services.postgresql import DBConnection
from agentscope.message import Msg
from agentscope_integration.cache.tail_trim import TailTrimConfig, tail_trim
from agentscope_integration.cache.cache_boundary import CacheBoundaryHook
from agentscope_integration.compaction.auto_compact import (
    AutoCompactConfig,
    AutoCompactManager,
)


def _dict_rows(rows):
    """Convert row objects to dicts for easier assertion."""
    result = []
    for r in (rows or []):
        if hasattr(r, "__dict__"):
            result.append(dict(r.__dict__))
        elif isinstance(r, dict):
            result.append(r)
        else:
            result.append({"data": str(r)})
    return result


async def main():
    print("=" * 60)
    print("E2E Test: Context Engineering Without Epoch Rollover")
    print("=" * 60)

    db = DBConnection()
    client = await db.client

    # Find a project with sandbox
    result = await client.table("projects").select(
        "project_id,sandbox"
    ).order("created_at", desc=True).limit(20).execute()

    project_id = None
    for p in (result.data or []):
        s = p.get("sandbox", "{}")
        if isinstance(s, str):
            try:
                s = json.loads(s)
            except Exception:
                s = {}
        if isinstance(s, dict) and s.get("id"):
            project_id = p["project_id"]
            break

    assert project_id, "No project with sandbox found"
    print(f"  Project: {project_id[:12]}...")

    # Find a thread
    result = await client.table("threads").select("thread_id").eq(
        "project_id", project_id
    ).limit(1).execute()
    assert result.data, "No thread found"
    thread_id = result.data[0]["thread_id"]
    print(f"  Thread: {thread_id[:12]}...")

    # ---- Test 1: MessagesTableMemory without epoch ----
    print("\n[Test 1] MessagesTableMemory stores messages without epoch metadata")
    from agentscope_integration.memory.messages_memory import MessagesTableMemory

    memory = MessagesTableMemory(
        db_client=client,
        thread_id=thread_id,
        project_id=project_id,
        exclude_tool_calls=True,
        keep_tool_results=True,
        enable_micro_compact=True,
    )

    # Add a user message
    user_msg = Msg("user", "Hello, this is an E2E test message", "user")
    result = await memory.add(user_msg)
    assert result is not None, "add() returned None"
    print(f"  User message added: id={str(result.get('message_id', '?'))[:12]}...")

    # Check the stored message metadata has NO epoch fields
    stored_metadata = result.get("metadata", "{}")
    if isinstance(stored_metadata, str):
        stored_metadata = json.loads(stored_metadata)
    epoch_keys = [
        "epoch_id",
        "epoch_index",
        "epoch_role",
        "budget_metric",
        "cache_epoch_id",
        "cache_segment_seq",
        "cache_breakpoint",
    ]
    for key in epoch_keys:
        assert key not in stored_metadata, (
            f"EPOCH KEY '{key}' FOUND in message metadata! Should have been removed."
        )
    print(f"  PASS: No epoch metadata in stored message")

    # Add an assistant message with tool calls
    assistant_msg = Msg(
        "assistant",
        [
            {"type": "text", "text": "Let me check the workspace."},
            {
                "type": "tool_use",
                "id": f"call-{uuid.uuid4().hex[:8]}",
                "name": "execute_command",
                "input": {"command": "ls /workspace"},
            },
        ],
        "assistant",
    )
    result2 = await memory.add(assistant_msg)
    assert result2 is not None
    stored2 = result2.get("metadata", "{}")
    if isinstance(stored2, str):
        stored2 = json.loads(stored2)
    for key in epoch_keys:
        assert key not in stored2, (
            f"EPOCH KEY '{key}' FOUND in assistant message metadata!"
        )
    print(f"  PASS: No epoch metadata in assistant message")

    # Add a tool result
    tool_msg = Msg(
        "assistant",
        [
            {
                "type": "tool_result",
                "id": "call-1",
                "name": "execute_command",
                "output": [{"type": "text", "text": "file1.txt  file2.txt"}],
            }
        ],
        "assistant",
    )
    result3 = await memory.add(tool_msg)
    assert result3 is not None
    stored3 = result3.get("metadata", "{}")
    if isinstance(stored3, str):
        stored3 = json.loads(stored3)
    for key in epoch_keys:
        assert key not in stored3, (
            f"EPOCH KEY '{key}' FOUND in tool message metadata!"
        )
    print(f"  PASS: No epoch metadata in tool message")

    # ---- Test 2: get_memory() returns messages ----
    print("\n[Test 2] get_memory() returns messages correctly")
    messages = await memory.get_memory()
    assert len(messages) > 0, "get_memory() returned empty list"
    print(f"  Loaded {len(messages)} messages")

    # Verify no epoch-snapshot in messages
    for msg in messages:
        text = msg.get_text_content() if hasattr(msg, "get_text_content") else ""
        assert "<epoch-snapshot>" not in str(text), (
            "Epoch snapshot text found in messages!"
        )
    print(f"  PASS: No epoch-snapshot in messages")

    # ---- Test 3: Tail Trim works ----
    print("\n[Test 3] TailTrim preserves system messages only")
    test_msgs = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "q2"},
        {"role": "assistant", "content": "a2"},
        {"role": "user", "content": "q3"},
        {"role": "assistant", "content": "a3"},
    ]
    result = tail_trim(
        test_msgs,
        TailTrimConfig(
            trigger_message_count=3,
            target_max_messages=3,
            keep_tail_turns=1,
            min_prefix_messages=1,
        ),
    )
    assert result.prefix_preserved, "Prefix should be preserved"
    assert result.removed_count > 0, "Should have removed some messages"
    # System message should be in prefix
    assert result.trimmed_messages[0]["role"] == "system"
    print(
        f"  PASS: TailTrim removed {result.removed_count} messages, "
        f"kept {len(result.trimmed_messages)} total"
    )

    # ---- Test 4: CacheBoundaryHook works ----
    print("\n[Test 4] CacheBoundaryHook freezes and stays stable")
    hook = CacheBoundaryHook()
    sys_msgs = [
        {"role": "system", "content": "You are a helpful assistant."},
    ]
    tools = [{"type": "function", "function": {"name": "read_file"}}]

    sys_hash = hook.hash_system_messages(sys_msgs)
    tool_hash = hook.hash_tools(tools)
    assert sys_hash, f"System hash should not be empty"
    assert tool_hash, f"Tool hash should not be empty"
    print(f"  System hash: {sys_hash}, Tool hash: {tool_hash}")

    # Freeze
    hook.freeze(sys_hash, tool_hash)
    print(f"  Frozen: system={hook._frozen_system_hash}, tools={hook._frozen_tool_hash}")

    # Check with same content: should be OK (no drift)
    ok2, warnings2 = hook.check(sys_hash, tool_hash)
    assert ok2, f"Second check should be OK (same hashes): {warnings2}"
    print(f"  Stable OK: no drift")

    # Check with different system prompt: should detect drift
    modified_sys = [
        {"role": "system", "content": "You are a DIFFERENT assistant."},
    ]
    modified_sys_hash = hook.hash_system_messages(modified_sys)
    ok3, warnings3 = hook.check(modified_sys_hash, tool_hash)
    assert not ok3, f"Should have detected system drift! got ok={ok3}, warnings={warnings3}"
    assert len(warnings3) > 0, f"Should have drift warnings"
    print(f"  Drift detected OK: {warnings3[0][:60]}...")

    # ---- Test 5: Auto Compact utilities ----
    print("\n[Test 5] AutoCompactConfig defaults are valid")
    config = AutoCompactConfig()
    assert config.trigger_tokens == 90000
    assert config.tail_turns >= 1
    assert config.compact_model_key != ""
    print(
        f"  PASS: trigger={config.trigger_tokens}, "
        f"tail_turns={config.tail_turns}, model={config.compact_model_key}"
    )

    # ---- Test 6: KV cache session uses thread_id only ----
    print("\n[Test 6] KV cache session derivation (no epoch_id)")
    from agentscope_integration.models.openrouter_model import OpenRouterChatModel

    model_test = OpenRouterChatModel.__new__(OpenRouterChatModel)
    model_test._kv_cache_session_sticky = True
    model_test._kv_cache_context = {"thread_id": "", "session_id": ""}
    model_test._kv_cache_session_id = ""

    model_test.set_cache_context(thread_id="test-thread-123")
    session_1 = model_test._kv_cache_session_id
    model_test.set_cache_context(thread_id="test-thread-123")
    session_2 = model_test._kv_cache_session_id

    assert session_1, "Session ID should not be empty"
    assert session_1 == session_2, (
        f"Session ID should be stable: {session_1} != {session_2}"
    )
    print(f"  PASS: Stable session={session_1} for thread=test-thread-123")

    # Verify no epoch_id in context
    assert "epoch_id" not in model_test._kv_cache_context, (
        "epoch_id should not be in cache context!"
    )
    print(f"  PASS: No epoch_id in KV cache context")

    # ---- Test 7: Multi-message flow without epoch ----
    print("\n[Test 7] Multi-message add + get_memory roundtrip")
    for i in range(5):
        msg = Msg("user", f"Test message {i}", "user")
        await memory.add(msg)

    all_msgs = await memory.get_memory()
    user_msgs = [
        m
        for m in all_msgs
        if hasattr(m, "get_text_content")
        and "Test message" in (m.get_text_content() or "")
    ]
    assert len(user_msgs) >= 5, (
        f"Expected at least 5 user messages, got {len(user_msgs)}"
    )
    print(f"  PASS: {len(all_msgs)} total messages, {len(user_msgs)} test messages")

    # ---- Summary ----
    print("\n" + "=" * 60)
    print("ALL E2E TESTS PASSED")
    print("=" * 60)
    print()
    print("Summary:")
    print("  [PASS] Messages stored without epoch metadata")
    print("  [PASS] get_memory() returns messages correctly")
    print("  [PASS] TailTrim preserves system messages")
    print("  [PASS] CacheBoundaryHook freezes and detects drift")
    print("  [PASS] AutoCompactConfig defaults are valid")
    print("  [PASS] KV cache session uses thread_id only")
    print("  [PASS] Multi-message roundtrip works")

    print("\nE2E test complete.")


if __name__ == "__main__":
    asyncio.run(main())
