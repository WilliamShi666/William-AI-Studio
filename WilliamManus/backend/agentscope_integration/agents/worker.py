"""
Worker Agent Implementation

The Worker agent executes specific tasks assigned by the Orchestrator.
It has access to all tools (sandbox, browser, skills, etc.) and reports
results back to the Orchestrator.
"""

from typing import Optional
import os

from agentscope.tool import Toolkit

from .metadata_aware_react_agent import MetadataAwareReActAgent as ReActAgent
from ..prompts.worker_prompt import get_worker_prompt
from ..utils.workspace_guard import relocate_external_paths
from utils.logger import logger


def _env_flag(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _clone_toolkit(source_toolkit: Toolkit) -> Toolkit:
    cloned_toolkit = Toolkit()

    if hasattr(source_toolkit, "tools"):
        for registered_tool in getattr(source_toolkit, "tools", {}).values():
            func = getattr(registered_tool, "original_func", None) or getattr(
                registered_tool,
                "func",
                None,
            )
            if func is not None:
                cloned_toolkit.register_tool_function(func)
        return cloned_toolkit

    if hasattr(source_toolkit, "functions"):
        for func in getattr(source_toolkit, "functions", {}).values():
            cloned_toolkit.register_tool_function(func)

    return cloned_toolkit


class WorkerAgent:
    """
    Worker Agent - Executes tasks assigned by the Orchestrator.
    
    The Worker has access to all tools and is responsible for:
    - Executing specific tasks
    - Using tools effectively
    - Reporting results back
    """
    
    def __init__(
        self,
        model,
        formatter,
        toolkit: Toolkit,
        memory=None,
        long_term_memory=None,
        long_term_memory_mode: str = "static_control",
        max_iters: int = 500,
        compression_config=None,
    ):
        """
        Initialize the Worker agent.
        
        Args:
            model: AgentScope model instance
            formatter: AgentScope formatter instance
            toolkit: Toolkit with all registered tools
            memory: Memory instance (optional)
            long_term_memory: Read-only long-term memory adapter (optional)
            long_term_memory_mode: Long-term memory mode for ReActAgent
            max_iters: Maximum iterations for ReAct loop
            compression_config: Memory compression configuration (optional)
        """
        self.model = model
        self.formatter = formatter
        self.toolkit = _clone_toolkit(toolkit)
        self.memory = memory
        self.long_term_memory = long_term_memory
        self.long_term_memory_mode = long_term_memory_mode
        self._parallel_tool_calls_enabled = _env_flag(
            "AGENTSCOPE_PARALLEL_TOOL_CALLS",
            True,
        )

        if self.long_term_memory is not None:
            self.toolkit.register_tool_function(
                self.long_term_memory.retrieve_from_memory,
            )

        # Create the ReActAgent
        self.agent = ReActAgent(
            name="Worker",
            sys_prompt=get_worker_prompt(),
            model=model,
            formatter=formatter,
            toolkit=self.toolkit,
            memory=memory,
            long_term_memory=self.long_term_memory,
            long_term_memory_mode=self.long_term_memory_mode,
            max_iters=max_iters,
            parallel_tool_calls=self._parallel_tool_calls_enabled,
            compression_config=compression_config,
        )

        async def _workspace_guard(agent, kwargs):
            msg = kwargs.get("msg")
            last = kwargs.get("last", True)
            if not last or not msg or getattr(msg, "role", None) != "assistant":
                return None
            updated_msg, replacements = await relocate_external_paths(
                msg,
                agent.toolkit,
                label="worker",
            )
            if replacements:
                patched = dict(kwargs)
                patched["msg"] = updated_msg
                return patched
            return None

        self.agent.register_instance_hook(
            hook_type="pre_print",
            hook_name="workspace_guard",
            hook=_workspace_guard,
        )
        
        logger.info(
            "[WorkerAgent] Initialized with %d tools, parallel_tool_calls=%s",
            len(self.toolkit.get_json_schemas()),
            self._parallel_tool_calls_enabled,
        )
    
    async def __call__(self, msg):
        """
        Execute a task.
        
        Args:
            msg: AgentScope Msg with task description
            
        Returns:
            AgentScope Msg with execution result
        """
        return await self.agent(msg)
    
    def set_console_output_enabled(self, enabled: bool):
        """Enable or disable console output."""
        self.agent.set_console_output_enabled(enabled)
    
    def get_agent(self) -> ReActAgent:
        """Get the underlying ReActAgent."""
        return self.agent
