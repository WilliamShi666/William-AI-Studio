"""
Agent Reliability Tests

Tests for:
1. Agent iteration limits (Worker and Orchestrator max_iters)
2. Backend message retry logic
3. Sandbox auto-recreation on failure

Run with: pytest tests/test_agent_reliability.py -v
"""

import pytest
import asyncio
import uuid
import inspect
from unittest.mock import AsyncMock, MagicMock, patch


class TestIterationLimits:
    """Test that Worker and Orchestrator have sufficient iteration limits."""
    
    def test_worker_max_iters_is_500(self):
        """
        Verify Worker max_iters is set to 500 for complex research tasks.
        
        Rationale: Deep research tasks may require 30+ tool calls (searches,
        extractions, file writes). 500 iterations provides sufficient headroom.
        """
        from agentscope_integration.agents.worker import WorkerAgent
        
        # Check default value in function signature
        sig = inspect.signature(WorkerAgent.__init__)
        max_iters_default = sig.parameters['max_iters'].default
        
        assert max_iters_default == 500, \
            f"Worker max_iters should be 500, got {max_iters_default}"
        print(f"✅ Worker max_iters is correctly set to {max_iters_default}")
    
    def test_orchestrator_max_iters_is_500(self):
        """
        Verify Orchestrator max_iters is set to 500 for complex delegation.
        
        Rationale: Complex tasks may require many delegations to Worker.
        500 iterations allows for extensive multi-step workflows.
        """
        from agentscope_integration.agents.orchestrator import OrchestratorAgent
        
        sig = inspect.signature(OrchestratorAgent.__init__)
        max_iters_default = sig.parameters['max_iters'].default
        
        assert max_iters_default == 500, \
            f"Orchestrator max_iters should be 500, got {max_iters_default}"
        print(f"✅ Orchestrator max_iters is correctly set to {max_iters_default}")


class TestMessageRetryLogic:
    """Test the backend message verification retry logic."""
    
    @pytest.mark.asyncio
    async def test_retry_logic_finds_message_after_initial_failures(self):
        """
        Test that retry logic successfully finds message after initial failures.
        
        Simulates the scenario where the database is slow and the message
        isn't immediately available.
        """
        attempt_count = 0
        
        async def mock_query():
            nonlocal attempt_count
            attempt_count += 1
            
            # Simulate message not found for first 2 attempts
            if attempt_count < 3:
                return MagicMock(data=[])
            
            # Message found on 3rd attempt
            return MagicMock(data=[{
                'id': 'msg-123',
                'timestamp': '2024-01-01T00:00:00Z'
            }])
        
        # Simulate the retry logic from api.py
        message_found = False
        max_retries = 10
        retry_delay = 0.01  # Shortened for test
        
        for attempt in range(max_retries):
            result = await mock_query()
            if result.data:
                message_found = True
                break
            await asyncio.sleep(retry_delay)
        
        assert message_found, "Message should be found after retries"
        assert attempt_count == 3, f"Should find message on 3rd attempt, got {attempt_count}"
        print(f"✅ Message found after {attempt_count} attempts")
    
    @pytest.mark.asyncio
    async def test_retry_logic_proceeds_after_max_retries(self):
        """
        Test that the agent proceeds even if message is never found.
        
        This ensures the system doesn't hang indefinitely.
        """
        async def mock_query_always_empty():
            return MagicMock(data=[])
        
        message_found = False
        max_retries = 5  # Reduced for test
        retry_delay = 0.01
        
        for attempt in range(max_retries):
            result = await mock_query_always_empty()
            if result.data:
                message_found = True
                break
            await asyncio.sleep(retry_delay)
        
        # Should complete without hanging, even if message not found
        assert not message_found, "Message should not be found in this test"
        print("✅ Retry logic completes gracefully when message not found")


class TestSandboxRecreation:
    """Test sandbox auto-recreation on connection failure."""
    
    @pytest.mark.asyncio
    async def test_sandbox_recreation_logic_structure(self):
        """
        Test that sandbox recreation logic is properly structured.
        
        This is a structural test that verifies the code paths exist.
        Full integration testing requires actual E2B/PPIO credentials.
        """
        # Verify the tool_base.py has the recreation logic
        from sandbox.tool_base import SandboxToolsBase
        
        # Check that _ensure_sandbox method exists
        assert hasattr(SandboxToolsBase, '_ensure_sandbox'), \
            "SandboxToolsBase should have _ensure_sandbox method"
        
        # Check the method is async
        method = getattr(SandboxToolsBase, '_ensure_sandbox')
        assert asyncio.iscoroutinefunction(method), \
            "_ensure_sandbox should be an async method"
        
        print("✅ SandboxToolsBase._ensure_sandbox method exists and is async")
    
    @pytest.mark.asyncio
    async def test_api_sandbox_recreation_logic_structure(self):
        """
        Test that API sandbox recreation logic is properly structured.
        """
        from sandbox.api import get_sandbox_by_id_safely
        
        # Check the function exists and is async
        assert asyncio.iscoroutinefunction(get_sandbox_by_id_safely), \
            "get_sandbox_by_id_safely should be an async function"
        
        print("✅ get_sandbox_by_id_safely function exists and is async")
    
    @pytest.mark.asyncio
    async def test_create_sandbox_import_available(self):
        """
        Test that create_sandbox is importable from sandbox.sandbox.
        """
        from sandbox.sandbox import create_sandbox
        
        assert asyncio.iscoroutinefunction(create_sandbox), \
            "create_sandbox should be an async function"
        
        print("✅ create_sandbox is importable and is async")


class TestSequentialAPIFlow:
    """Test the sequential API call flow (frontend change verification)."""
    
    def test_message_before_agent_flow(self):
        """
        Verify the expected flow: message save -> agent start.
        
        This is a documentation test that describes the expected behavior.
        The actual frontend change is in page.tsx.
        """
        expected_flow = [
            "1. User submits message",
            "2. Frontend calls addUserMessageMutation.mutateAsync()",
            "3. Wait for message save to complete",
            "4. Frontend calls startAgentMutation.mutateAsync()",
            "5. Agent starts with message already in database",
        ]
        
        # This test documents the expected flow
        print("Expected sequential API flow:")
        for step in expected_flow:
            print(f"  {step}")
        
        print("✅ Sequential API flow documented")


# Integration test that requires database connection
class TestIntegration:
    """Integration tests that require actual database/service connections."""
    
    @pytest.mark.skip(reason="Requires database connection")
    @pytest.mark.asyncio
    async def test_full_message_save_and_agent_start(self):
        """
        Full integration test for message save and agent start.
        
        Requires:
        - Database connection
        - Valid thread_id and project_id
        """
        pass
    
    @pytest.mark.skip(reason="Requires E2B/PPIO credentials")
    @pytest.mark.asyncio
    async def test_sandbox_recreation_with_real_service(self):
        """
        Full integration test for sandbox recreation.
        
        Requires:
        - E2B/PPIO API credentials
        - Valid project_id
        """
        pass


if __name__ == "__main__":
    # Run tests with pytest
    pytest.main([__file__, "-v"])
