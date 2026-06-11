"""Shared AgentScope context and compression builders."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Optional



def _env_flag(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class ToolCallMemoryPolicy:
    """Model-family specific memory settings for tool messages."""

    api_format: str
    keep_tool_results: bool
    drop_tool_call_only: bool


@dataclass(frozen=True)
class ContextBuilderSettings:
    """Env-derived settings shared across runner and Shadow Clone agent builds."""

    retain_complete_tool_runs: int
    enable_tool_history_summary: bool
    tool_history_summary_max_runs: int
    tool_history_summary_max_chars: int
    kv_cache_canonical_json: bool
    kv_cache_contract_version: str

    # Micro Compact
    enable_micro_compact: bool
    # TailTrim — cache-preserving middle-message removal
    enable_tail_trim: bool
    tail_trim_keep_turns: int
    # Auto Compact
    enable_auto_compact: bool
    auto_compact_trigger_tokens: int
    auto_compact_model_key: str
    auto_compact_tail_turns: int
    auto_compact_shadow_mode: bool


class ContextBuilder:
    """Build shared memory policies and compression configs for AgentScope agents."""

    def __init__(self, settings: ContextBuilderSettings) -> None:
        self.settings = settings

    @classmethod
    def from_env(cls) -> "ContextBuilder":
        settings = ContextBuilderSettings(
            retain_complete_tool_runs=int(
                os.environ.get(
                    "AGENTSCOPE_RETAIN_COMPLETE_TOOL_RUNS",
                    os.environ.get("AGENTSCOPE_RETAIN_RECENT_TOOL_RUNS", "3"),
                )
            ),
            enable_tool_history_summary=_env_flag(
                "AGENTSCOPE_ENABLE_TOOL_HISTORY_SUMMARY",
                default=True,
            ),
            tool_history_summary_max_runs=int(
                os.environ.get("AGENTSCOPE_TOOL_HISTORY_SUMMARY_MAX_RUNS", "8"),
            ),
            tool_history_summary_max_chars=int(
                os.environ.get("AGENTSCOPE_TOOL_HISTORY_SUMMARY_MAX_CHARS", "8000"),
            ),
            kv_cache_canonical_json=_env_flag(
                "AGENTSCOPE_KV_CACHE_CANONICAL_JSON",
                default=True,
            ),
            kv_cache_contract_version=str(
                os.environ.get("AGENTSCOPE_KV_CACHE_CONTRACT_VERSION", "v1"),
            ).strip().lower() or "v1",
            # Micro Compact
            enable_micro_compact=_env_flag(
                "AGENTSCOPE_MICRO_COMPACT_ENABLED",
                default=True,
            ),
            # TailTrim — cache-preserving middle-message removal (default: on, 8 turns)
            enable_tail_trim=_env_flag(
                "AGENTSCOPE_TAIL_TRIM_ENABLED",
                default=True,
            ),
            tail_trim_keep_turns=int(
                os.environ.get("AGENTSCOPE_TAIL_TRIM_KEEP_TURNS", "8"),
            ),
            # Auto Compact
            enable_auto_compact=_env_flag(
                "AGENTSCOPE_AUTO_COMPACT_ENABLED",
                default=True,
            ),
            auto_compact_trigger_tokens=int(
                os.environ.get("AGENTSCOPE_AUTO_COMPACT_TRIGGER_TOKENS", "90000"),
            ),
            auto_compact_model_key=os.environ.get(
                "AGENTSCOPE_AUTO_COMPACT_MODEL", "deepseek-v4-flash",
            ),
            auto_compact_tail_turns=int(
                os.environ.get("AGENTSCOPE_AUTO_COMPACT_TAIL_TURNS", "2"),
            ),
            auto_compact_shadow_mode=_env_flag(
                "AGENTSCOPE_AUTO_COMPACT_SHADOW_MODE",
                default=False,
            ),
        )
        return cls(settings)

    @staticmethod
    def is_qwen_model_key(model_key: Optional[str]) -> bool:
        normalized_key = str(model_key or "").lower()
        return (
            "qwen3.5-plus" in normalized_key
            or "qwen3.5-397b-a17b" in normalized_key
        )

    @staticmethod
    def is_deepseek_model_key(model_key: Optional[str]) -> bool:
        return "deepseek" in str(model_key or "").strip().lower()

    @staticmethod
    def _optional_env_flag(name: str) -> Optional[bool]:
        value = os.environ.get(name)
        if value is None:
            return None
        return str(value).strip().lower() in {"1", "true", "yes", "on"}

    def should_exclude_tool_calls(self, model_key: Optional[str]) -> bool:
        """Return whether orchestrator memory should drop assistant tool calls.

        DeepSeek quality mode keeps complete tool_call/tool_result pairs by
        default because OpenAI-compatible reasoning models are sensitive to
        orphan-looking tool results in replayed history.  Keep the legacy
        behavior for other model families and provide an env override for safe
        rollback or targeted A/B tests.
        """
        override = self._optional_env_flag("AGENTSCOPE_EXCLUDE_TOOL_CALLS")
        if override is not None:
            return override
        return not self.is_deepseek_model_key(model_key)

    def resolve_tool_memory_policy(
        self,
        model_key: Optional[str],
    ) -> ToolCallMemoryPolicy:
        key = (model_key or "").lower()
        openai_compatible_tokens = (
            "glm",
            "kimi",
            "claude",
            "openai",
            "proxy",
            "mimo",
            "minimax",
            "qwen",
            "volcengine",
            "doubao",
        )

        if "gemini" in key:
            return ToolCallMemoryPolicy(
                api_format="gemini_like",
                keep_tool_results=True,
                drop_tool_call_only=True,
            )

        if any(token in key for token in openai_compatible_tokens):
            return ToolCallMemoryPolicy(
                api_format="openai",
                keep_tool_results=True,
                drop_tool_call_only=False,
            )

        return ToolCallMemoryPolicy(
            api_format="openai",
            keep_tool_results=True,
            drop_tool_call_only=False,
        )

    @staticmethod
    def uses_openai_memory_policy(policy: Optional[ToolCallMemoryPolicy]) -> bool:
        return bool(policy and policy.api_format == "openai")

    def build_orchestrator_memory_kwargs(
        self,
        *,
        thread_id: str,
        project_id: str,
        model_key: Optional[str],
    ) -> dict[str, Any]:
        policy = self.resolve_tool_memory_policy(model_key)
        return {
            "thread_id": thread_id,
            "project_id": project_id,
            "exclude_tool_calls": self.should_exclude_tool_calls(model_key),
            "keep_tool_results": policy.keep_tool_results,
            "drop_tool_call_only": policy.drop_tool_call_only,
            "retain_complete_tool_runs": self.settings.retain_complete_tool_runs,
            "enable_tool_history_summary": self.settings.enable_tool_history_summary,
            "tool_history_summary_max_runs": self.settings.tool_history_summary_max_runs,
            "tool_history_summary_max_chars": self.settings.tool_history_summary_max_chars,
            "kv_cache_canonical_json": self.settings.kv_cache_canonical_json,
            "kv_cache_contract_version": self.settings.kv_cache_contract_version,
            "enable_micro_compact": self.settings.enable_micro_compact,
            "enable_tail_trim": self.settings.enable_tail_trim,
            "tail_trim_keep_turns": self.settings.tail_trim_keep_turns,
        }

    def build_shadow_clone_tail_memory_kwargs(
        self,
        *,
        thread_id: str,
        project_id: str,
        thread_run_id: str,
        model_key: Optional[str],
    ) -> dict[str, Any]:
        """Build a slimmer orchestrator memory profile for post-subagent continuation."""
        kwargs = self.build_orchestrator_memory_kwargs(
            thread_id=thread_id,
            project_id=project_id,
            model_key=model_key,
        )
        kwargs["thread_run_id"] = thread_run_id
        kwargs["keep_tool_results"] = False
        kwargs["drop_tool_call_only"] = True
        kwargs["retain_complete_tool_runs"] = 0
        kwargs["enable_tool_history_summary"] = False
        return kwargs

    def build_worker_memory_kwargs(
        self,
        *,
        thread_id: str,
        project_id: str,
        thread_run_id: str,
        model_key: Optional[str],
    ) -> dict[str, Any]:
        policy = self.resolve_tool_memory_policy(model_key)
        return {
            "thread_id": thread_id,
            "project_id": project_id,
            "thread_run_id": thread_run_id,
            "exclude_tool_calls": False,
            "drop_tool_call_only": policy.drop_tool_call_only,
            "retain_complete_tool_runs": self.settings.retain_complete_tool_runs,
            "enable_tool_history_summary": self.settings.enable_tool_history_summary,
            "enable_micro_compact": self.settings.enable_micro_compact,
            "enable_tail_trim": self.settings.enable_tail_trim,
            "tail_trim_keep_turns": self.settings.tail_trim_keep_turns,
            "tool_history_summary_max_runs": self.settings.tool_history_summary_max_runs,
            "tool_history_summary_max_chars": self.settings.tool_history_summary_max_chars,
            "kv_cache_canonical_json": self.settings.kv_cache_canonical_json,
            "kv_cache_contract_version": self.settings.kv_cache_contract_version,
        }
