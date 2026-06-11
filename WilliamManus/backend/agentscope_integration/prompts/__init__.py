"""Prompt templates for AgentScope agents."""

from .orchestrator_prompt import ORCHESTRATOR_PROMPT, get_orchestrator_prompt
from .worker_prompt import WORKER_PROMPT, get_worker_prompt

__all__ = [
    "ORCHESTRATOR_PROMPT",
    "WORKER_PROMPT", 
    "get_orchestrator_prompt",
    "get_worker_prompt",
]
