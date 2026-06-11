"""
AgentScope Integration Module

This module provides AgentScope-based agent implementation to replace Google ADK.
"""

__all__ = ["AgentScopeRunner"]


def __getattr__(name: str):
    if name == "AgentScopeRunner":
        from .runner import AgentScopeRunner

        return AgentScopeRunner
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
