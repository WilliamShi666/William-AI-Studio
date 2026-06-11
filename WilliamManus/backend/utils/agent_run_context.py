import contextvars
from typing import Optional, Tuple

_agent_run_id_ctx: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "agent_run_id", default=None
)
_thread_id_ctx: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "thread_id", default=None
)
_model_name_ctx: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar(
    "model_name", default=None
)
_execution_epoch_ctx: contextvars.ContextVar[Optional[int]] = contextvars.ContextVar(
    "execution_epoch",
    default=None,
)


def set_agent_run_context(
    agent_run_id: Optional[str],
    thread_id: Optional[str],
    model_name: Optional[str] = None,
    current_execution_epoch: Optional[int] = None,
) -> None:
    """Set agent run context for downstream tool execution."""
    _agent_run_id_ctx.set(agent_run_id)
    _thread_id_ctx.set(thread_id)
    _model_name_ctx.set(model_name)
    _execution_epoch_ctx.set(current_execution_epoch)


def get_agent_run_context() -> Tuple[Optional[str], Optional[str]]:
    """Get agent run context values (agent_run_id, thread_id)."""
    return _agent_run_id_ctx.get(), _thread_id_ctx.get()


def get_agent_run_execution_context() -> Tuple[Optional[str], Optional[str], Optional[int]]:
    """Get agent run context values including the current execution epoch."""
    return (
        _agent_run_id_ctx.get(),
        _thread_id_ctx.get(),
        _execution_epoch_ctx.get(),
    )


def get_agent_model_context() -> Optional[str]:
    """Get the model name currently bound to this run context."""
    return _model_name_ctx.get()


def clear_agent_run_context() -> None:
    """Clear agent run context to avoid stale cross-run writes."""
    _agent_run_id_ctx.set(None)
    _thread_id_ctx.set(None)
    _model_name_ctx.set(None)
    _execution_epoch_ctx.set(None)
