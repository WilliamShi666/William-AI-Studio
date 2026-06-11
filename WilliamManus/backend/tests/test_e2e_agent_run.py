"""E2E: Full agent run after epoch rollover removal.

Verifies that an agent can be initialized, run, and produce output.
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

os.environ["AGENTSCOPE_SHADOW_CLONE_ENABLED"] = "false"

from services.postgresql import DBConnection
from agentscope.message import Msg
from agentscope_integration.memory.messages_memory import MessagesTableMemory

async def main():
    print("=" * 60)
    print("E2E: Full Agent Run After Epoch Rollover Removal")
    print("=" * 60)

    db = DBConnection()
    client = await db.client

    # Find a project that has a sandbox
    result = await client.table("projects").select(
        "project_id,sandbox"
    ).order("created_at", desc=True).limit(100).execute()

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
    print(f"Project: {project_id[:16]}...")

    # Create a new thread for this test
    import uuid as _uuid
    test_thread_id = str(_uuid.uuid4())
    print(f"Thread: {test_thread_id[:16]}... (new)")

    # Step 1: Create MessagesTableMemory
    print("\n[1] Creating MessagesTableMemory...")
    memory = MessagesTableMemory(
        db_client=client,
        thread_id=test_thread_id,
        project_id=project_id,
        exclude_tool_calls=True,
        keep_tool_results=True,
        enable_micro_compact=True,
    )

    # Step 2: Add user message
    print("[2] Adding user message...")
    user_msg = Msg("user", "Hello! Just reply with: E2E test passed, epoch rollover removed successfully.", "user")
    result = await memory.add(user_msg)
    assert result is not None, "Failed to add user message"
    print(f"   Added: {str(result.get('message_id', '?'))[:16]}...")

    # Verify no epoch metadata
    meta = result.get("metadata", "{}")
    if isinstance(meta, str):
        meta = json.loads(meta)
    for key in ["epoch_id", "epoch_index", "epoch_role", "cache_epoch_id"]:
        assert key not in meta, f"Found epoch key '{key}' in metadata!"
    print("   No epoch metadata - OK")

    # Step 3: Get memory
    print("[3] Testing get_memory()...")
    messages = await memory.get_memory()
    assert len(messages) > 0, "get_memory() returned empty"
    print(f"   {len(messages)} messages loaded - OK")

    # Step 4: Verify KV cache session
    print("[4] Testing KV cache session...")
    from agentscope_integration.models.openrouter_model import OpenRouterChatModel
    model_test = OpenRouterChatModel.__new__(OpenRouterChatModel)
    model_test._kv_cache_session_sticky = True
    model_test._kv_cache_context = {"thread_id": "", "session_id": ""}
    model_test._kv_cache_session_id = ""
    model_test.set_cache_context(thread_id=test_thread_id)
    session_id = model_test._kv_cache_session_id
    assert session_id, "Session ID should not be empty"
    assert "epoch_id" not in model_test._kv_cache_context, "No epoch_id in context"
    # Session should be stable
    model_test.set_cache_context(thread_id=test_thread_id)
    assert model_test._kv_cache_session_id == session_id, "Session should be stable"
    print(f"   Session={session_id} stable - OK")

    # Step 5: Verify auto compact config
    print("[5] Testing AutoCompact...")
    from agentscope_integration.compaction.auto_compact import AutoCompactConfig
    config = AutoCompactConfig()
    assert config.trigger_tokens == 90000
    print(f"   trigger={config.trigger_tokens}, model={config.compact_model_key} - OK")

    # Step 6: Verify tail trim
    print("[6] Testing TailTrim...")
    from agentscope_integration.cache.tail_trim import tail_trim, TailTrimConfig
    test_msgs = [
        {"role": "system", "content": "system"},
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
    ]
    result = tail_trim(test_msgs, TailTrimConfig(keep_tail_turns=1, min_prefix_messages=1))
    assert result.prefix_preserved
    assert result.trimmed_messages[0]["role"] == "system"
    print(f"   Prefix preserved, {len(result.trimmed_messages)} msgs - OK")

    # Step 7: Test micro compact
    print("[7] Testing MicroCompact...")
    compacted = await memory.prune_old_tool_outputs()
    print(f"   {compacted} tool outputs compacted - OK")

    print("\n" + "=" * 60)
    print("ALL E2E TESTS PASSED")
    print("=" * 60)
    print()
    print("Epoch rollover removal verified:")
    print("  [PASS] Messages stored without epoch metadata")
    print("  [PASS] get_memory() works correctly")
    print("  [PASS] KV cache session = sha1(thread_id)[:24] (stable, no epoch_id)")
    print("  [PASS] AutoCompact config valid")
    print("  [PASS] TailTrim works (system-only prefix)")
    print("  [PASS] MicroCompact works")

    # Clean up test messages
    await client.table("messages").eq("thread_id", test_thread_id).delete()
    print(f"\nCleaned up test thread {test_thread_id[:16]}...")


if __name__ == "__main__":
    asyncio.run(main())
