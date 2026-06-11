"""Task executor seam for Shadow Clone V2 agent turns."""

from __future__ import annotations

from dataclasses import dataclass
import inspect
import json
import re
from typing import Any, Awaitable, Callable, Protocol

from . import event_log
from .models import AgentIdentity, EventType, Task

V2_MANAGEMENT_TOOL_NAMES = frozenset(
    {
        "team_create",
        "team_update",
        "team_delete",
        "team_shutdown",
        "task_create",
        "task_get",
        "task_update",
        "task_list",
        "send_message",
        "message_read",
        "message_ack",
        "message_dead_letter",
    }
)
TOOL_ARGUMENT_SUMMARY_MAX_CHARS = 500
TOOL_ARGUMENT_SUMMARY_MAX_ITEMS = 20
SENSITIVE_TOOL_ARGUMENT_KEYS = frozenset(
    {
        "authorization",
        "api_key",
        "apikey",
        "access_token",
        "refresh_token",
        "token",
        "cookie",
        "cookies",
        "secret",
        "password",
        "passwd",
        "private_key",
    }
)


@dataclass(frozen=True)
class ShadowCloneV2RunContext:
    """Context required by a V2 task executor."""

    thread_id: str
    project_id: str
    agent_run_id: str
    model_key: str
    thread_run_id: str | None = None
    account_id: str | None = None
    subagent_model: str | None = None
    db_client: object | None = None
    trace: object | None = None
    tool_sequence_start: int = 0


@dataclass(frozen=True)
class ShadowCloneV2TaskExecutionResult:
    """Output and sequence cursor returned by a V2 task executor."""

    output: str
    next_sequence: int | None = None


class ShadowCloneV2TaskExecutionError(RuntimeError):
    """Worker failure that carries the latest V2 tool event sequence."""

    def __init__(self, message: str, *, next_sequence: int | None = None) -> None:
        super().__init__(message)
        self.next_sequence = next_sequence


class ShadowCloneV2TaskExecutor(Protocol):
    """Protocol implemented by concrete V2 task executors."""

    async def execute(
        self,
        *,
        task: Task,
        agent: AgentIdentity,
        run_context: ShadowCloneV2RunContext,
        inbox_messages: list[Any] | None = None,
        on_stream_activity: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> str:
        """Execute one task for one agent and return a result reference/summary."""


WorkerFactory = Callable[..., Any]


def _select_worker_model(run_context: ShadowCloneV2RunContext) -> str:
    """Choose the model key for a V2 worker turn."""
    subagent_model = str(run_context.subagent_model or "").strip()
    if subagent_model:
        return subagent_model
    return str(run_context.model_key or "").strip()


def _message_field(message: Any, field_name: str) -> Any:
    if isinstance(message, dict):
        return message.get(field_name)
    return getattr(message, field_name, None)


def _format_inbox_messages(inbox_messages: list[Any] | None) -> list[str]:
    formatted: list[str] = []
    for index, message in enumerate(inbox_messages or [], start=1):
        sender = str(_message_field(message, "sender") or "unknown").strip()
        summary = str(_message_field(message, "summary") or "").strip()
        text = str(_message_field(message, "text") or "").strip()
        message_id = str(_message_field(message, "id") or _message_field(message, "message_id") or "").strip()
        payload = _message_field(message, "payload")
        payload_text = ""
        if payload:
            try:
                payload_text = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            except (TypeError, ValueError):
                payload_text = str(payload)
        parts = [
            f"Message {index}:",
            f"- id: {message_id or 'unknown'}",
            f"- from: {sender}",
        ]
        if summary:
            parts.append(f"- summary: {summary}")
        if text:
            parts.append(f"- text: {text}")
        if payload_text:
            parts.append(f"- payload: {payload_text}")
        formatted.append("\n".join(parts))
    return formatted


def _build_worker_task_prompt(
    *,
    task: Task,
    agent: AgentIdentity,
    inbox_messages: list[Any] | None = None,
) -> str:
    """Build the user message sent to the concrete AgentScope worker."""
    sections = [
        "## Shadow Clone V2 Worker Turn",
        f"Agent name: {agent.agent_name}",
        f"Agent role: {agent.role}",
        f"Task id: {task.id}",
        f"Task subject: {task.subject}",
        "",
        "## Assigned task",
        task.description,
        "",
        "## Coordination tools",
        "Use send_message for explicit peer coordination. For a broadcast, call send_message with recipient=\"*\" and message_type=\"broadcast\"; facilitator-origin broadcast reaches every teammate, and teammate-origin broadcast reaches every other teammate plus the facilitator. Use broadcast only when every applicable participant must receive the same instruction.",
        "",
        "## Realtime visible progress",
        "Before calling any tool, first write one short assistant-visible natural-language progress line for the user. This must be normal assistant text, not hidden reasoning, not a tool call, and not a system progress log.",
        "If the task asks you to start by outputting a marker such as SUB_STREAM_MARKER, include that complete marker in this first assistant-visible progress line before any tool call. Do not leave the marker only in hidden reasoning or only in the final completion summary.",
    ]
    formatted_messages = _format_inbox_messages(inbox_messages)
    if formatted_messages:
        sections.extend(
            [
                "",
                "## Inbox messages",
                "Use these durable peer/main-agent messages when they are relevant to the assigned task. If a message changes the required output, reflect that change explicitly.",
                "",
                *formatted_messages,
            ]
        )
    sections.extend(
        [
            "",
            "Stay scoped to this task. Return a concise completion summary and "
            "reference any created `/workspace/...` artifacts.",
        ]
    )
    return "\n".join(sections)


def _worker_stream_text(msg: Any) -> str:
    get_text_content = getattr(msg, "get_text_content", None)
    if callable(get_text_content):
        try:
            text = get_text_content()
            if isinstance(text, str):
                return text
        except Exception:
            pass
    content = getattr(msg, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                parts.append(block["text"])
        return "".join(parts)
    return ""


def _build_worker_stream_activity(
    *,
    task: Task,
    agent: AgentIdentity,
    run_context: ShadowCloneV2RunContext,
    msg: Any,
    is_last: bool,
    sequence: int,
) -> dict[str, Any] | None:
    if str(getattr(msg, "role", "") or "").strip() != "assistant":
        return None
    text = _worker_stream_text(msg).strip()
    if not text:
        return None
    return {
        "type": "subagent_activity",
        "status": "running",
        "source": "shadow_clone_v2",
        "shadow_clone_mode": "v2",
        "thread_run_id": run_context.thread_run_id or run_context.agent_run_id,
        "agent_run_id": run_context.agent_run_id,
        "ui_phase": "subagents_running",
        "activity_owner": "shadow_clone",
        "phase_reason": "shadow_clone_v2_subagent_activity",
        "subtask_id": task.id,
        "sequence": sequence,
        "role": task.subject or agent.role,
        "agent_name": agent.agent_name,
        "message_type": "assistant",
        "content": {
            "role": "assistant",
            "content": text,
        },
        "metadata": {
            "stream_status": "complete" if is_last else "chunk",
            "source": "shadow_clone_v2",
            "subtask_id": task.id,
            "agent_name": agent.agent_name,
        },
    }


_COMPLETE_SUB_STREAM_MARKER_RE = re.compile(
    r"\bSUB_STREAM_MARKER_[A-Za-z0-9_-]+_\d+\b"
)
_SUB_STREAM_MARKER_PARTS_RE = re.compile(
    r"\[\s*[\"']SUB_STREAM_MARKER[\"']\s*,\s*[\"']([^\"']+)[\"']\s*,\s*[\"']([^\"']+)[\"']\s*\]"
)
_SUB_STREAM_MARKER_FRAGMENT_RE = re.compile(
    r"Fragment\s+([ABC])\s*[:=]\s*(?:the\s+literal\s+string\s*)?"
    r"[`\"']([^`\"']+)[`\"']",
    re.IGNORECASE,
)
_SAFE_SUB_STREAM_MARKER_PART_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _extract_fragment_style_sub_stream_marker(description: str) -> str | None:
    fragments: dict[str, str] = {}
    for match in _SUB_STREAM_MARKER_FRAGMENT_RE.finditer(description):
        fragments[match.group(1).upper()] = match.group(2).strip()
    if (
        fragments.get("A") != "SUB_STREAM_MARKER"
        or not fragments.get("B")
        or not fragments.get("C")
    ):
        return None
    if not (
        _SAFE_SUB_STREAM_MARKER_PART_RE.fullmatch(fragments["B"])
        and _SAFE_SUB_STREAM_MARKER_PART_RE.fullmatch(fragments["C"])
    ):
        return None
    return f"SUB_STREAM_MARKER_{fragments['B']}_{fragments['C']}"


def _extract_visible_start_marker(task: Task) -> str | None:
    """Extract a deterministic user-requested subagent start marker from task text."""
    description = str(getattr(task, "description", "") or "")
    complete_match = _COMPLETE_SUB_STREAM_MARKER_RE.search(description)
    if complete_match:
        return complete_match.group(0)
    parts_match = _SUB_STREAM_MARKER_PARTS_RE.search(description)
    if parts_match:
        return f"SUB_STREAM_MARKER_{parts_match.group(1)}_{parts_match.group(2)}"
    fragment_marker = _extract_fragment_style_sub_stream_marker(description)
    if fragment_marker:
        return fragment_marker
    return None


def _build_worker_opening_progress_activity(
    *,
    task: Task,
    agent: AgentIdentity,
    run_context: ShadowCloneV2RunContext,
    sequence: int,
) -> dict[str, Any]:
    """Build deterministic assistant-visible worker progress before the first tool."""
    marker = _extract_visible_start_marker(task)
    subject = str(getattr(task, "subject", "") or "assigned task").strip()
    if marker:
        text = (
            f"{agent.agent_name} is starting {subject}. "
            f"{marker}"
        )
    else:
        text = f"{agent.agent_name} is starting {subject} and will report progress before using tools."
    return {
        "type": "subagent_activity",
        "status": "running",
        "source": "shadow_clone_v2",
        "shadow_clone_mode": "v2",
        "thread_run_id": run_context.thread_run_id or run_context.agent_run_id,
        "agent_run_id": run_context.agent_run_id,
        "ui_phase": "subagents_running",
        "activity_owner": "shadow_clone",
        "phase_reason": "shadow_clone_v2_subagent_activity",
        "subtask_id": task.id,
        "sequence": sequence,
        "role": task.subject or agent.role,
        "agent_name": agent.agent_name,
        "message_type": "assistant",
        "content": {
            "role": "assistant",
            "content": text,
        },
        "metadata": {
            "stream_status": "chunk",
            "source": "shadow_clone_v2",
            "subtask_id": task.id,
            "agent_name": agent.agent_name,
            "opening_progress": True,
        },
    }


def _response_to_text(response: Any) -> str:
    """Extract text from an AgentScope response-like object."""
    if response is None:
        return ""
    get_text_content = getattr(response, "get_text_content", None)
    if callable(get_text_content):
        try:
            text = get_text_content()
            if isinstance(text, str):
                return text
        except Exception:
            pass

    content = getattr(response, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                chunks.append(str(block.get("text") or ""))
            elif hasattr(block, "text"):
                chunks.append(str(getattr(block, "text", "")))
        return "\n".join(chunk for chunk in chunks if chunk)
    return str(content or response)


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _tool_call_id(tool_call: Any, *, fallback: str) -> str:
    if isinstance(tool_call, dict):
        return str(tool_call.get("id") or tool_call.get("tool_call_id") or fallback)
    return str(getattr(tool_call, "id", "") or fallback)


def _tool_call_name(tool_call: Any) -> str:
    if isinstance(tool_call, dict):
        return str(tool_call.get("name") or tool_call.get("tool_name") or "").strip()
    return str(getattr(tool_call, "name", "") or "").strip()


def _tool_call_arguments(tool_call: Any) -> Any:
    if isinstance(tool_call, dict):
        if "input" in tool_call:
            return tool_call.get("input")
        if "arguments" in tool_call:
            return tool_call.get("arguments")
        function_payload = tool_call.get("function")
        if isinstance(function_payload, dict):
            return function_payload.get("arguments")
    return getattr(tool_call, "input", None) or getattr(tool_call, "arguments", None)


_SENSITIVE_RESULT_KEY_RE = re.compile(
    r"(?i)^(token|password|secret|api[_-]?key|authorization|cookie|access[_-]?token|refresh[_-]?token)$"
)
_SENSITIVE_RESULT_ASSIGNMENT_RE = re.compile(
    r"(?i)\b(token|password|secret|api[_-]?key|authorization|cookie|access[_-]?token|refresh[_-]?token)\s*=\s*([^\s,;]+)"
)
_SENSITIVE_RESULT_COLON_RE = re.compile(
    r"(?i)\b(token|password|secret|api[_-]?key|access[_-]?token|refresh[_-]?token)\s*:\s*([^\s,;]+)"
)
_SENSITIVE_RESULT_AUTHORIZATION_RE = re.compile(
    r"(?i)\bauthorization\s*:\s*Bearer\s+([^\s,;]+)"
)
_SENSITIVE_RESULT_COOKIE_RE = re.compile(
    r"(?i)\bcookie\s*:\s*([^\s,;]+)"
)
_SENSITIVE_RESULT_QUOTED_RE = re.compile(
    r"(?i)([\"'](?:token|password|secret|api[_-]?key|authorization|cookie|access[_-]?token|refresh[_-]?token)[\"']\s*:\s*[\"'])([^\"']+)([\"'])"
)


def _redact_sensitive_result_value(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[Any, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if _SENSITIVE_RESULT_KEY_RE.match(key_text):
                redacted[key] = "[REDACTED]"
            else:
                redacted[key] = _redact_sensitive_result_value(item)
        return redacted
    if isinstance(value, list):
        return [_redact_sensitive_result_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_sensitive_result_value(item) for item in value)
    if isinstance(value, str):
        return _redact_sensitive_result_text(value)
    return value


def _redact_sensitive_result_text(text: str) -> str:
    try:
        parsed = json.loads(text)
    except Exception:
        parsed = None
    if isinstance(parsed, (dict, list)):
        try:
            return json.dumps(
                _redact_sensitive_result_value(parsed),
                ensure_ascii=False,
                sort_keys=True,
            )
        except Exception:
            pass
    redacted = _SENSITIVE_RESULT_QUOTED_RE.sub(
        lambda match: f"{match.group(1)}[REDACTED]{match.group(3)}",
        text,
    )
    redacted = _SENSITIVE_RESULT_AUTHORIZATION_RE.sub(
        "authorization: Bearer [REDACTED]",
        redacted,
    )
    redacted = _SENSITIVE_RESULT_COOKIE_RE.sub("cookie: [REDACTED]", redacted)
    redacted = _SENSITIVE_RESULT_ASSIGNMENT_RE.sub(
        lambda match: f"{match.group(1)}=[REDACTED]",
        redacted,
    )
    redacted = _SENSITIVE_RESULT_COLON_RE.sub(
        lambda match: f"{match.group(1)}: [REDACTED]",
        redacted,
    )
    return redacted


def _summarize_tool_result(value: Any, *, limit: int = 500) -> str:
    redacted_value = _redact_sensitive_result_value(value)
    if isinstance(redacted_value, str):
        text = redacted_value.strip()
    else:
        try:
            text = json.dumps(redacted_value, ensure_ascii=False, sort_keys=True)
        except Exception:
            text = _redact_sensitive_result_text(str(redacted_value or "").strip())
    if len(text) <= limit:
        return text
    return text[:limit] + "…"


def _json_size_bytes(value: Any) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=False, default=str).encode("utf-8"))
    except Exception:
        return len(str(value).encode("utf-8", errors="replace"))


def _is_sensitive_argument_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return any(sensitive in normalized for sensitive in SENSITIVE_TOOL_ARGUMENT_KEYS)


def _summarize_tool_argument_value(value: Any) -> tuple[Any, bool]:
    if isinstance(value, dict):
        redacted = False
        summary: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= TOOL_ARGUMENT_SUMMARY_MAX_ITEMS:
                summary["__truncated_items__"] = max(0, len(value) - index)
                redacted = True
                break
            key_text = str(key)
            if _is_sensitive_argument_key(key_text):
                summary[key_text] = "[REDACTED]"
                redacted = True
                continue
            summarized_item, item_redacted = _summarize_tool_argument_value(item)
            summary[key_text] = summarized_item
            redacted = redacted or item_redacted
        return summary, redacted
    if isinstance(value, list):
        redacted = False
        summary_items = []
        for item in value[:TOOL_ARGUMENT_SUMMARY_MAX_ITEMS]:
            summarized_item, item_redacted = _summarize_tool_argument_value(item)
            summary_items.append(summarized_item)
            redacted = redacted or item_redacted
        if len(value) > TOOL_ARGUMENT_SUMMARY_MAX_ITEMS:
            summary_items.append(
                {"__truncated_items__": len(value) - TOOL_ARGUMENT_SUMMARY_MAX_ITEMS}
            )
            redacted = True
        return summary_items, redacted
    if isinstance(value, str):
        if len(value) <= TOOL_ARGUMENT_SUMMARY_MAX_CHARS:
            return value, False
        return value[:TOOL_ARGUMENT_SUMMARY_MAX_CHARS] + "…", True
    return value, False


def _build_tool_arguments_event_payload(arguments: Any) -> dict[str, Any]:
    summary, redacted = _summarize_tool_argument_value(arguments)
    return {
        "arguments_summary": summary,
        "arguments_redacted": bool(redacted),
        "arguments_size_bytes": _json_size_bytes(arguments),
    }


def _get_shadow_clone_v2_agent_tools(worker: Any, toolkit: Any) -> Any:
    tool_state = getattr(worker, "_shadow_clone_v2_agent_tools", None)
    if tool_state is None:
        tool_state = getattr(toolkit, "_shadow_clone_v2_agent_tools", None)
    return tool_state


def _sync_tool_state_sequence(tool_state: Any, next_sequence: int) -> int:
    if tool_state is None:
        return next_sequence
    tool_next_sequence = getattr(tool_state, "next_sequence", None)
    if isinstance(tool_next_sequence, int):
        next_sequence = max(next_sequence, tool_next_sequence)
    if hasattr(tool_state, "_next_sequence"):
        try:
            setattr(tool_state, "_next_sequence", next_sequence)
        except Exception:
            pass
    return next_sequence


def _install_worker_tool_event_recorder(
    worker: Any,
    *,
    task: Task,
    agent: AgentIdentity,
    run_context: ShadowCloneV2RunContext,
    has_visible_activity: Callable[[], bool] | None = None,
    on_opening_activity: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
) -> Callable[[], int | None]:
    """Wrap worker toolkit calls so ordinary worker tools become durable V2 events."""
    toolkit = getattr(worker, "toolkit", None)
    if toolkit is None:
        nested_agent = getattr(worker, "agent", None)
        toolkit = getattr(nested_agent, "toolkit", None)
    original_call = getattr(toolkit, "call_tool_function", None)
    if toolkit is None or not callable(original_call):
        return lambda: None

    next_sequence = int(run_context.tool_sequence_start or 0)
    tool_state = _get_shadow_clone_v2_agent_tools(worker, toolkit)

    async def _recording_call_tool_function(tool_call: Any):
        nonlocal next_sequence
        next_sequence = _sync_tool_state_sequence(tool_state, next_sequence)
        call_id = _tool_call_id(tool_call, fallback=f"tool-{next_sequence}")
        tool_name = _tool_call_name(tool_call) or "unknown_tool"
        if tool_name in V2_MANAGEMENT_TOOL_NAMES:
            next_sequence = _sync_tool_state_sequence(tool_state, next_sequence)
            result = await _maybe_await(original_call(tool_call))
            next_sequence = _sync_tool_state_sequence(tool_state, next_sequence)
            return result
        if (
            on_opening_activity is not None
            and (has_visible_activity is None or not has_visible_activity())
        ):
            activity = _build_worker_opening_progress_activity(
                task=task,
                agent=agent,
                run_context=run_context,
                sequence=next_sequence,
            )
            await on_opening_activity(activity)
            next_sequence += 1
            next_sequence = _sync_tool_state_sequence(tool_state, next_sequence)
        arguments = _tool_call_arguments(tool_call)
        arguments_payload = _build_tool_arguments_event_payload(arguments)
        await event_log.append_event(
            run_id=run_context.agent_run_id,
            thread_id=run_context.thread_id,
            project_id=run_context.project_id,
            sequence=next_sequence,
            event_type=EventType.TOOL_CALL_STARTED,
            activity_owner=f"agent:{agent.agent_name}",
            payload={
                "tool_call_id": call_id,
                "tool_name": tool_name,
                "agent_name": agent.agent_name,
                "task_id": task.id,
                **arguments_payload,
            },
        )
        next_sequence += 1
        next_sequence = _sync_tool_state_sequence(tool_state, next_sequence)
        try:
            tool_result = await _maybe_await(original_call(tool_call))
        except Exception as exc:
            next_sequence = _sync_tool_state_sequence(tool_state, next_sequence)
            await event_log.append_event(
                run_id=run_context.agent_run_id,
                thread_id=run_context.thread_id,
                project_id=run_context.project_id,
                sequence=next_sequence,
                event_type=EventType.TOOL_CALL_FAILED,
                activity_owner=f"agent:{agent.agent_name}",
                payload={
                    "tool_call_id": call_id,
                    "tool_name": tool_name,
                    "agent_name": agent.agent_name,
                    "task_id": task.id,
                    "error_type": type(exc).__name__,
                    "error": _summarize_tool_result(str(exc)),
                },
            )
            next_sequence += 1
            next_sequence = _sync_tool_state_sequence(tool_state, next_sequence)
            raise

        async def _recording_result_iterator():
            nonlocal next_sequence
            last_content: Any = None
            try:
                async for chunk in tool_result:
                    last_content = getattr(chunk, "content", chunk)
                    yield chunk
            except Exception as exc:
                next_sequence = _sync_tool_state_sequence(tool_state, next_sequence)
                await event_log.append_event(
                    run_id=run_context.agent_run_id,
                    thread_id=run_context.thread_id,
                    project_id=run_context.project_id,
                    sequence=next_sequence,
                    event_type=EventType.TOOL_CALL_FAILED,
                    activity_owner=f"agent:{agent.agent_name}",
                    payload={
                        "tool_call_id": call_id,
                        "tool_name": tool_name,
                        "agent_name": agent.agent_name,
                        "task_id": task.id,
                        "error_type": type(exc).__name__,
                        "error": _summarize_tool_result(str(exc)),
                    },
                )
                next_sequence += 1
                next_sequence = _sync_tool_state_sequence(tool_state, next_sequence)
                raise
            next_sequence = _sync_tool_state_sequence(tool_state, next_sequence)
            await event_log.append_event(
                run_id=run_context.agent_run_id,
                thread_id=run_context.thread_id,
                project_id=run_context.project_id,
                sequence=next_sequence,
                event_type=EventType.TOOL_CALL_COMPLETED,
                activity_owner=f"agent:{agent.agent_name}",
                payload={
                    "tool_call_id": call_id,
                    "tool_name": tool_name,
                    "agent_name": agent.agent_name,
                    "task_id": task.id,
                    "result_summary": _summarize_tool_result(last_content),
                },
            )
            next_sequence += 1
            next_sequence = _sync_tool_state_sequence(tool_state, next_sequence)

        return _recording_result_iterator()

    setattr(toolkit, "call_tool_function", _recording_call_tool_function)
    nested_agent = getattr(worker, "agent", None)
    if nested_agent is not None and getattr(nested_agent, "toolkit", None) is toolkit:
        setattr(nested_agent.toolkit, "call_tool_function", _recording_call_tool_function)
    return lambda: next_sequence


async def _default_agentscope_worker_factory(
    *,
    model_key: str,
    run_context: ShadowCloneV2RunContext,
    agent: AgentIdentity | None = None,
    **_: Any,
) -> Any:
    """Build a production AgentScope worker for one Shadow Clone V2 turn."""
    from agentscope.memory import InMemoryMemory

    from agentscope_integration.adapters.thread_manager_adapter import (
        ThreadManagerAdapter,
    )
    from agentscope_integration.agents.worker import WorkerAgent
    from agentscope_integration.models import ModelFactory
    from agentscope_integration.tools import ToolkitAdapter

    if "/" in str(model_key or ""):
        model, formatter = ModelFactory.create_from_full_name(
            model_name=model_key,
            trace=run_context.trace,
        )
    else:
        model, formatter = ModelFactory.create(
            model_key=model_key,
            trace=run_context.trace,
        )
    thread_manager = (
        ThreadManagerAdapter(db_client=run_context.db_client)
        if run_context.db_client is not None
        else None
    )
    toolkit = ToolkitAdapter(
        project_id=run_context.project_id,
        thread_id=run_context.thread_id,
        thread_manager=thread_manager,
        db_client=run_context.db_client,
        shadow_clone_run_id=run_context.agent_run_id,
        strict_sandbox=False,
        shadow_clone_fail_fast=False,
        trace=run_context.trace,
    ).get_toolkit()

    from agentscope_integration.shadow_clone_v2.agent_tools import (
        ShadowCloneV2ToolContext,
        register_agent_tools,
    )

    shadow_tools = register_agent_tools(
        toolkit,
        context=ShadowCloneV2ToolContext(
            run_id=run_context.agent_run_id,
            thread_id=run_context.thread_id,
            project_id=run_context.project_id,
            account_id=run_context.account_id,
            actor_name=agent.agent_name if agent is not None else "team-lead",
            sequence_start=run_context.tool_sequence_start,
        ),
    )
    worker = WorkerAgent(
        model=model,
        formatter=formatter,
        toolkit=toolkit,
        memory=InMemoryMemory(),
        long_term_memory=None,
        long_term_memory_mode="static_control",
    )
    setattr(worker, "_shadow_clone_v2_agent_tools", shadow_tools)
    return worker


class AgentScopeShadowCloneV2TaskExecutor:
    """Concrete V2 executor that runs one task through an AgentScope worker."""

    def __init__(self, *, worker_factory: WorkerFactory | None = None) -> None:
        self.worker_factory = worker_factory or _default_agentscope_worker_factory

    async def execute(
        self,
        *,
        task: Task,
        agent: AgentIdentity,
        run_context: ShadowCloneV2RunContext,
        inbox_messages: list[Any] | None = None,
        on_stream_activity: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> str:
        model_key = _select_worker_model(run_context)
        prompt = _build_worker_task_prompt(
            task=task,
            agent=agent,
            inbox_messages=inbox_messages,
        )
        worker = await _maybe_await(
            self.worker_factory(
                model_key=model_key,
                prompt=prompt,
                task=task,
                agent=agent,
                run_context=run_context,
            )
        )
        visible_activity_emitted = False

        def _has_visible_activity() -> bool:
            return visible_activity_emitted

        async def _emit_stream_activity(activity: dict[str, Any]) -> None:
            nonlocal visible_activity_emitted
            if on_stream_activity is not None:
                await on_stream_activity(activity)
            visible_activity_emitted = True

        worker_tool_next_sequence = _install_worker_tool_event_recorder(
            worker,
            task=task,
            agent=agent,
            run_context=run_context,
            has_visible_activity=_has_visible_activity,
            on_opening_activity=_emit_stream_activity if on_stream_activity is not None else None,
        )

        from agentscope.message import Msg

        user_msg = Msg(
            name="ShadowCloneV2Facilitator",
            content=prompt,
            role="user",
        )

        try:
            if on_stream_activity is not None:
                stream_messages = globals().get("stream_printing_messages")
                if stream_messages is None:
                    from agentscope.pipeline import stream_printing_messages as stream_messages

                worker_agent = (
                    worker.get_agent()
                    if callable(getattr(worker, "get_agent", None))
                    else getattr(worker, "agent", worker)
                )
                worker_response_unset = object()
                response = None
                captured_worker_response: Any = worker_response_unset

                async def _run_worker_once() -> Any:
                    nonlocal captured_worker_response
                    captured_worker_response = await _maybe_await(worker(user_msg))
                    return captured_worker_response

                stream_sequence = max(0, int(run_context.tool_sequence_start or 0))
                async for stream_msg, is_last in stream_messages(
                    agents=[worker_agent],
                    coroutine_task=_run_worker_once(),
                ):
                    response = stream_msg if is_last else response
                    activity = _build_worker_stream_activity(
                        task=task,
                        agent=agent,
                        run_context=run_context,
                        msg=stream_msg,
                        is_last=bool(is_last),
                        sequence=stream_sequence,
                    )
                    stream_sequence += 1
                    if activity is not None:
                        await _emit_stream_activity(activity)
                if (
                    captured_worker_response is not worker_response_unset
                    and captured_worker_response is not None
                ):
                    response = captured_worker_response
                elif response is None:
                    if captured_worker_response is not worker_response_unset:
                        response = captured_worker_response
                    else:
                        response = await _run_worker_once()
            else:
                response = await _maybe_await(worker(user_msg))
        except Exception as exc:
            tool_state = getattr(worker, "_shadow_clone_v2_agent_tools", None)
            next_sequence = getattr(tool_state, "next_sequence", None)
            worker_next_sequence = worker_tool_next_sequence()
            if isinstance(worker_next_sequence, int):
                next_sequence = max(
                    next_sequence if isinstance(next_sequence, int) else 0,
                    worker_next_sequence,
                )
            raise ShadowCloneV2TaskExecutionError(
                str(exc),
                next_sequence=next_sequence if isinstance(next_sequence, int) else None,
            ) from exc

        tool_state = getattr(worker, "_shadow_clone_v2_agent_tools", None)
        next_sequence = getattr(tool_state, "next_sequence", None)
        worker_next_sequence = worker_tool_next_sequence()
        if isinstance(worker_next_sequence, int):
            next_sequence = max(
                next_sequence if isinstance(next_sequence, int) else 0,
                worker_next_sequence,
            )
        return ShadowCloneV2TaskExecutionResult(
            output=_response_to_text(response),
            next_sequence=next_sequence if isinstance(next_sequence, int) else None,
        )


class StubShadowCloneV2TaskExecutor:
    """Temporary executor until the real AgentScope worker is wired."""

    async def execute(
        self,
        *,
        task: Task,
        agent: AgentIdentity,
        run_context: ShadowCloneV2RunContext,
        inbox_messages: list[Any] | None = None,
    ) -> str:
        return f"Shadow Clone V2 task {task.id} completed by {agent.agent_name}."
