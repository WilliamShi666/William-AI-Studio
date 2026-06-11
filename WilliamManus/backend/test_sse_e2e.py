#!/usr/bin/env python3
"""
End-to-end test of SSE streaming with a real agent run.

This test:
1. Creates a real agent run via the API
2. Pushes write_file chunks to Redis (simulating the agent writing a file)
3. Connects to the SSE endpoint and verifies messages are received
"""

import asyncio
import json
import uuid
import sys
import os
import httpx

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
API_URL = "http://localhost:8002/api"
DATABASE_URL = os.getenv('DATABASE_URL')

# Test data
TEST_FILE_PATH = "/workspace/test_streaming.md"
TEST_FILE_CONTENT = """# Test Streaming

Content chunk 1.
Content chunk 2.
Content chunk 3.
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


async def get_test_user_token():
    """Get a test user token by creating a JWT for the codex test user."""
    import asyncpg
    import jwt
    from datetime import timedelta
    
    conn = await asyncpg.connect(DATABASE_URL)
    
    try:
        # Get codex test user
        row = await conn.fetchrow("""
            SELECT id, email FROM users WHERE email = 'codex.test+ssr1@local.dev' LIMIT 1
        """)
        
        if not row:
            print("❌ Codex test user not found in database")
            return None, None
        
        user_id = str(row['id'])
        email = row['email']
        print(f"📋 Using test user: {email} (id: {user_id})")
        
        # Create JWT token using the same secret as the backend
        JWT_SECRET = os.getenv('JWT_SECRET_KEY')
        if not JWT_SECRET:
            raise RuntimeError("JWT_SECRET_KEY must be set to run this E2E test")
        token = jwt.encode(
            {
                'sub': user_id,
                'exp': datetime.utcnow() + timedelta(hours=1),
                'iat': datetime.utcnow()
            },
            JWT_SECRET,
            algorithm='HS256'
        )
        
        return token, user_id
        
    finally:
        await conn.close()


async def create_agent_run(user_id: str):
    """Create a test agent run in the database with a thread owned by the user."""
    import asyncpg
    conn = await asyncpg.connect(DATABASE_URL)
    
    try:
        # First, check if user has any existing threads
        # Note: account_id in threads is varchar, not uuid
        row = await conn.fetchrow("""
            SELECT thread_id, project_id 
            FROM threads 
            WHERE account_id = $1
            LIMIT 1
        """, user_id)
        
        if not row:
            # Create a test project and thread for this user
            project_id = str(uuid.uuid4())
            await conn.execute("""
                INSERT INTO projects (project_id, account_id, name, status, created_at, updated_at)
                VALUES ($1::uuid, $2, 'SSE Test Project', 'active', NOW(), NOW())
            """, project_id, user_id)
            print(f"✅ Created test project: {project_id}")
            
            thread_id = str(uuid.uuid4())
            await conn.execute("""
                INSERT INTO threads (thread_id, project_id, account_id, name, status, created_at, updated_at)
                VALUES ($1::uuid, $2::uuid, $3, 'SSE Test Thread', 'active', NOW(), NOW())
            """, thread_id, project_id, user_id)
            print(f"✅ Created test thread: {thread_id}")
        else:
            thread_id = str(row['thread_id'])
            project_id = str(row['project_id'])
            print(f"📋 Using existing thread: {thread_id}")
        
        # Create agent run
        agent_run_id = str(uuid.uuid4())
        await conn.execute("""
            INSERT INTO agent_runs (agent_run_id, thread_id, status, created_at, updated_at)
            VALUES ($1::uuid, $2::uuid, 'running', NOW(), NOW())
        """, agent_run_id, thread_id)
        
        print(f"✅ Created agent_run: {agent_run_id}")
        print(f"   thread_id: {thread_id}")
        
        return agent_run_id, thread_id
        
    finally:
        await conn.close()


async def push_write_file_chunks(redis_client, agent_run_id: str, thread_id: str):
    """Push write_file streaming chunks to Redis."""
    response_list_key = f"agent_run:{agent_run_id}:responses"
    response_channel = f"agent_run:{agent_run_id}:new_response"
    
    # Split content into chunks
    chunk_size = 30
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
    
    print(f"✅ Pushed {len(chunks)} chunks")
    return len(chunks)


async def test_sse_endpoint(agent_run_id: str, token: str, expected_chunks: int):
    """Connect to SSE endpoint and verify messages."""
    print(f"\n🔌 Connecting to SSE endpoint...")
    
    url = f"{API_URL}/agent-run/{agent_run_id}/stream?token={token}"
    
    received_chunks = 0
    received_messages = []
    
    try:
        # Use a shorter timeout since we just want to verify initial messages
        async with httpx.AsyncClient(timeout=10.0) as client:
            async with client.stream('GET', url) as response:
                print(f"📨 Response status: {response.status_code}")
                
                if response.status_code != 200:
                    error_text = await response.aread()
                    print(f"❌ Error: {error_text.decode()}")
                    return False
                
                print("📡 Reading SSE stream (will timeout after receiving initial messages)...")
                
                try:
                    async for line in response.aiter_lines():
                        if not line or not line.startswith('data: '):
                            continue
                        
                        data_str = line[6:]  # Remove 'data: ' prefix
                        
                        try:
                            data = json.loads(data_str)
                            received_messages.append(data)
                            
                            # Check for write_file chunks
                            if data.get('type') == 'assistant':
                                metadata = data.get('metadata', {})
                                if isinstance(metadata, str):
                                    metadata = json.loads(metadata)
                                
                                if metadata.get('stream_status') == 'tool_call_chunk':
                                    tool_calls = metadata.get('tool_calls', [])
                                    if tool_calls:
                                        tool_name = tool_calls[0].get('function', {}).get('name')
                                        if tool_name == 'write_file':
                                            received_chunks += 1
                                            print(f"   ✅ Received write_file chunk #{received_chunks}")
                            
                            # Check for completion
                            if data.get('type') == 'status' and data.get('status') == 'completed':
                                print("   📋 Received completion message")
                                break
                            
                            # Stop after receiving expected chunks (don't wait for completion)
                            if received_chunks >= expected_chunks:
                                print(f"   📋 Received all {expected_chunks} expected chunks, stopping")
                                break
                                
                        except json.JSONDecodeError:
                            continue
                except asyncio.TimeoutError:
                    print("   ⏱️ Stream read timed out (expected for long-running streams)")
                
    except Exception as e:
        print(f"❌ Error: {e}")
        return False
    
    print(f"\n📊 Results:")
    print(f"   Total messages received: {len(received_messages)}")
    print(f"   write_file chunks received: {received_chunks}")
    print(f"   Expected chunks: {expected_chunks}")
    
    if received_chunks >= expected_chunks:
        print(f"\n✅ SUCCESS: All {expected_chunks} write_file chunks received!")
        return True
    else:
        print(f"\n❌ FAILURE: Expected {expected_chunks} chunks, got {received_chunks}")
        return False


async def cleanup(redis_client, agent_run_id: str):
    """Clean up test data."""
    import asyncpg
    
    # Clean Redis
    response_list_key = f"agent_run:{agent_run_id}:responses"
    await redis_client.delete(response_list_key)
    
    # Clean database
    conn = await asyncpg.connect(DATABASE_URL)
    try:
        await conn.execute("""
            DELETE FROM agent_runs WHERE agent_run_id = $1::uuid
        """, agent_run_id)
    finally:
        await conn.close()
    
    print(f"🧹 Cleaned up test data")


async def main():
    print("=" * 60)
    print("End-to-End SSE Streaming Test")
    print("=" * 60)
    
    redis_client = await get_redis_client()
    agent_run_id = None
    
    try:
        # Test Redis connection
        await redis_client.ping()
        print("✅ Redis connection OK")
        
        # Get auth token
        token, user_id = await get_test_user_token()
        if not token:
            print("❌ Could not get auth token")
            return False
        
        # Create agent run
        agent_run_id, thread_id = await create_agent_run(user_id)
        if not agent_run_id:
            return False
        
        # Push write_file chunks
        num_chunks = await push_write_file_chunks(redis_client, agent_run_id, thread_id)
        
        # Small delay to ensure Redis is ready
        await asyncio.sleep(0.5)
        
        # Test SSE endpoint
        success = await test_sse_endpoint(agent_run_id, token, num_chunks)
        
        return success
        
    finally:
        if agent_run_id:
            await cleanup(redis_client, agent_run_id)
        await redis_client.aclose()


if __name__ == "__main__":
    success = asyncio.run(main())
    print("\n" + "=" * 60)
    if success:
        print("✅ END-TO-END TEST PASSED!")
    else:
        print("❌ END-TO-END TEST FAILED!")
    print("=" * 60)
    sys.exit(0 if success else 1)
