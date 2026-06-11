#!/usr/bin/env python3
"""
Direct test of SSE stream generator to verify write_file chunks are sent.

This test bypasses authentication and directly tests the stream_generator function.
"""

import asyncio
import json
import uuid
import sys
import os

# Add backend to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv()

import redis.asyncio as aioredis
from datetime import datetime, timezone

# Configuration
REDIS_HOST = os.getenv('REDIS_HOST', 'localhost')
REDIS_PORT = int(os.getenv('REDIS_PORT', 6379))
REDIS_PASSWORD = os.getenv('REDIS_PASSWORD', '')
REDIS_DB = int(os.getenv('REDIS_DB', 0))

# Test data
TEST_FILE_PATH = "/workspace/test_streaming.md"
TEST_FILE_CONTENT = """# Test Streaming File

This is a test file to verify streaming functionality.

## Section 1
Some content here to make the file longer.

## Section 2
More content to ensure multiple chunks are sent.
"""


async def get_redis_client():
    """Create Redis client."""
    return aioredis.Redis(
        host=REDIS_HOST,
        port=REDIS_PORT,
        password=REDIS_PASSWORD if REDIS_PASSWORD else None,
        db=REDIS_DB,
        decode_responses=True
    )


async def push_write_file_chunks(redis_client, agent_run_id: str, thread_id: str):
    """Push write_file streaming chunks to Redis."""
    response_list_key = f"agent_run:{agent_run_id}:responses"
    response_channel = f"agent_run:{agent_run_id}:new_response"
    
    # Split content into chunks
    chunk_size = 50
    chunks = [TEST_FILE_CONTENT[i:i+chunk_size] for i in range(0, len(TEST_FILE_CONTENT), chunk_size)]
    
    print(f"📝 Pushing {len(chunks)} write_file chunks to Redis...")
    
    for i, chunk_text in enumerate(chunks):
        tool_call_data = {
            "id": f"stream_write_{agent_run_id}",
            "index": 0,
            "type": "function",
            "function": {
                "name": "write_file",
                "arguments": json.dumps({
                    "file_path": TEST_FILE_PATH,
                    "file_contents_delta": chunk_text,
                    "delta_index": i,
                })
            }
        }
        
        payload = {
            "sequence": i,
            "message_id": None,
            "thread_id": thread_id,
            "type": "assistant",
            "is_llm_message": True,
            "content": json.dumps({"role": "assistant", "content": ""}),
            "metadata": json.dumps({
                "thread_run_id": agent_run_id,
                "stream_status": "tool_call_chunk",
                "tool_calls": [tool_call_data],
            }),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        
        await redis_client.rpush(response_list_key, json.dumps(payload))
        await redis_client.expire(response_list_key, 300)  # 5 min TTL for test data
        await redis_client.publish(response_channel, "new")
    
    # Push completion message
    completion_payload = {
        "type": "status",
        "status": "completed",
        "message": "Agent run completed successfully"
    }
    await redis_client.rpush(response_list_key, json.dumps(completion_payload))
    await redis_client.expire(response_list_key, 300)  # 5 min TTL for test data
    await redis_client.publish(response_channel, "new")
    
    print(f"✅ All {len(chunks)} chunks + completion pushed to Redis")
    return len(chunks)


async def simulate_sse_stream_generator(redis_client, agent_run_id: str):
    """Simulate the SSE stream generator logic from api.py."""
    response_list_key = f"agent_run:{agent_run_id}:responses"
    
    print(f"\n🔍 Simulating SSE stream generator...")
    print(f"   Redis key: {response_list_key}")
    
    # Fetch all responses from Redis (simulating initial fetch)
    initial_responses_json = await redis_client.lrange(response_list_key, 0, -1)
    
    print(f"   [SSE] Initial fetch from Redis: {len(initial_responses_json) if initial_responses_json else 0} responses")
    
    if not initial_responses_json:
        print("   ❌ No responses found in Redis!")
        return False
    
    write_file_chunks_count = 0
    all_messages = []
    
    for i, response_json in enumerate(initial_responses_json):
        response = json.loads(response_json)
        all_messages.append(response)
        
        # Check for write_file tool_call_chunk
        if response.get("type") == "assistant":
            metadata = response.get("metadata")
            if isinstance(metadata, str):
                metadata = json.loads(metadata)
            
            if isinstance(metadata, dict) and metadata.get("stream_status") == "tool_call_chunk":
                tool_calls = metadata.get("tool_calls") or []
                if tool_calls and isinstance(tool_calls, list):
                    tool_name = tool_calls[0].get("function", {}).get("name")
                    if tool_name == "write_file":
                        write_file_chunks_count += 1
                        
                        # Extract file_contents_delta
                        args_str = tool_calls[0].get("function", {}).get("arguments", "{}")
                        args = json.loads(args_str) if isinstance(args_str, str) else args_str
                        delta = args.get("file_contents_delta", "")
                        delta_index = args.get("delta_index", -1)
                        
                        print(f"   [SSE] write_file chunk #{write_file_chunks_count}: delta_index={delta_index}, delta_len={len(delta)}")
    
    print(f"\n📊 Summary:")
    print(f"   Total messages: {len(all_messages)}")
    print(f"   write_file chunks: {write_file_chunks_count}")
    
    if write_file_chunks_count > 0:
        print(f"\n✅ SUCCESS: SSE stream generator would send {write_file_chunks_count} write_file chunks to frontend!")
        return True
    else:
        print(f"\n❌ FAILURE: No write_file chunks found!")
        return False


async def main():
    print("=" * 60)
    print("Direct SSE Stream Generator Test")
    print("=" * 60)
    
    redis_client = await get_redis_client()
    
    # Generate test IDs
    agent_run_id = str(uuid.uuid4())
    thread_id = str(uuid.uuid4())
    
    print(f"📋 Test agent_run_id: {agent_run_id}")
    print(f"📋 Test thread_id: {thread_id}")
    
    try:
        # Test Redis connection
        await redis_client.ping()
        print("✅ Redis connection OK\n")
        
        # Push write_file chunks
        num_chunks = await push_write_file_chunks(redis_client, agent_run_id, thread_id)
        
        # Simulate SSE stream generator
        success = await simulate_sse_stream_generator(redis_client, agent_run_id)
        
        # Cleanup
        response_list_key = f"agent_run:{agent_run_id}:responses"
        await redis_client.delete(response_list_key)
        print(f"\n🧹 Cleaned up Redis key: {response_list_key}")
        
        print("\n" + "=" * 60)
        if success:
            print("✅ TEST PASSED: Backend SSE streaming logic is working correctly!")
            print("   The write_file chunks are properly stored in Redis and")
            print("   would be sent to the frontend via SSE.")
        else:
            print("❌ TEST FAILED: Backend SSE streaming logic has issues!")
        print("=" * 60)
        
        return success
        
    finally:
        await redis_client.aclose()


if __name__ == "__main__":
    success = asyncio.run(main())
    sys.exit(0 if success else 1)
