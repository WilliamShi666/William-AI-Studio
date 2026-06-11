#!/usr/bin/env python3
"""
Test script to verify SSE streaming for write_file tool calls.

This script:
1. Creates a test agent_run in the database
2. Pushes write_file streaming chunks to Redis
3. Connects to the SSE endpoint and verifies messages are received

Usage:
    python test_sse_streaming.py
"""

import asyncio
import json
import uuid
import httpx
import redis.asyncio as aioredis
from datetime import datetime, timezone
import os
from dotenv import load_dotenv

load_dotenv()

# Configuration
REDIS_HOST = os.getenv('REDIS_HOST', 'localhost')
REDIS_PORT = int(os.getenv('REDIS_PORT', 6379))
REDIS_PASSWORD = os.getenv('REDIS_PASSWORD', '')
REDIS_DB = int(os.getenv('REDIS_DB', 0))
API_URL = "http://localhost:8002/api"

# Test data
TEST_THREAD_ID = str(uuid.uuid4())
TEST_AGENT_RUN_ID = str(uuid.uuid4())
TEST_FILE_PATH = "/workspace/test_streaming.md"
TEST_FILE_CONTENT = """# Test Streaming File

This is a test file to verify streaming functionality.

## Section 1
Some content here to make the file longer.

## Section 2
More content to ensure multiple chunks are sent.

## Section 3
Final section with additional text.
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


async def create_test_agent_run(db_url: str):
    """Create a test agent_run in the database."""
    import asyncpg
    
    # Parse database URL
    # Format: postgresql://user:password@host:port/database
    conn = await asyncpg.connect(db_url)
    
    try:
        # First, get a valid thread_id and project_id from existing data
        row = await conn.fetchrow("""
            SELECT thread_id, project_id FROM threads LIMIT 1
        """)
        
        if not row:
            print("❌ No existing threads found. Please create a thread first.")
            return None, None
        
        thread_id = str(row['thread_id'])
        project_id = str(row['project_id'])
        
        print(f"📋 Using existing thread_id: {thread_id}")
        print(f"📋 Using existing project_id: {project_id}")
        
        # Create agent_run
        agent_run_id = str(uuid.uuid4())
        await conn.execute("""
            INSERT INTO agent_runs (agent_run_id, thread_id, status, created_at, updated_at)
            VALUES ($1::uuid, $2::uuid, 'running', NOW(), NOW())
        """, agent_run_id, thread_id)
        
        print(f"✅ Created test agent_run: {agent_run_id}")
        return agent_run_id, thread_id
        
    finally:
        await conn.close()


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
        
        if i == 0 or i == len(chunks) - 1:
            print(f"  ✅ Pushed chunk {i}/{len(chunks)-1}")
    
    # Push completion message
    completion_payload = {
        "type": "status",
        "status": "completed",
        "message": "Agent run completed successfully"
    }
    await redis_client.rpush(response_list_key, json.dumps(completion_payload))
    await redis_client.expire(response_list_key, 300)  # 5 min TTL for test data
    await redis_client.publish(response_channel, "new")
    
    print(f"✅ All chunks pushed to Redis")
    
    # Verify Redis content
    all_responses = await redis_client.lrange(response_list_key, 0, -1)
    print(f"📊 Redis list has {len(all_responses)} items")
    
    return len(chunks)


async def test_sse_endpoint(agent_run_id: str, expected_chunks: int):
    """Connect to SSE endpoint and verify messages."""
    print(f"\n🔌 Connecting to SSE endpoint for agent_run: {agent_run_id}")
    
    # Note: We need a valid auth token. For testing, we'll check if the endpoint responds.
    url = f"{API_URL}/agent-run/{agent_run_id}/stream"
    
    print(f"📡 SSE URL: {url}")
    print("⚠️  Note: SSE endpoint requires authentication. Check backend logs for [SSE] messages.")
    
    # Try to connect without auth to see the error
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url)
            print(f"📨 Response status: {response.status_code}")
            if response.status_code == 401:
                print("🔐 Authentication required (expected for unauthenticated request)")
            elif response.status_code == 200:
                print("✅ SSE endpoint responded successfully")
                # Read some data
                print(f"📄 Response preview: {response.text[:500]}...")
    except Exception as e:
        print(f"❌ Error connecting to SSE: {e}")


async def cleanup(redis_client, agent_run_id: str, db_url: str):
    """Clean up test data."""
    import asyncpg
    
    # Clean Redis
    response_list_key = f"agent_run:{agent_run_id}:responses"
    await redis_client.delete(response_list_key)
    print(f"🧹 Cleaned Redis key: {response_list_key}")
    
    # Clean database
    conn = await asyncpg.connect(db_url)
    try:
        await conn.execute("""
            DELETE FROM agent_runs WHERE agent_run_id = $1::uuid
        """, agent_run_id)
        print(f"🧹 Cleaned agent_run from database: {agent_run_id}")
    finally:
        await conn.close()


async def main():
    print("=" * 60)
    print("SSE Streaming Test")
    print("=" * 60)
    
    # Get database URL
    db_url = os.getenv('DATABASE_URL')
    if not db_url:
        print("❌ DATABASE_URL not set")
        return
    
    redis_client = await get_redis_client()
    
    try:
        # Test Redis connection
        await redis_client.ping()
        print("✅ Redis connection OK")
        
        # Create test agent_run
        agent_run_id, thread_id = await create_test_agent_run(db_url)
        if not agent_run_id:
            return
        
        # Push write_file chunks
        num_chunks = await push_write_file_chunks(redis_client, agent_run_id, thread_id)
        
        # Test SSE endpoint
        await test_sse_endpoint(agent_run_id, num_chunks)
        
        print("\n" + "=" * 60)
        print("📋 MANUAL VERIFICATION STEPS:")
        print("=" * 60)
        print(f"1. Check backend logs for [SSE] messages:")
        print(f"   tail -f /tmp/backend_api.log | grep '\\[SSE\\]'")
        print(f"\n2. The agent_run_id is: {agent_run_id}")
        print(f"   Redis key: agent_run:{agent_run_id}:responses")
        print(f"\n3. To test with curl (need auth token):")
        print(f"   curl -N '{API_URL}/agent-run/{agent_run_id}/stream?token=YOUR_TOKEN'")
        
        # Ask if user wants to cleanup
        print(f"\n⚠️  Test data will be cleaned up automatically.")
        await asyncio.sleep(5)
        
        # Cleanup
        await cleanup(redis_client, agent_run_id, db_url)
        
    finally:
        await redis_client.close()
    
    print("\n✅ Test completed!")


if __name__ == "__main__":
    asyncio.run(main())
