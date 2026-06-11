"""
Multi-turn Memory Persistence Test for AgentScope Integration.

This script tests that conversation context persists across multiple
AgentScopeRunner instances (simulating multiple user turns).

The key test case:
- Turn 1: User says "我叫小明，我喜欢蓝色"
- Turn 2: User asks "我叫什么名字？"
- Expected: Agent responds with "小明"

Usage:
    python test_multi_turn_memory.py
"""

import asyncio
import os
import sys
import json
import uuid
import time

# Add backend to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv(override=True)


async def test_memory_persistence_direct():
    """
    Test MessagesTableMemory directly without running the full agent.
    This isolates the memory layer to verify save/load works correctly.
    """
    print("\n" + "=" * 60)
    print("Test 1: Direct Memory Persistence (No Agent)")
    print("=" * 60)
    
    from services.postgresql import DBConnection
    from agentscope_integration.memory import MessagesTableMemory
    from agentscope.message import Msg
    
    db = DBConnection()
    client = await db.client
    
    # Create a unique thread for this test
    thread_id = str(uuid.uuid4())
    project_id = str(uuid.uuid4())
    
    print(f"Test Thread ID: {thread_id}")
    
    try:
        # Create memory instance (simulating Turn 1)
        memory1 = MessagesTableMemory(
            db_client=client,
            thread_id=thread_id,
            project_id=project_id,
            exclude_tool_calls=True,
        )
        
        # Add user message
        user_msg = Msg(
            name="user",
            content="我叫小明，我喜欢蓝色",
            role="user",
        )
        result = await memory1.add(user_msg)
        print(f"✓ Added user message: {result is not None}")
        
        # Add assistant response
        assistant_msg = Msg(
            name="assistant",
            content="你好小明！蓝色是一个很好的颜色选择。",
            role="assistant",
        )
        result = await memory1.add(assistant_msg)
        print(f"✓ Added assistant message: {result is not None}")
        
        # Verify messages in memory1
        messages1 = await memory1.get_memory()
        print(f"✓ Memory1 has {len(messages1)} messages")
        
        # Create NEW memory instance (simulating Turn 2 - new runner)
        memory2 = MessagesTableMemory(
            db_client=client,
            thread_id=thread_id,  # Same thread
            project_id=project_id,
            exclude_tool_calls=True,
        )
        
        # Load messages from DB
        messages2 = await memory2.get_memory()
        print(f"✓ Memory2 loaded {len(messages2)} messages from DB")
        
        # Verify content
        if len(messages2) >= 2:
            for i, msg in enumerate(messages2):
                text = msg.get_text_content() if hasattr(msg, 'get_text_content') else str(msg.content)
                print(f"  Message {i+1}: role={msg.role}, content={text[:50]}...")
            
            # Check if "小明" is in the loaded messages
            all_content = " ".join([
                msg.get_text_content() if hasattr(msg, 'get_text_content') else str(msg.content)
                for msg in messages2
            ])
            
            if "小明" in all_content:
                print("\n✅ SUCCESS: Memory persisted correctly - '小明' found in loaded messages")
                return True
            else:
                print("\n❌ FAIL: '小明' not found in loaded messages")
                return False
        else:
            print(f"\n❌ FAIL: Expected 2+ messages, got {len(messages2)}")
            return False
            
    except Exception as e:
        print(f"\n❌ ERROR: {e}")
        import traceback
        traceback.print_exc()
        return False
    finally:
        # Cleanup: Delete test messages
        try:
            await client.table('messages').eq('thread_id', thread_id).delete()
            print(f"\n✓ Cleaned up test messages for thread {thread_id}")
        except Exception as e:
            print(f"\n⚠ Failed to cleanup: {e}")


async def test_multi_turn_with_agent():
    """
    Test multi-turn conversation with actual AgentScopeRunner.
    This tests the full integration including agent processing.
    """
    print("\n" + "=" * 60)
    print("Test 2: Multi-Turn with Agent (Full Integration)")
    print("=" * 60)
    
    from services.postgresql import DBConnection
    from agentscope_integration import AgentScopeRunner
    
    db = DBConnection()
    client = await db.client
    
    # Get any existing thread and project for testing
    result = await client.table('threads').select('thread_id, project_id').order('created_at', desc=True).limit(1).execute()
    
    if not result.data:
        print("❌ No test data available - need existing thread")
        return False
    
    thread_id = result.data[0]['thread_id']
    project_id = result.data[0]['project_id']
    
    print(f"Thread ID: {thread_id}")
    print(f"Project ID: {project_id}")
    
    try:
        # Turn 1: Introduce ourselves
        print("\n--- Turn 1: Introduction ---")
        run_id_1 = str(uuid.uuid4())
        
        runner1 = AgentScopeRunner(
            thread_id=thread_id,
            project_id=project_id,
            model_key="gemini-3-flash",
            db_client=client,
        )
        
        turn1_message = "请记住：我叫小明，我最喜欢的颜色是蓝色。请确认你记住了。"
        print(f"User: {turn1_message}")
        
        turn1_response = ""
        async for chunk in runner1.run(turn1_message, run_id_1):
            if chunk.get('content'):
                try:
                    content = json.loads(chunk['content'])
                    if content.get('content'):
                        turn1_response = content['content']
                except:
                    pass
        
        print(f"Assistant: {turn1_response[:200]}...")
        
        # Wait a moment for DB writes to complete
        await asyncio.sleep(1)
        
        # Turn 2: Ask about what we said (NEW runner instance)
        print("\n--- Turn 2: Memory Test ---")
        run_id_2 = str(uuid.uuid4())
        
        runner2 = AgentScopeRunner(
            thread_id=thread_id,  # Same thread
            project_id=project_id,
            model_key="gemini-3-flash",
            db_client=client,
        )
        
        turn2_message = "我叫什么名字？我最喜欢什么颜色？"
        print(f"User: {turn2_message}")
        
        turn2_response = ""
        async for chunk in runner2.run(turn2_message, run_id_2):
            if chunk.get('content'):
                try:
                    content = json.loads(chunk['content'])
                    if content.get('content'):
                        turn2_response = content['content']
                except:
                    pass
        
        print(f"Assistant: {turn2_response[:200]}...")
        
        # Verify the agent remembered
        if "小明" in turn2_response and "蓝" in turn2_response:
            print("\n✅ SUCCESS: Agent remembered both name (小明) and color (蓝色)")
            return True
        elif "小明" in turn2_response:
            print("\n⚠ PARTIAL: Agent remembered name but not color")
            return True  # Partial success
        else:
            print("\n❌ FAIL: Agent did not remember the information")
            print(f"   Expected: '小明' and '蓝' in response")
            print(f"   Got: {turn2_response}")
            return False
            
    except Exception as e:
        print(f"\n❌ ERROR: {e}")
        import traceback
        traceback.print_exc()
        return False


async def test_db_message_query():
    """
    Diagnostic test: Query the messages table directly to see what's stored.
    """
    print("\n" + "=" * 60)
    print("Test 3: Direct DB Query (Diagnostic)")
    print("=" * 60)
    
    from services.postgresql import DBConnection
    
    db = DBConnection()
    client = await db.client
    
    # Get a recent thread
    threads = await client.table('threads').select('thread_id').order('created_at', desc=True).limit(1).execute()
    
    if not threads.data:
        print("❌ No threads found")
        return False
    
    thread_id = threads.data[0]['thread_id']
    print(f"Checking thread: {thread_id}")
    
    # Query messages
    messages = await client.table('messages').select('*').eq('thread_id', thread_id).order('created_at').limit(10).execute()
    
    if not messages.data:
        print("❌ No messages found for this thread")
        return False
    
    print(f"\nFound {len(messages.data)} messages:")
    print("-" * 40)
    
    for i, msg in enumerate(messages.data):
        msg_type = msg.get('type', 'unknown')
        role = msg.get('role', 'unknown')
        content_raw = msg.get('content', '{}')
        
        # Parse content
        if isinstance(content_raw, str):
            try:
                content = json.loads(content_raw)
            except:
                content = {'content': content_raw}
        else:
            content = content_raw
        
        text = content.get('content', '')[:80] if isinstance(content, dict) else str(content)[:80]
        has_tool_calls = bool(content.get('tool_calls')) if isinstance(content, dict) else False
        
        print(f"{i+1}. type={msg_type}, role={role}, has_tool_calls={has_tool_calls}")
        print(f"   content: {text}...")
        
        # Check metadata for thread_run_id
        metadata_raw = msg.get('metadata', '{}')
        if isinstance(metadata_raw, str):
            try:
                metadata = json.loads(metadata_raw)
            except:
                metadata = {}
        else:
            metadata = metadata_raw or {}
        
        thread_run_id = metadata.get('thread_run_id', 'N/A')
        print(f"   thread_run_id: {thread_run_id}")
        print()
    
    return True


async def main():
    """Run all tests."""
    print("\n" + "=" * 60)
    print("Multi-Turn Memory Persistence Tests")
    print("=" * 60)
    print(f"Time: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    
    results = {}
    
    # Test 1: Direct memory persistence
    try:
        results['direct_memory'] = await test_memory_persistence_direct()
    except Exception as e:
        print(f"Test 1 failed with exception: {e}")
        results['direct_memory'] = False
    
    # Test 2: DB diagnostic
    try:
        results['db_query'] = await test_db_message_query()
    except Exception as e:
        print(f"Test 3 failed with exception: {e}")
        results['db_query'] = False
    
    # Test 3: Full agent test (optional - takes longer)
    run_agent_test = os.environ.get('RUN_AGENT_TEST', 'false').lower() == 'true'
    if run_agent_test:
        try:
            results['agent_multi_turn'] = await test_multi_turn_with_agent()
        except Exception as e:
            print(f"Test 2 failed with exception: {e}")
            results['agent_multi_turn'] = False
    else:
        print("\n⏭ Skipping agent test (set RUN_AGENT_TEST=true to enable)")
        results['agent_multi_turn'] = None
    
    # Summary
    print("\n" + "=" * 60)
    print("Test Summary")
    print("=" * 60)
    print(f"  Direct Memory:     {'✅ PASS' if results.get('direct_memory') else '❌ FAIL'}")
    print(f"  DB Query:          {'✅ PASS' if results.get('db_query') else '❌ FAIL'}")
    if results.get('agent_multi_turn') is not None:
        print(f"  Agent Multi-Turn:  {'✅ PASS' if results.get('agent_multi_turn') else '❌ FAIL'}")
    else:
        print(f"  Agent Multi-Turn:  ⏭ SKIPPED")
    
    # Overall result (excluding skipped tests)
    active_results = [v for v in results.values() if v is not None]
    all_passed = all(active_results) if active_results else False
    print(f"\nOverall: {'✅ All tests passed!' if all_passed else '❌ Some tests failed'}")
    print("=" * 60)
    
    return all_passed


if __name__ == "__main__":
    result = asyncio.run(main())
    sys.exit(0 if result else 1)
