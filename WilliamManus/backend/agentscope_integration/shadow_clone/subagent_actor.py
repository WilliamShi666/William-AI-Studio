"""Dramatiq actor entrypoint for independent Shadow Clone subagents."""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any, Awaitable, Callable, Dict, Optional

import dramatiq  # type: ignore
from agentscope.message import Msg
from agentscope.pipeline import stream_printing_messages

from agentscope_integration.streaming import SSEAdapter
from services import redis as redis_service
from services.postgresql import DBConnection
from utils.agent_run_context import clear_agent_run_context, set_agent_run_context
from utils.dramatiq_queue_names import SHADOW_CLONE_SUBAGENT_QUEUE
from utils.logger import logger

from .constants import (
    SHADOW_CLONE_REDIS_WRITE_RETRY_ATTEMPTS,
    SHADOW_CLONE_REDIS_WRITE_TIMEOUT_SECONDS,
    SHADOW_CLONE_SUMMARY_MAX_CHARS,
    SHADOW_CLONE_SUBAGENT_ACTOR_TIME_LIMIT,
    SHADOW_CLONE_SUBAGENT_TIMEOUT,
)
from .failure_policy import classify_failure_text
from .peer_mailbox import (
    format_notes_for_result_appendix,
    reconcile_late_notes,
)
from .result_store import (
    ShadowCloneResultClaimLostError,
    ShadowCloneResultStateUnavailableError,
    submit_result,
)
from .sandbox_lease import get_run_sandbox_lease
from .state_machine import (
    TERMINAL_SHADOW_CLONE_STATUSES,
    claim_subagent_for_epoch,
    finalize_subagent_for_epoch,
    get_state,
    update_subagent_for_epoch as update_subagent,
)
from .subagent_factory import create_subagent


db = DBConnection()
_ACTIVITY_STREAM_TIMEOUT_SECONDS = min(
    1.0,
    float(SHADOW_CLONE_REDIS_WRITE_TIMEOUT_SECONDS),
)
_DEFAULT_ATTACHABLE_BINDING_STATES = frozenset({"locked", "reattaching"})


class ShadowCloneExecutionInvalidatedError(RuntimeError):
    """Raised when a subagent must stop because parent Shadow Clone execution is no longer valid."""


def _is_attachable_binding_state(state: Any) -> bool:
    normalized = str(state or "").strip().lower()
    if not normalized:
        return False

    try:
        from . import sandbox_lease as sandbox_lease_module

        for helper_name in (
            "is_shadow_clone_attachable_binding_state",
            "is_attachable_binding_state",
        ):
            helper = getattr(sandbox_lease_module, helper_name, None)
            if callable(helper):
                return bool(helper(normalized))

        for attr_name in (
            "ATTACHABLE_BINDING_STATES",
            "STRICT_ATTACHABLE_BINDING_STATES",
            "SHADOW_CLONE_ATTACHABLE_BINDING_STATES",
        ):
            states = getattr(sandbox_lease_module, attr_name, None)
            if states is not None:
                normalized_states = {
                    str(item or "").strip().lower()
                    for item in states
                    if str(item or "").strip()
                }
                if normalized_states:
                    return normalized in normalized_states
    except Exception:
        pass

    return normalized in _DEFAULT_ATTACHABLE_BINDING_STATES


async def _raise_if_execution_invalidated(
    *,
    parent_run_id: str,
    execution_epoch: int,
) -> None:
    try:
        state = await get_state(parent_run_id)
    except Exception:
        state = None
    if not isinstance(state, dict):
        raise ShadowCloneExecutionInvalidatedError(
            "Shadow Clone subagent aborted because the run state is no longer available."
        )

    current_status = str(state.get("status") or "").strip().lower()
    if current_status in TERMINAL_SHADOW_CLONE_STATUSES:
        raise ShadowCloneExecutionInvalidatedError(
            "Shadow Clone subagent aborted because the parent run is already terminal."
        )

    current_epoch = int(state.get("execution_epoch") or 0)
    if current_epoch != execution_epoch:
        raise ShadowCloneExecutionInvalidatedError(
            "Shadow Clone subagent aborted because the execution epoch was superseded."
        )

    try:
        lease = await get_run_sandbox_lease(parent_run_id)
    except Exception:
        lease = None

    binding_state = str((lease or {}).get("binding_state") or "").strip().lower()
    if binding_state and not _is_attachable_binding_state(binding_state):
        raise ShadowCloneExecutionInvalidatedError(
            "Shadow Clone subagent aborted because the sandbox lease is no longer active."
        )


def _response_to_text(response: Any) -> str:
    if response is None:
        return ""
    if hasattr(response, "get_text_content"):
        try:
            text = response.get_text_content()
            if isinstance(text, str):
                return text
        except Exception:
            pass
    content = getattr(response, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        chunks = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                chunks.append(str(block.get("text") or ""))
            elif hasattr(block, "text"):
                chunks.append(str(getattr(block, "text", "")))
        return "\n".join([item for item in chunks if item])
    return str(content or "")


def _truncate_for_state(text: str, max_chars: int = SHADOW_CLONE_SUMMARY_MAX_CHARS) -> str:
    if not text:
        return ""
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rstrip() + "..."


def _merge_result_with_late_peer_notes(base_result: str, late_notes: list[Dict[str, str]]) -> str:
    appendix = format_notes_for_result_appendix(late_notes)
    if not base_result.strip():
        return appendix
    return base_result.rstrip() + "\n\n" + appendix


def _build_provisional_result_summary(activity_payload: Dict[str, Any]) -> Optional[str]:
    message_type = str(activity_payload.get("message_type") or "").strip().lower()
    content = (
        activity_payload.get("content")
        if isinstance(activity_payload.get("content"), dict)
        else {}
    )
    metadata = (
        activity_payload.get("metadata")
        if isinstance(activity_payload.get("metadata"), dict)
        else {}
    )

    if message_type == "tool":
        tool_name = str(content.get("tool_name") or "").strip()
        if tool_name:
            return f"Subagent began executing tool: {tool_name}"
        return "Subagent began executing a tool."

    tool_calls = content.get("tool_calls")
    if isinstance(tool_calls, list) and tool_calls:
        first_call = tool_calls[0] if isinstance(tool_calls[0], dict) else {}
        function_payload = (
            first_call.get("function")
            if isinstance(first_call.get("function"), dict)
            else {}
        )
        tool_name = str(function_payload.get("name") or "").strip()
        if tool_name:
            return f"Subagent dispatched tool call: {tool_name}"
        return "Subagent dispatched a tool call."

    stream_status = str(metadata.get("stream_status") or "").strip().lower()
    if stream_status == "tool_result_chunk":
        return "Subagent received tool output."

    return None


async def _close_subagent_long_term_memory(subagent: Any) -> None:
    long_term_memory = getattr(subagent, "_shadow_readonly_long_term_memory", None)
    if long_term_memory is None or not hasattr(long_term_memory, "__aexit__"):
        return
    try:
        await long_term_memory.__aexit__(None, None, None)
    except Exception:
        logger.warning(
            "[ShadowClone] Failed to close subagent long-term memory sidecar",
            exc_info=True,
        )


def _safe_json_loads(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


async def _publish_shadow_clone_update(parent_run_id: str, payload: Dict[str, Any], *, timeout: float) -> None:
    await redis_service.publish(
        f"shadow_clone:{parent_run_id}:updates",
        json.dumps(payload, ensure_ascii=False),
        timeout=timeout,
    )


async def _publish_subagent_update(
    parent_run_id: str,
    subtask_id: str,
    status: str,
    *,
    execution_epoch: int,
    attempt_index: int | None = None,
    role: str | None = None,
    result_summary: str | None = None,
    error: str | None = None,
    failure_class: str | None = None,
) -> None:
    payload = {
        "subtask_id": subtask_id,
        "status": status,
        "execution_epoch": int(execution_epoch),
    }
    if role:
        payload["role"] = role
    if attempt_index is not None:
        payload["attempt_index"] = int(attempt_index)
    if result_summary:
        payload["result_summary"] = result_summary
    if error:
        payload["error"] = error
    if failure_class:
        payload["failure_class"] = failure_class
    await _publish_shadow_clone_update(
        parent_run_id,
        payload,
        timeout=SHADOW_CLONE_REDIS_WRITE_TIMEOUT_SECONDS,
    )


async def _publish_subagent_activity(parent_run_id: str, payload: Dict[str, Any]) -> None:
    activity_payload = {
        "event_type": "activity",
        **payload,
    }
    await _publish_shadow_clone_update(
        parent_run_id,
        activity_payload,
        timeout=_ACTIVITY_STREAM_TIMEOUT_SECONDS,
    )


async def _retry_shadow_clone_redis_action(
    operation_name: str,
    action: Callable[[], Awaitable[Any]],
    *,
    parent_run_id: str,
    subtask_id: str,
) -> Any:
    attempts = max(1, SHADOW_CLONE_REDIS_WRITE_RETRY_ATTEMPTS + 1)
    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        try:
            return await action()
        except (
            ShadowCloneResultClaimLostError,
            ShadowCloneResultStateUnavailableError,
        ):
            raise
        except Exception as exc:
            last_error = exc
            if attempt >= attempts:
                break
            backoff = min(0.5 * attempt, 2.0)
            logger.warning(
                "[ShadowClone] Redis action failed op=%s run_id=%s subtask_id=%s attempt=%s/%s error=%s",
                operation_name,
                parent_run_id,
                subtask_id,
                attempt,
                attempts,
                exc,
            )
            await asyncio.sleep(backoff)

    raise RuntimeError(
        f"Shadow Clone Redis action failed op={operation_name} run_id={parent_run_id} "
        f"subtask_id={subtask_id} after {attempts} attempts: {last_error}"
    )


def _build_activity_payload(
    *,
    subtask_id: str,
    role: str,
    sse_message: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    metadata = _safe_json_loads(sse_message.get("metadata"))
    content = _safe_json_loads(sse_message.get("content"))
    message_type = str(sse_message.get("type") or "assistant").strip().lower()
    stream_status = str(metadata.get("stream_status") or "").strip().lower()

    has_tool_calls = bool(content.get("tool_calls"))
    has_tool_result = message_type == "tool" and (
        content.get("tool_name") is not None or content.get("result") is not None
    )
    has_text = bool(str(content.get("content") or "").strip())
    has_reasoning = bool(str(content.get("reasoning_content") or "").strip())

    should_publish = (
        stream_status in {"chunk", "reasoning_chunk", "tool_call_chunk", "tool_result_chunk"}
        or has_tool_calls
        or has_tool_result
        or (
            stream_status == "complete"
            and message_type == "assistant"
            and (has_text or has_reasoning)
        )
    )
    if not should_publish:
        return None

    return {
        "subtask_id": subtask_id,
        "execution_epoch": metadata.get("shadow_clone_execution_epoch"),
        "sequence": sse_message.get("sequence"),
        "role": role,
        "message_type": message_type,
        "content": content,
        "metadata": metadata,
        "created_at": sse_message.get("created_at"),
        "updated_at": sse_message.get("updated_at"),
    }


async def _execute_subagent_with_activity(
    *,
    parent_run_id: str,
    subtask_id: str,
    role: str,
    thread_id: str,
    user_task: str,
    subagent,
    execution_epoch: int,
    on_useful_progress: Optional[Callable[[Dict[str, Any]], Awaitable[None]]] = None,
) -> Any:
    if hasattr(subagent, "set_console_output_enabled"):
        try:
            subagent.set_console_output_enabled(False)
        except Exception:
            pass

    thread_run_id = str(
        getattr(subagent, "_shadow_thread_run_id", f"shadow:{parent_run_id}:{subtask_id}"),
    )
    sse_adapter = SSEAdapter(thread_id, thread_run_id)
    request_message = Msg(name="user", content=user_task, role="user")
    final_response = None
    saw_stream_event = False
    agent_task: asyncio.Task[Any] | None = None
    stream_task_error: BaseException | None = None
    stream_coroutine = None

    def _ensure_agent_task() -> asyncio.Task[Any]:
        nonlocal agent_task
        if agent_task is None:
            agent_task = asyncio.create_task(subagent(request_message))
        return agent_task

    async def _abort_if_invalidated() -> None:
        await _raise_if_execution_invalidated(
            parent_run_id=parent_run_id,
            execution_epoch=execution_epoch,
        )

    async def _run_agent_task_for_stream() -> Any:
        nonlocal stream_task_error
        try:
            return await _ensure_agent_task()
        except BaseException as exc:
            stream_task_error = exc
            return None

    async def _cancel_and_drain_agent_task() -> None:
        task = agent_task
        if task is None:
            return
        if not task.done():
            task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except BaseException:
            pass

    def _close_stream_coroutine_if_idle() -> None:
        nonlocal stream_coroutine
        coroutine = stream_coroutine
        if coroutine is None or agent_task is not None:
            return
        if getattr(coroutine, "cr_running", False):
            return
        try:
            coroutine.close()
        except Exception:
            pass
        finally:
            stream_coroutine = None

    stream_coroutine = _run_agent_task_for_stream()
    try:
        async for msg, is_last in stream_printing_messages(
            agents=[subagent],
            coroutine_task=stream_coroutine,
        ):
            await _abort_if_invalidated()
            saw_stream_event = True
            if is_last:
                final_response = msg
            try:
                activity_payload = _build_activity_payload(
                    subtask_id=subtask_id,
                    role=role,
                    sse_message=sse_adapter.convert(msg, is_last),
                )
                if activity_payload:
                    metadata = activity_payload.setdefault("metadata", {})
                    metadata["shadow_clone_execution_epoch"] = int(execution_epoch)
                    await _publish_subagent_activity(parent_run_id, activity_payload)
                    if callable(on_useful_progress):
                        await on_useful_progress(activity_payload)
            except Exception as activity_error:
                logger.debug(
                    "[ShadowClone] Failed to publish activity run_id=%s subtask_id=%s: %s",
                    parent_run_id,
                    subtask_id,
                    activity_error,
                )
    except ShadowCloneExecutionInvalidatedError:
        await _cancel_and_drain_agent_task()
        raise
    except Exception:
        if saw_stream_event:
            await _cancel_and_drain_agent_task()
            raise
        await _cancel_and_drain_agent_task()
        logger.debug(
            "[ShadowClone] Streaming path unavailable for run_id=%s subtask_id=%s; falling back to non-streaming execution",
            parent_run_id,
            subtask_id,
            exc_info=True,
        )
        await _abort_if_invalidated()
        return await subagent(request_message)
    finally:
        _close_stream_coroutine_if_idle()

    if stream_task_error is not None:
        raise stream_task_error

    return final_response


async def execute_subagent(
    parent_run_id: str,
    subtask_id: str,
    subtask_config: Dict[str, Any],
    project_id: str,
    thread_id: str,
    *,
    initialize_db: bool = True,
    langfuse_trace_id: str = "",
) -> None:
    """Run one subtask with isolated timeout and report status/results."""
    role = str(subtask_config.get("role") or "specialist")
    user_task = str(subtask_config.get("task_description") or "").strip()
    execution_epoch = int(subtask_config.get("execution_epoch") or 0)
    attempt_index = max(1, int(subtask_config.get("attempt_index") or 1))
    status = "failed"
    result_summary = ""
    _lf_span = None
    _lf_client = None

    if langfuse_trace_id:
        try:
            from services.langfuse import langfuse as _lf_client_ref

            _lf_client = _lf_client_ref
            _lf_span = _lf_client.start_observation(
                name=f"subagent:{role}:{subtask_id[:8]}",
                as_type="span",
                trace_context={"trace_id": langfuse_trace_id},
                input={"role": role, "task": user_task[:300], "subtask_id": subtask_id},
            )
        except Exception:
            _lf_span = None
            _lf_client = None

    set_agent_run_context(parent_run_id, thread_id)
    try:
        if initialize_db:
            await db.initialize()
        db_client = await db.client

        try:
            state = await get_state(parent_run_id)
        except Exception:
            state = None
        if not isinstance(state, dict):
            clear_agent_run_context()
            return
        current_status = str(state.get("status") or "").strip().lower()
        if current_status in TERMINAL_SHADOW_CLONE_STATUSES:
            clear_agent_run_context()
            return
        current_epoch = int(state.get("execution_epoch") or 0)
        if current_epoch != execution_epoch:
            clear_agent_run_context()
            return
        claim_token = uuid.uuid4().hex

        try:
            running_marked = await _retry_shadow_clone_redis_action(
                "claim_subagent_running",
                lambda: claim_subagent_for_epoch(
                    parent_run_id,
                    subtask_id,
                    expected_epoch=execution_epoch,
                    owner_token=claim_token,
                    role=role,
                    timeout=SHADOW_CLONE_REDIS_WRITE_TIMEOUT_SECONDS,
                ),
                parent_run_id=parent_run_id,
                subtask_id=subtask_id,
            )
            if running_marked is False:
                logger.info(
                    "[ShadowClone] Aborting subagent before execution because the run state is no longer active "
                    "run_id=%s subtask_id=%s expected_epoch=%s",
                    parent_run_id,
                    subtask_id,
                    execution_epoch,
                )
                clear_agent_run_context()
                return
        except Exception as start_status_error:
            logger.warning(
                "[ShadowClone] Failed to claim subagent execution run_id=%s subtask_id=%s; aborting execution: %s",
                parent_run_id,
                subtask_id,
                start_status_error,
            )
            clear_agent_run_context()
            return

        failure_class = ""
        late_peer_notes_count = 0
        persisted_result = ""
        provisional_result_persisted = False
        subagent = None

        try:
            async def _persist_useful_progress(activity_payload: Dict[str, Any]) -> None:
                nonlocal provisional_result_persisted
                if provisional_result_persisted:
                    return

                provisional_summary = _build_provisional_result_summary(activity_payload)
                if not provisional_summary:
                    return

                try:
                    persist_result = await _retry_shadow_clone_redis_action(
                        "submit_result",
                        lambda: submit_result(
                            run_id=parent_run_id,
                            subtask_id=subtask_id,
                            role=role,
                            status="running",
                            full_result=provisional_summary,
                            summary_override=provisional_summary,
                            attempt_index=attempt_index,
                            expected_epoch=execution_epoch,
                            owner_token=claim_token,
                            timeout=SHADOW_CLONE_REDIS_WRITE_TIMEOUT_SECONDS,
                        ),
                        parent_run_id=parent_run_id,
                        subtask_id=subtask_id,
                    )
                except (
                    ShadowCloneResultStateUnavailableError,
                    ShadowCloneResultClaimLostError,
                ):
                    provisional_result_persisted = True
                    return
                except Exception as persist_error:
                    logger.debug(
                        "[ShadowClone] Failed to persist provisional subagent result run_id=%s "
                        "subtask_id=%s: %s",
                        parent_run_id,
                        subtask_id,
                        persist_error,
                    )
                    return

                provisional_result_persisted = bool(
                    not isinstance(persist_result, dict)
                    or persist_result.get("status") in {"submitted", "stale_ignored"}
                )

            subagent = await create_subagent(
                subtask_config=subtask_config,
                project_id=project_id,
                parent_run_id=parent_run_id,
                subtask_id=subtask_id,
                db_client=db_client,
                thread_id=thread_id,
                langfuse_trace_id=langfuse_trace_id,
                langfuse_parent_observation=_lf_span,
            )
            response = await asyncio.wait_for(
                _execute_subagent_with_activity(
                    parent_run_id=parent_run_id,
                    subtask_id=subtask_id,
                    role=role,
                    thread_id=thread_id,
                    user_task=user_task,
                    subagent=subagent,
                    execution_epoch=execution_epoch,
                    on_useful_progress=_persist_useful_progress,
                ),
                timeout=SHADOW_CLONE_SUBAGENT_TIMEOUT,
            )
            result_summary = _response_to_text(response)
            status = "completed"
        except ShadowCloneExecutionInvalidatedError as exc:
            logger.info(
                "[ShadowClone] Subagent execution invalidated run_id=%s subtask_id=%s: %s",
                parent_run_id,
                subtask_id,
                exc,
            )
            if subagent is not None:
                await _close_subagent_long_term_memory(subagent)
            clear_agent_run_context()
            return
        except asyncio.TimeoutError:
            status = "failed"
            result_summary = (
                f"SubAgent timed out after {SHADOW_CLONE_SUBAGENT_TIMEOUT} seconds."
            )
            failure_class = classify_failure_text(result_summary)
        except Exception as exc:
            status = "failed"
            result_summary = f"SubAgent execution failed: {exc}"
            failure_class = classify_failure_text(result_summary)
            logger.error(
                "[ShadowClone] SubAgent failed run_id=%s subtask_id=%s error=%s",
                parent_run_id,
                subtask_id,
                exc,
                exc_info=True,
            )

        peer_note_cursor = 0
        seen_peer_note_ids: set[str] = set()
        if subagent is not None:
            peer_note_cursor = int(getattr(subagent, "_shadow_peer_note_cursor", 0) or 0)
            seen_peer_note_ids = set(
                getattr(subagent, "_shadow_seen_peer_note_ids", set()) or set(),
            )
        persisted_result = result_summary
        skip_final_persistence = False
        try:
            await _raise_if_execution_invalidated(
                parent_run_id=parent_run_id,
                execution_epoch=execution_epoch,
            )
        except ShadowCloneExecutionInvalidatedError as exc:
            skip_final_persistence = True
            logger.info(
                "[ShadowClone] Skipping final subagent persistence after invalidation "
                "run_id=%s subtask_id=%s: %s",
                parent_run_id,
                subtask_id,
                exc,
            )

        try:
            if not skip_final_persistence:
                late_notes, peer_note_cursor = await reconcile_late_notes(
                    run_id=parent_run_id,
                    recipient_subtask_id=subtask_id,
                    cursor=peer_note_cursor,
                    seen_note_ids=seen_peer_note_ids,
                    timeout=SHADOW_CLONE_REDIS_WRITE_TIMEOUT_SECONDS,
                )
                if late_notes:
                    late_peer_notes_count = len(late_notes)
                    persisted_result = _merge_result_with_late_peer_notes(
                        result_summary,
                        late_notes,
                    )
                    logger.info(
                        "[ShadowClone] Reconciled late peer notes run_id=%s subtask_id=%s count=%s",
                        parent_run_id,
                        subtask_id,
                        late_peer_notes_count,
                    )
                if subagent is not None:
                    setattr(subagent, "_shadow_peer_note_cursor", peer_note_cursor)
        except Exception:
            logger.warning(
                "[ShadowClone] Late peer note reconciliation failed run_id=%s subtask_id=%s",
                parent_run_id,
                subtask_id,
                exc_info=True,
            )

        skip_terminal_updates = skip_final_persistence
        if not skip_final_persistence:
            try:
                await _raise_if_execution_invalidated(
                    parent_run_id=parent_run_id,
                    execution_epoch=execution_epoch,
                )
            except ShadowCloneExecutionInvalidatedError as exc:
                skip_terminal_updates = True
                logger.info(
                    "[ShadowClone] Skipping final subagent result submit after invalidation "
                    "run_id=%s subtask_id=%s: %s",
                    parent_run_id,
                    subtask_id,
                    exc,
                )

        if not skip_terminal_updates:
            try:
                persist_result = await _retry_shadow_clone_redis_action(
                    "submit_result",
                    lambda: submit_result(
                        run_id=parent_run_id,
                        subtask_id=subtask_id,
                        role=role,
                        status=status,
                        full_result=persisted_result,
                        summary_override=result_summary if late_peer_notes_count > 0 else None,
                        late_peer_notes_count=late_peer_notes_count,
                        attempt_index=attempt_index,
                        failure_class=failure_class or None,
                        expected_epoch=execution_epoch,
                        owner_token=claim_token,
                        timeout=SHADOW_CLONE_REDIS_WRITE_TIMEOUT_SECONDS,
                    ),
                    parent_run_id=parent_run_id,
                    subtask_id=subtask_id,
                )
                if (
                    isinstance(persist_result, dict)
                    and persist_result.get("status") == "stale_ignored"
                ):
                    skip_terminal_updates = True
            except ShadowCloneResultStateUnavailableError:
                logger.warning(
                    "[ShadowClone] Run state disappeared before result persistence completed "
                    "run_id=%s subtask_id=%s expected_epoch=%s",
                    parent_run_id,
                    subtask_id,
                    execution_epoch,
                )
                return
            except ShadowCloneResultClaimLostError:
                logger.warning(
                    "[ShadowClone] Subtask claim was lost before result persistence completed "
                    "run_id=%s subtask_id=%s expected_epoch=%s",
                    parent_run_id,
                    subtask_id,
                    execution_epoch,
                )
                return
            except Exception as persist_error:
                logger.error(
                    "[ShadowClone] Failed to persist subagent result run_id=%s subtask_id=%s: %s",
                    parent_run_id,
                    subtask_id,
                    persist_error,
                    exc_info=True,
                )
                status = "failed"
                if not result_summary:
                    result_summary = f"Result persistence failed: {persist_error}"

        if skip_terminal_updates:
            logger.info(
                "[ShadowClone] Skipping terminal updates for stale subagent result run_id=%s subtask_id=%s expected_epoch=%s",
                parent_run_id,
                subtask_id,
                execution_epoch,
            )
            return

        try:
            terminal_state_updated = await _retry_shadow_clone_redis_action(
                "finalize_subagent_terminal",
                lambda: finalize_subagent_for_epoch(
                    parent_run_id,
                    subtask_id,
                    status,
                    expected_epoch=execution_epoch,
                    owner_token=claim_token,
                    result_summary=_truncate_for_state(result_summary),
                    role=role,
                    timeout=SHADOW_CLONE_REDIS_WRITE_TIMEOUT_SECONDS,
                ),
                parent_run_id=parent_run_id,
                subtask_id=subtask_id,
            )
            if terminal_state_updated is False:
                logger.warning(
                    "[ShadowClone] Skipping terminal publish because the run state is no longer active "
                    "run_id=%s subtask_id=%s expected_epoch=%s",
                    parent_run_id,
                    subtask_id,
                    execution_epoch,
                )
                return
        except Exception as state_error:
            logger.error(
                "[ShadowClone] Failed to persist terminal subagent state run_id=%s subtask_id=%s: %s",
                parent_run_id,
                subtask_id,
                state_error,
                exc_info=True,
            )

        try:
            try:
                state = await get_state(parent_run_id)
            except Exception:
                state = None
            current_epoch = (
                int(state.get("execution_epoch"))
                if isinstance(state, dict) and state.get("execution_epoch") is not None
                else -1
            )
            if current_epoch == execution_epoch:
                terminal_summary = _truncate_for_state(result_summary)
                try:
                    await _publish_subagent_update(
                        parent_run_id,
                        subtask_id,
                        status,
                        execution_epoch=execution_epoch,
                        attempt_index=attempt_index,
                        role=role,
                        result_summary=terminal_summary,
                        error=terminal_summary if status == "failed" else None,
                        failure_class=failure_class or None,
                    )
                except TypeError:
                    await _publish_subagent_update(parent_run_id, subtask_id, status)
        except Exception as publish_error:
            logger.warning(
                "[ShadowClone] Failed to publish subagent update run_id=%s subtask_id=%s status=%s: %s",
                parent_run_id,
                subtask_id,
                status,
                publish_error,
            )

        try:
            if subagent is not None:
                await _close_subagent_long_term_memory(subagent)
        except Exception:
            logger.warning(
                "[ShadowClone] Failed to close subagent long-term memory run_id=%s subtask_id=%s",
                parent_run_id,
                subtask_id,
                exc_info=True,
            )
    finally:
        if _lf_span:
            try:
                _lf_span.update(
                    output={"status": status, "result": result_summary[:300]},
                    level="DEFAULT" if status == "completed" else "WARNING",
                )
                _lf_span.end()
            except Exception:
                pass
            if _lf_client is not None:
                try:
                    _lf_client.flush()
                except Exception:
                    pass
        clear_agent_run_context()


@dramatiq.actor(
    time_limit=SHADOW_CLONE_SUBAGENT_ACTOR_TIME_LIMIT * 1000,
    max_retries=0,
    queue_name=SHADOW_CLONE_SUBAGENT_QUEUE,
)
async def run_subagent(
    parent_run_id: str,
    subtask_id: str,
    subtask_config: Dict[str, Any],
    project_id: str,
    thread_id: str,
    langfuse_trace_id: str = "",
):
    await execute_subagent(
        parent_run_id=parent_run_id,
        subtask_id=subtask_id,
        subtask_config=subtask_config,
        project_id=project_id,
        thread_id=thread_id,
        initialize_db=True,
        langfuse_trace_id=langfuse_trace_id,
    )
