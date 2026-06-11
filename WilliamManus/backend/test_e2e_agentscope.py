"""
End-to-end test for AgentScope integration.

This script tests:
1. Basic agent response (simple questions)
2. Sandbox code execution (real agent workflow with tools)

Usage:
    python test_e2e_agentscope.py
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


async def get_project_with_sandbox(client):
    """Get a project that has an active sandbox."""
    result = await client.table('projects').select('project_id, name, sandbox').order('created_at', desc=True).limit(10).execute()
    
    for p in result.data or []:
        sandbox = p.get('sandbox', '{}')
        if isinstance(sandbox, str):
            try:
                sandbox = json.loads(sandbox) if sandbox.strip() else {}
            except:
                sandbox = {}
        if sandbox.get('id'):
            # Get thread for this project
            threads = await client.table('threads').select('thread_id').eq('project_id', p['project_id']).order('created_at', desc=True).limit(1).execute()
            thread_id = threads.data[0]['thread_id'] if threads.data else None
            return p['project_id'], thread_id, p.get('name', 'unnamed')
    
    return None, None, None


async def get_any_thread_and_project(client):
    """Get any existing thread and project from the database for testing."""
    result = await client.table('threads').select('thread_id, project_id').order('created_at', desc=True).limit(1).execute()
    
    if result.data and len(result.data) > 0:
        thread_id = result.data[0]['thread_id']
        project_id = result.data[0]['project_id']
        return thread_id, project_id
    
    return None, None


async def test_simple_response():
    """Test basic agent response without tools."""
    print("\n" + "=" * 60)
    print("Test 1: Simple Response (No Tools)")
    print("=" * 60)
    
    from services.postgresql import DBConnection
    from agentscope_integration import AgentScopeRunner
    
    db = DBConnection()
    client = await db.client
    
    # Get any thread and project
    thread_id, project_id = await get_any_thread_and_project(client)
    
    if not thread_id or not project_id:
        print("❌ No test data available")
        return False
    
    print(f"Thread ID: {thread_id}")
    print(f"Project ID: {project_id}")
    
    # Create runner
    runner = AgentScopeRunner(
        thread_id=thread_id,
        project_id=project_id,
        model_key="gemini-3-flash",
        db_client=client,
    )
    
    # Simple test
    test_message = "What is 2 + 2? Please answer briefly in one sentence."
    print(f"\nTask: {test_message}")
    print("\nExecuting...")
    
    try:
        thread_run_id = str(uuid.uuid4())
        response_chunks = []
        final_content = ""
        start_time = time.time()
        
        async for chunk in runner.run(test_message, thread_run_id):
            response_chunks.append(chunk)
            
            if chunk.get('content'):
                try:
                    content = json.loads(chunk['content'])
                    if content.get('content'):
                        final_content = content['content']
                except:
                    pass
        
        duration = time.time() - start_time
        print(f"\n✅ Completed in {duration:.1f}s")
        print(f"Response: {final_content[:200] if final_content else 'No content'}")
        print(f"Total chunks: {len(response_chunks)}")
        
        return len(response_chunks) > 0 and final_content
        
    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()
        return False


async def test_sandbox_code_execution():
    """Test code execution in sandbox - the real agent workflow."""
    print("\n" + "=" * 60)
    print("Test 2: Sandbox Code Execution (With Tools)")
    print("=" * 60)
    
    from services.postgresql import DBConnection
    from agentscope_integration import AgentScopeRunner
    
    db = DBConnection()
    client = await db.client
    
    # Get project with sandbox
    project_id, thread_id, project_name = await get_project_with_sandbox(client)
    
    if not project_id or not thread_id:
        print("❌ No project with sandbox found")
        print("   Please create a project with sandbox first via the frontend")
        return False
    
    print(f"Project: {project_name[:50]}...")
    print(f"Project ID: {project_id}")
    print(f"Thread ID: {thread_id}")
    
    # Create runner
    runner = AgentScopeRunner(
        thread_id=thread_id,
        project_id=project_id,
        model_key="gemini-3-flash",
        db_client=client,
    )
    
    # Complex task that requires sandbox tools
    test_task = """请在沙箱中完成以下任务：
1. 创建一个名为 agentscope_test.py 的 Python 文件
2. 文件内容为一个简单的Python脚本，打印当前时间和 "Hello from AgentScope Test!"
3. 运行这个脚本并告诉我输出结果"""
    
    print(f"\nTask: {test_task}")
    print("\nExecuting (this may take a few minutes)...")
    print("-" * 40)
    
    try:
        thread_run_id = str(uuid.uuid4())
        response_chunks = []
        tool_calls_seen = []
        last_content = ""
        start_time = time.time()
        last_print_time = start_time
        
        async for chunk in runner.run(test_task, thread_run_id):
            response_chunks.append(chunk)
            elapsed = time.time() - start_time
            
            # Track tool calls and responses
            if chunk.get('content'):
                try:
                    content = json.loads(chunk['content'])
                    
                    # Track tool calls
                    if content.get('tool_calls'):
                        for tc in content['tool_calls']:
                            tool_name = tc.get('function', {}).get('name', 'unknown')
                            if tool_name not in tool_calls_seen:
                                tool_calls_seen.append(tool_name)
                                print(f"  [{elapsed:6.1f}s] 🔧 Tool called: {tool_name}")
                    
                    # Track text responses (but don't spam)
                    if content.get('content'):
                        last_content = content['content']
                        # Print progress every 10 seconds
                        if time.time() - last_print_time > 10:
                            preview = last_content[:80].replace('\n', ' ')
                            print(f"  [{elapsed:6.1f}s] 💬 {preview}...")
                            last_print_time = time.time()
                            
                except:
                    pass
            
            # Timeout check (10 minutes)
            if elapsed > 600:
                print(f"\n❌ Timeout after {elapsed:.1f}s")
                return False
        
        duration = time.time() - start_time
        print("-" * 40)
        print(f"\n✅ Completed in {duration:.1f}s")
        print(f"Total chunks: {len(response_chunks)}")
        print(f"Tools used: {tool_calls_seen}")
        
        # Print final response
        if last_content:
            print(f"\nFinal Response:")
            print("-" * 40)
            # Print up to 500 chars
            print(last_content[:500] + ("..." if len(last_content) > 500 else ""))
            print("-" * 40)
        
        # Verify expected tools were called
        expected_tools = ['write_file', 'execute_command']
        tools_called = [t for t in expected_tools if t in tool_calls_seen]
        missing_tools = [t for t in expected_tools if t not in tool_calls_seen]
        
        if missing_tools:
            print(f"\n⚠️  Warning: Expected tools not called: {missing_tools}")
            print(f"   Tools that were called: {tool_calls_seen}")
        
        if tools_called:
            print(f"\n✅ Expected tools called: {tools_called}")
            return True
        else:
            print(f"\n❌ No expected tools were called")
            return False
        
    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()
        return False


async def main():
    """Run end-to-end tests."""
    print("\n" + "=" * 60)
    print("AgentScope End-to-End Tests")
    print("=" * 60)
    print(f"Timeout: 10 minutes per test")
    print(f"Time: {time.strftime('%Y-%m-%d %H:%M:%S')}")
    
    results = {}
    
    # Test 1: Simple response (quick sanity check)
    try:
        results['simple'] = await test_simple_response()
    except Exception as e:
        print(f"Test 1 failed with exception: {e}")
        results['simple'] = False
    
    if not results['simple']:
        print("\n❌ Basic test failed, skipping sandbox test")
        results['sandbox'] = False
    else:
        # Test 2: Sandbox code execution (real agent workflow)
        try:
            results['sandbox'] = await test_sandbox_code_execution()
        except Exception as e:
            print(f"Test 2 failed with exception: {e}")
            results['sandbox'] = False
    
    # Summary
    print("\n" + "=" * 60)
    print("Test Summary")
    print("=" * 60)
    print(f"  Simple Response:    {'✅ PASS' if results.get('simple') else '❌ FAIL'}")
    print(f"  Sandbox Execution:  {'✅ PASS' if results.get('sandbox') else '❌ FAIL'}")
    
    all_passed = all(results.values())
    print(f"\nOverall: {'✅ All tests passed!' if all_passed else '❌ Some tests failed'}")
    print("=" * 60)
    
    return all_passed


if __name__ == "__main__":
    result = asyncio.run(main())
    sys.exit(0 if result else 1)
