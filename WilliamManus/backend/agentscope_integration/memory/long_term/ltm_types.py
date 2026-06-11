"""Types and settings for AgentScope long-term memory integration."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Tuple


VALID_LTM_CONTROL_MODES = {"agent_control", "static_control", "both"}
DEFAULT_DASHSCOPE_COMPAT_BASE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1"


@dataclass(frozen=True)
class LTMSettings:
    """Runtime settings for long-term memory wiring."""

    enabled: bool = False
    control_mode: str = "both"
    attach_scope: str = "orchestrator"
    memories: Tuple[str, ...] = ("task", "tool")
    fail_open: bool = True
    retrieve_limit: int = 5
    write_gate_enabled: bool = True
    static_record_enabled: bool = False
    model_name: str = "qwen3.5-35b-a3b"
    embedding_model_name: str = "text-embedding-v4"
    rerank_model_name: str = "qwen3-vl-rerank"
    global_workspace: str = "global_task_tool_v1"
    api_key: str = ""
    api_base: str = DEFAULT_DASHSCOPE_COMPAT_BASE_URL
    embedding_dimensions: int = 1024
    reme_config_path: str = ""
    reme_kwargs: dict = field(default_factory=dict)
    task_query_mode: str = "merged"
    task_query_keyword_limit: int = 8

    def is_valid_control_mode(self) -> bool:
        return self.control_mode in VALID_LTM_CONTROL_MODES

    def has_memory(self, memory_name: str) -> bool:
        return memory_name in set(self.memories)


def normalize_memories(raw: str, default: Iterable[str] = ("task", "tool")) -> Tuple[str, ...]:
    """Parse and normalize AGENTSCOPE_LTM_MEMORIES."""

    if not raw:
        return tuple(default)

    normalized = []
    for token in str(raw).split(","):
        value = token.strip().lower()
        if not value:
            continue
        if value not in {"task", "tool", "personal"}:
            continue
        if value not in normalized:
            normalized.append(value)

    return tuple(normalized) if normalized else tuple(default)
