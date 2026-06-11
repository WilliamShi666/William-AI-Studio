"""Redis key builders for Shadow Clone V2.

V2 intentionally uses an `sc_v2:` namespace so it never collides with the
existing V1 `shadow_clone:` keys.
"""

from __future__ import annotations

V2_REDIS_PREFIX = "sc_v2"
_VALID_MAILBOX_LANES = frozenset({"control", "text", "deadletter"})


def _required(value: str, name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{name} is required")
    if any(separator in normalized for separator in (" ", "\n", "\r", "\t")):
        raise ValueError(f"{name} must not contain whitespace")
    return normalized


def run_events_key(run_id: str) -> str:
    run = _required(run_id, "run_id")
    return f"{V2_REDIS_PREFIX}:run:{run}:events"


def thread_team_key(
    *,
    project_id: str,
    thread_id: str,
    account_id: str | None = None,
) -> str:
    project = _required(project_id, "project_id")
    thread = _required(thread_id, "thread_id")
    account = str(account_id or "").strip()
    if account:
        return (
            f"{V2_REDIS_PREFIX}:account:{_required(account, 'account_id')}:"
            f"project:{project}:thread:{thread}:team"
        )
    return f"{V2_REDIS_PREFIX}:project:{project}:thread:{thread}:team"


def agent_mailbox_key(
    *,
    project_id: str,
    thread_id: str,
    account_id: str | None = None,
    agent_name: str,
    lane: str,
) -> str:
    project = _required(project_id, "project_id")
    thread = _required(thread_id, "thread_id")
    agent = _required(agent_name, "agent_name")
    normalized_lane = _required(lane, "lane")
    if normalized_lane not in _VALID_MAILBOX_LANES:
        allowed = ", ".join(sorted(_VALID_MAILBOX_LANES))
        raise ValueError(f"lane must be one of: {allowed}")
    account = str(account_id or "").strip()
    if account:
        return (
            f"{V2_REDIS_PREFIX}:account:{_required(account, 'account_id')}:"
            f"project:{project}:thread:{thread}:"
            f"mailbox:{agent}:{normalized_lane}"
        )
    return (
        f"{V2_REDIS_PREFIX}:project:{project}:thread:{thread}:"
        f"mailbox:{agent}:{normalized_lane}"
    )


def task_key(*, run_id: str, task_id: str) -> str:
    run = _required(run_id, "run_id")
    task = _required(task_id, "task_id")
    return f"{V2_REDIS_PREFIX}:run:{run}:task:{task}"
