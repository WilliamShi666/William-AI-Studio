"""Shared sandbox session state for Shadow Clone runs.

This module gives all tool instances in the same process a single runtime
handle for one Shadow Clone run. It does not perform any provider I/O by
itself; it only centralizes ownership of the live sandbox object and the
associated metadata so tools stop maintaining fragmented stale handles.
"""

from __future__ import annotations

import asyncio
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


@dataclass
class ShadowCloneSandboxSessionState:
    """Process-local shared runtime state for one Shadow Clone run."""

    project_id: str
    run_id: str
    sandbox_type: str = "desktop"
    sandbox: Any = None
    sandbox_id: Optional[str] = None
    sandbox_pass: Optional[str] = None
    ensure_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_bound_at: float = 0.0

    def bind(
        self,
        *,
        sandbox: Any | None = None,
        sandbox_id: Optional[str] = None,
        sandbox_pass: Optional[str] = None,
        sandbox_type: Optional[str] = None,
    ) -> None:
        if sandbox is not None:
            self.sandbox = sandbox
        if sandbox_id is not None:
            self.sandbox_id = str(sandbox_id).strip() or None
        if sandbox_pass is not None:
            self.sandbox_pass = str(sandbox_pass)
        if sandbox_type:
            self.sandbox_type = str(sandbox_type).strip() or self.sandbox_type
        self.last_bound_at = time.monotonic()

    def clear_runtime_handle(self) -> None:
        self.sandbox = None


_shadow_clone_session_states: Dict[str, ShadowCloneSandboxSessionState] = {}
_shadow_clone_session_states_lock = threading.Lock()


def get_or_create_shadow_clone_session_state(
    *,
    project_id: str,
    run_id: str,
    sandbox_type: str = "desktop",
) -> ShadowCloneSandboxSessionState:
    run_key = str(run_id or "").strip()
    if not run_key:
        raise ValueError("run_id is required for Shadow Clone session state")

    with _shadow_clone_session_states_lock:
        state = _shadow_clone_session_states.get(run_key)
        if state is None:
            state = ShadowCloneSandboxSessionState(
                project_id=str(project_id or "").strip(),
                run_id=run_key,
                sandbox_type=str(sandbox_type or "desktop").strip() or "desktop",
            )
            _shadow_clone_session_states[run_key] = state
            return state

        if project_id:
            state.project_id = str(project_id).strip() or state.project_id
        if sandbox_type:
            state.sandbox_type = str(sandbox_type).strip() or state.sandbox_type
        return state


def get_shadow_clone_session_state(run_id: str) -> Optional[ShadowCloneSandboxSessionState]:
    run_key = str(run_id or "").strip()
    if not run_key:
        return None
    with _shadow_clone_session_states_lock:
        return _shadow_clone_session_states.get(run_key)


def clear_shadow_clone_session_state(run_id: str) -> None:
    run_key = str(run_id or "").strip()
    if not run_key:
        return
    with _shadow_clone_session_states_lock:
        _shadow_clone_session_states.pop(run_key, None)
