"""
Test script for AgentScope integration.

This script tests the basic flow of the AgentScope-based agent runner.
"""

import asyncio
import os
import sys

# Add backend to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from dotenv import load_dotenv
load_dotenv(override=True)


async def test_agentscope_runner():
    """Test the AgentScopeRunner directly."""
    from agentscope_integration import AgentScopeRunner
    
    print("=" * 60)
    print("Testing AgentScopeRunner")
    print("=" * 60)
    
    # Create runner without database (for testing)
    runner = AgentScopeRunner(
        thread_id="test-thread-123",
        project_id="test-project-456",
        model_key="gemini-3-flash",
        db_client=None,  # No database for this test
    )
    
    print(f"Runner created: {runner}")
    print(f"  thread_id: {runner.thread_id}")
    print(f"  project_id: {runner.project_id}")
    print(f"  model_key: {runner.model_key}")
    
    # Test simple run (non-streaming)
    print("\nTesting simple run...")
    try:
        result = await runner.run_simple("Hello, what is 2 + 2?")
        print(f"Result: {result}")
    except Exception as e:
        print(f"Error: {e}")
        import traceback
        traceback.print_exc()
    
    print("\n" + "=" * 60)
    print("Test completed")
    print("=" * 60)


async def test_model_factory():
    """Test the ModelFactory."""
    from agentscope_integration.models import ModelFactory
    
    print("=" * 60)
    print("Testing ModelFactory")
    print("=" * 60)
    
    for model_key in ["gemini-3-flash", "gemini-3-pro", "minimax-m2.1", "deepseek-chat", "kimi-k2.5"]:
        try:
            model, formatter = ModelFactory.create(model_key)
            print(f"  {model_key}: OK")
        except Exception as e:
            print(f"  {model_key}: FAILED - {e}")
    
    print("=" * 60)


async def main():
    """Run all tests."""
    print("\n" + "=" * 60)
    print("AgentScope Integration Tests")
    print("=" * 60 + "\n")
    
    # Test model factory first
    await test_model_factory()
    
    # Test runner
    await test_agentscope_runner()


if __name__ == "__main__":
    asyncio.run(main())
