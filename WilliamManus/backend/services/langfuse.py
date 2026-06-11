import os
import re
import hashlib
from langfuse import Langfuse
from typing import Iterable

from utils.logger import logger

public_key = os.getenv("LANGFUSE_PUBLIC_KEY")
secret_key = os.getenv("LANGFUSE_SECRET_KEY")
host = os.getenv("LANGFUSE_HOST", "https://cloud.langfuse.com")

_env_raw = os.getenv("LANGFUSE_TRACING_ENVIRONMENT", "default")
_env_pattern = re.compile(r"^(?!langfuse)[a-z0-9_-]{1,40}$")
if _env_pattern.match(_env_raw):
    environment = _env_raw
else:
    logger.warning(
        "[langfuse] LANGFUSE_TRACING_ENVIRONMENT=%r is invalid (must match ^(?!langfuse)[a-z0-9_-]{1,40}$); "
        "falling back to 'default'",
        _env_raw,
    )
    environment = "default"

# 检查是否有有效的配置
enabled = bool(public_key and secret_key)

# 根据 langfuse 版本使用不同的初始化方式
try:
    if enabled:
        langfuse = Langfuse(
            public_key=public_key,
            secret_key=secret_key,
            host=host,
            environment=environment,
        )
    else:
        # 如果没有配置，创建一个禁用的实例
        langfuse = Langfuse(
            public_key="disabled",
            secret_key="disabled",
            host=host,
            environment=environment,
        )
except TypeError:
    # 如果上面的方式失败，尝试旧版本的初始化方式
    try:
        langfuse = Langfuse(enabled=enabled)
    except TypeError:
        # 如果都失败，创建一个简单的占位符
        class DisabledLangfuse:
            def trace(self, *args, **kwargs):
                return self
            def span(self, *args, **kwargs):
                return self
            def start_span(self, *args, **kwargs):
                return self
            def start_observation(self, *args, **kwargs):
                return self
            def start_generation(self, *args, **kwargs):
                return self
            def create_event(self, *args, **kwargs):
                return None
            def update(self, *args, **kwargs):
                return self
            def update_trace(self, *args, **kwargs):
                return self
            def end(self, *args, **kwargs):
                pass
            def flush(self, *args, **kwargs):
                pass
            def get_trace_url(self, *args, **kwargs):
                return "(disabled)"
            def score(self, *args, **kwargs):
                return None
            def create_score(self, *args, **kwargs):
                return None
        langfuse = DisabledLangfuse()


class NoOpLangfuseTrace:
    def update(self, *args, **kwargs):
        return self

    def update_trace(self, *args, **kwargs):
        return self

    def span(self, *args, **kwargs):
        return self

    def start_observation(self, *args, **kwargs):
        return self

    def start_generation(self, *args, **kwargs):
        return self

    def create_event(self, *args, **kwargs):
        return self

    def end(self, *args, **kwargs):
        return None


def generate_trace_id(seed: str) -> str:
    """
    Return a Langfuse-compatible trace id.

    Langfuse v3 expects a 32-char hex OTEL trace id. Some client versions expose
    `create_trace_id`, some do not. We always return a stable 32-char lowercase
    hex string so tracing setup never blocks the main execution path.
    """
    raw_seed = str(seed or "").strip()
    if not raw_seed:
        raise ValueError("seed is required")

    if len(raw_seed) == 32 and all(c in "0123456789abcdef" for c in raw_seed.lower()):
        return raw_seed.lower()

    create_trace_id = getattr(langfuse, "create_trace_id", None)
    if callable(create_trace_id):
        try:
            generated = str(create_trace_id(seed=raw_seed) or "").strip()
            if len(generated) == 32 and all(
                c in "0123456789abcdef" for c in generated.lower()
            ):
                return generated.lower()
        except Exception as exc:
            logger.warning("[langfuse] create_trace_id failed for %r: %s", raw_seed, exc)

    return hashlib.md5(raw_seed.encode("utf-8"), usedforsecurity=False).hexdigest()


def start_root_span(*, name: str, trace_id: str):
    """
    Start a root Langfuse span if supported, otherwise return a no-op trace.
    """
    start_span = getattr(langfuse, "start_span", None)
    if not callable(start_span):
        return NoOpLangfuseTrace()
    try:
        return start_span(name=name, trace_context={"trace_id": trace_id})
    except Exception as exc:
        logger.warning("[langfuse] start_span failed for trace_id=%s: %s", trace_id, exc)
        return NoOpLangfuseTrace()


def get_trace_url(trace_id: str) -> str:
    getter = getattr(langfuse, "get_trace_url", None)
    if not callable(getter):
        return "(unavailable)"
    try:
        return str(getter(trace_id=trace_id))
    except Exception:
        return "(unavailable)"


# ── Score writing helpers ───────────────────────────────────────────────

SCORE_NUMERIC = "NUMERIC"

# Source of truth for all Phase 1 + v4 scores. min/max used by bootstrap_langfuse_score_configs.py.
# Infra monitoring (io_token_ratio, kv_cache_hit_rate, etc.) lives in trace metadata, not here.
# pass_at_3 / pass_pow_3 are dataset-run-level, not per-trace scores.
PHASE1_SCORE_REGISTRY: dict[str, dict] = {
    # Token / latency / cost
    "total_tokens":                 {"min": 0, "max": 10_000_000},
    "ttft_seconds":                 {"min": 0, "max": 600},
    "e2e_duration_seconds":         {"min": 0, "max": 7200},
    "estimated_cost_usd":           {"min": 0, "max": 1000},
    "iteration_count":              {"min": 0, "max": 10_000},
    # Outcome / quality (0..1 normalized)
    "code_exec_success_rate":       {"min": 0, "max": 1},
    "task_completed":               {"min": 0, "max": 1},   # boolean-as-numeric
    "deliverable_quality":          {"min": 0, "max": 5},
    "first_try_success_rate":       {"min": 0, "max": 1},
    "waste_ratio":                  {"min": 0, "max": 1},
    "convergence_speed":            {"min": 0, "max": 100},
    # Composite dimension scores
    "o_score":                      {"min": 0, "max": 100},
    "b_score":                      {"min": 0, "max": 100},
    # q_score placeholder — not written by metrics_collector yet; Q1/Q2/Q6/Q7/Q8 still manual
    "q_score":                      {"min": 0, "max": 100},
    # Performance-per-cost ratios
    "quality_per_dollar":           {"min": 0, "max": 1_000_000},
    "quality_per_ktoken":           {"min": 0, "max": 1_000_000},
    "quality_per_iteration":        {"min": 0, "max": 1_000_000},
    # v4 quality dimension — registered so annotation queue / LLM Judge / Managed Eval can write later
    "outcome_verified":             {"min": 0, "max": 1},   # sandbox state-check via outcome_verifier.py
    "groundedness":                 {"min": 0, "max": 1},   # Langfuse Managed Eval (Hallucination)
    "tool_selection_accuracy":      {"min": 0, "max": 1},   # manual / LLM Judge
    "tool_parameter_accuracy":      {"min": 0, "max": 1},   # manual / LLM Judge
    "error_recovery_success":       {"min": 0, "max": 1},   # manual annotation
    # Safety (boolean-as-numeric; hard gate, not part of weighted score)
    "tool_authorization_violation": {"min": 0, "max": 1},
    "toxic_output_detected":        {"min": 0, "max": 1},
}
PHASE1_SCORE_NAMES = frozenset(PHASE1_SCORE_REGISTRY.keys())  # back-compat alias


def _validate_score_name(name: str) -> str:
    normalized = str(name or "").strip()
    if not normalized:
        raise ValueError("Score name must be a non-empty string")
    return normalized


def _coerce_trace_id(trace_id: str) -> str:
    """
    Langfuse v3 requires a 32-char hex trace_id (OTEL standard).
    Accept either an already-hex trace_id OR an arbitrary caller key (e.g. agent_run_id UUID),
    and deterministically derive a Langfuse trace_id from it.
    """
    raw = str(trace_id or "").strip()
    if not raw:
        raise ValueError("trace_id is required")
    # Already 32 lowercase hex chars? assume it's a Langfuse trace_id and pass through.
    if len(raw) == 32 and all(c in "0123456789abcdef" for c in raw.lower()):
        return raw.lower()
    return generate_trace_id(raw)


def _build_score_kwargs(
    trace_id: str,
    name: str,
    value: float,
    *,
    observation_id: str | None = None,
    comment: str | None = None,
) -> dict:
    coerced_trace_id = _coerce_trace_id(trace_id)
    normalized_name = _validate_score_name(name)
    score_id = f"{coerced_trace_id}-{normalized_name}"
    if observation_id:
        score_id += f"-{observation_id}"
    kwargs: dict = {
        "score_id": score_id,
        "trace_id": coerced_trace_id,
        "name": normalized_name,
        "value": value,
        "data_type": SCORE_NUMERIC,
    }
    if observation_id:
        kwargs["observation_id"] = observation_id
    if comment:
        kwargs["comment"] = comment
    return kwargs

def score_numeric(
    trace_id: str,
    name: str,
    value: float,
    *,
    observation_id: str | None = None,
    comment: str | None = None,
) -> None:
    """Write a numeric score to a trace or observation. Fails silently."""
    if not enabled:
        return
    try:
        kwargs = _build_score_kwargs(
            trace_id,
            name,
            value,
            observation_id=observation_id,
            comment=comment,
        )
        langfuse.create_score(**kwargs)
    except Exception as e:
        logger.warning("[langfuse] Failed to write numeric score %s: %s", name, e)


def score_boolean(
    trace_id: str,
    name: str,
    value: bool,
    *,
    observation_id: str | None = None,
    comment: str | None = None,
) -> None:
    """Write a boolean score to a trace or observation. Fails silently."""
    if not enabled:
        return
    try:
        kwargs = _build_score_kwargs(
            trace_id,
            name,
            1 if value else 0,
            observation_id=observation_id,
            comment=comment,
        )
        langfuse.create_score(**kwargs)
    except Exception as e:
        logger.warning("[langfuse] Failed to write boolean score %s: %s", name, e)


def score_many_numeric(
    trace_id: str,
    scores: dict[str, float | int],
    *,
    observation_id: str | None = None,
) -> None:
    """Write a batch of numeric scores to a trace or observation."""
    for name, value in scores.items():
        score_numeric(
            trace_id,
            name,
            float(value),
            observation_id=observation_id,
        )


def score_many_boolean(
    trace_id: str,
    scores: dict[str, bool],
    *,
    observation_id: str | None = None,
) -> None:
    """Write a batch of boolean-as-numeric scores to a trace or observation."""
    for name, value in scores.items():
        score_boolean(
            trace_id,
            name,
            bool(value),
            observation_id=observation_id,
        )


def filter_registered_scores(
    scores: dict[str, float | int | bool | None],
    *,
    allowed_names: Iterable[str] = PHASE1_SCORE_NAMES,
) -> dict[str, float | int | bool]:
    """Drop None values and names outside the current score registry."""
    allowed = {str(name) for name in allowed_names}
    filtered: dict[str, float | int | bool] = {}
    for name, value in scores.items():
        if value is None:
            continue
        normalized = str(name or "").strip()
        if normalized not in allowed:
            continue
        filtered[normalized] = value
    return filtered
