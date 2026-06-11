"""Types for post-run long-term memory review."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class RunMetrics:
    """Aggregated metrics extracted from one run response stream."""

    total_tool_calls: int = 0
    tool_failures: int = 0
    max_consecutive_failures: int = 0
    tool_error_details: list[dict] = field(default_factory=list)
    total_responses: int = 0
    final_status: str = "running"
    duration_seconds: float = 0.0


@dataclass(frozen=True)
class ReviewSettings:
    """Runtime settings for post-run review."""

    enabled: bool = False
    use_run_model: bool = True
    tool_failure_threshold: int = 3
    min_responses: int = 5
    confidence_threshold: float = 0.65
    max_context_chars: int = 15000
    review_timeout_seconds: float = 60.0
    model_name: str = "minimax-m2.5"
    lock_ttl_seconds: int = 86400
    force_record_on_high_friction: bool = False


@dataclass
class ReviewResult:
    """Structured review output from review model parsing."""

    should_record: bool = False
    confidence: float = 0.0
    task_memories: list[str] = field(default_factory=list)
    tool_memories: list[str] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)
    raw_response: str = ""
